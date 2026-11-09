"""HTTP API + static frontend.

Uploads are stored under DATA_DIR/scores/<id>/:
  meta.json      job status (queued | processing | done | error)
  source.<ext>   the uploaded file
  omr/           Audiveris output (only for PDF/images)
  score.orig.xml MusicXML before any lyric edits (for OMR: lyrics already normalised)
  score.xml      MusicXML shown and downloaded (score.orig.xml + lyric edits)
  analysis.json  playback model from app.musicxml.analyze
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from .lyrics import LyricsError, apply_lyric_edits, normalize_lyrics
from .musicxml import MusicXMLError, analyze, read_musicxml_bytes
from .omr import OMRError, run_audiveris

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
SCORES_DIR = DATA_DIR / "scores"
ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", ROOT / "frontend"))
VENDOR_DIR = Path(os.environ.get("VENDOR_DIR", ROOT / "vendor"))
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

OMR_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif"}
XML_EXTS = {".xml", ".musicxml", ".mxl"}

# Audiveris is memory hungry: process one score at a time.
executor = ThreadPoolExecutor(max_workers=1)
_lock = threading.Lock()
_edit_lock = threading.Lock()


def _dir(score_id: str) -> Path:
    if not score_id.isalnum():
        raise HTTPException(404, "Partitura não encontrada")
    d = SCORES_DIR / score_id
    if not (d / "meta.json").exists():
        raise HTTPException(404, "Partitura não encontrada")
    return d


def _read_meta(d: Path) -> dict:
    return json.loads((d / "meta.json").read_text())


def _write_meta(d: Path, **changes) -> dict:
    with _lock:
        meta = _read_meta(d) if (d / "meta.json").exists() else {}
        meta.update(changes, updated=time.time())
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False))
        tmp.replace(d / "meta.json")
    return meta


def process(d: Path) -> None:
    meta = _read_meta(d)
    source = d / meta["source"]
    title = Path(meta["filename"]).stem
    try:
        if source.suffix.lower() in OMR_EXTS:
            _write_meta(d, status="processing", stage="Reconhecendo a partitura (OMR)…")
            xml_path = run_audiveris(source, d / "omr")
            xml = xml_path.read_bytes()
        else:
            _write_meta(d, status="processing", stage="Lendo MusicXML…")
            xml = read_musicxml_bytes(source.read_bytes())
        is_omr = source.suffix.lower() in OMR_EXTS
        _store_score(d, xml, normalize=is_omr)
        _write_meta(d, stage="Separando vozes…")
        result = analyze(xml, title_fallback=title)
        if not result["voices"]:
            raise MusicXMLError("Nenhuma nota reconhecida na partitura")
        if source.suffix.lower() in OMR_EXTS:
            # OCR'd titles are unreliable (Audiveris picks an arbitrary text line): use the filename.
            result["title"] = title
        (d / "analysis.json").write_text(json.dumps(result, ensure_ascii=False))
        _write_meta(d, status="done", stage=None, title=result["title"], voices=len(result["voices"]))
    except (OMRError, MusicXMLError) as e:
        _write_meta(d, status="error", stage=None, error=str(e))
    except Exception as e:  # noqa: BLE001 - report anything to the UI
        traceback.print_exc()
        _write_meta(d, status="error", stage=None, error=f"Erro inesperado: {e}")


def _store_score(d: Path, xml: bytes, normalize: bool) -> None:
    """Write score.orig.xml/score.xml. OMR output gets its lyrics normalised (see app.lyrics);
    uploaded MusicXML is kept as is, since its voices/lyrics were written on purpose."""
    if normalize:
        xml, _stats = normalize_lyrics(xml)
    (d / "score.orig.xml").write_bytes(xml)
    (d / "score.xml").write_bytes(xml)
    _write_meta(d, lyrics_edited=False)


def resume_pending() -> None:
    SCORES_DIR.mkdir(parents=True, exist_ok=True)
    for meta_file in SCORES_DIR.glob("*/meta.json"):
        meta = json.loads(meta_file.read_text())
        d = meta_file.parent
        # Scores processed before lyric normalisation existed.
        if meta.get("status") == "done" and not (d / "score.orig.xml").exists() and (d / "score.xml").exists():
            try:
                _store_score(d, (d / "score.xml").read_bytes(), normalize=Path(meta["source"]).suffix.lower() in OMR_EXTS)
            except LyricsError:
                traceback.print_exc()
        if meta.get("status") in ("queued", "processing"):
            _write_meta(meta_file.parent, status="queued", stage="Na fila…")
            executor.submit(process, meta_file.parent)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    resume_pending()
    yield


app = FastAPI(title="Leitor de Partituras", lifespan=lifespan)


@app.middleware("http")
async def revalidate_by_default(request, call_next):
    """Without Cache-Control, browsers cache heuristically and may keep serving an old app.js or
    an old score.xml (e.g. after lyrics are normalised/edited). Make them revalidate (ETag) instead."""
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.post("/api/scores", status_code=202)
async def upload(file: UploadFile) -> dict:
    filename = Path(file.filename or "partitura").name
    ext = Path(filename).suffix.lower()
    if ext not in OMR_EXTS | XML_EXTS:
        raise HTTPException(400, "Formato não suportado. Envie PDF, imagem (PNG/JPG/TIFF) ou MusicXML (.xml/.musicxml/.mxl).")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Arquivo maior que 50 MB")
    if not data:
        raise HTTPException(400, "Arquivo vazio")
    score_id = uuid.uuid4().hex[:12]
    d = SCORES_DIR / score_id
    d.mkdir(parents=True)
    (d / f"source{ext}").write_bytes(data)
    meta = _write_meta(
        d, id=score_id, filename=filename, source=f"source{ext}", status="queued", stage="Na fila…", created=time.time()
    )
    executor.submit(process, d)
    return meta


@app.get("/api/scores")
def list_scores() -> list[dict]:
    metas = [json.loads(p.read_text()) for p in SCORES_DIR.glob("*/meta.json")]
    return sorted(metas, key=lambda m: m.get("created", 0), reverse=True)


@app.get("/api/scores/{score_id}")
def get_score(score_id: str) -> dict:
    return _read_meta(_dir(score_id))


@app.delete("/api/scores/{score_id}", status_code=204)
def delete_score(score_id: str) -> Response:
    d = _dir(score_id)
    if _read_meta(d).get("status") in ("queued", "processing"):
        raise HTTPException(409, "A partitura ainda está sendo processada")
    shutil.rmtree(d)
    return Response(status_code=204)


@app.get("/api/scores/{score_id}/analysis")
def get_analysis(score_id: str) -> JSONResponse:
    f = _dir(score_id) / "analysis.json"
    if not f.exists():
        raise HTTPException(409, "Partitura ainda não processada")
    return JSONResponse(json.loads(f.read_text()))


@app.get("/api/scores/{score_id}/musicxml")
def get_musicxml(score_id: str) -> FileResponse:
    d = _dir(score_id)
    f = d / "score.xml"
    if not f.exists():
        raise HTTPException(409, "Partitura ainda não processada")
    name = Path(_read_meta(d)["filename"]).stem + ".musicxml"
    return FileResponse(
        f,
        media_type="application/vnd.recordare.musicxml+xml",
        filename=name,
        headers={"Cache-Control": "no-cache"},  # changes when lyrics are edited
    )


class LyricEdit(BaseModel):
    part: str
    staff: str = "1"
    measure: int
    offset: float  # quarters from the start of the measure
    verse: str = "1"
    text: str  # trailing "-" = word continues; "" removes the syllable


class LyricEdits(BaseModel):
    edits: list[LyricEdit]


def _require_done(d: Path) -> None:
    if _read_meta(d).get("status") != "done":
        raise HTTPException(409, "Partitura ainda não processada")


@app.put("/api/scores/{score_id}/lyrics")
def edit_lyrics(score_id: str, body: LyricEdits) -> dict:
    d = _dir(score_id)
    _require_done(d)
    with _edit_lock:
        try:
            xml = apply_lyric_edits((d / "score.xml").read_bytes(), [e.model_dump() for e in body.edits])
        except LyricsError as e:
            raise HTTPException(400, str(e)) from e
        (d / "score.xml").write_bytes(xml)
    return _write_meta(d, lyrics_edited=True)


@app.post("/api/scores/{score_id}/lyrics/restore")
def restore_lyrics(score_id: str) -> dict:
    d = _dir(score_id)
    _require_done(d)
    with _edit_lock:
        shutil.copyfile(d / "score.orig.xml", d / "score.xml")
    return _write_meta(d, lyrics_edited=False)


@app.get("/api/scores/{score_id}/source")
def get_source(score_id: str) -> FileResponse:
    d = _dir(score_id)
    meta = _read_meta(d)
    return FileResponse(d / meta["source"], filename=meta["filename"], content_disposition_type="inline")


if VENDOR_DIR.exists():
    app.mount("/vendor", StaticFiles(directory=VENDOR_DIR), name="vendor")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
