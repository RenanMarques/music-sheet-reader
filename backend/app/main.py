"""HTTP API + static frontend.

Uploads are stored under DATA_DIR/scores/<id>/:
  meta.json      job status (queued | processing | done | error)
  source.<ext>   the uploaded file
  omr/           Audiveris output (only for PDF/images)
  For a score combined from one file per voice ("partes"): source-<n>.<ext> and omr/<n>/ per
  file, listed in meta["sources"] in choir order (see app.merge).
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

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from .lyrics import LyricsError, apply_lyric_edits, normalize_lyrics
from .layout import can_combine, can_separate, combine_staves, separate_staves
from .merge import PartFile, merge_scores
from .sheettext import common_title, credit_lines, pdf_lines
from .voices import voice_from_lines
from .musicxml import MusicXMLError, analyze, read_musicxml_bytes
from .omr import OMRError, run_audiveris

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
SCORES_DIR = DATA_DIR / "scores"
ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", ROOT / "frontend"))
VENDOR_DIR = Path(os.environ.get("VENDOR_DIR", ROOT / "vendor"))
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_PARTS = 12
MAX_NAME = 40

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


def _read_source(d: Path, source: Path, omr_dir: Path, what: str) -> bytes:
    """MusicXML for one uploaded file (running OMR on PDFs/images)."""
    if source.suffix.lower() in OMR_EXTS:
        _write_meta(d, status="processing", stage=f"Reconhecendo {what} (OMR)…")
        return run_audiveris(source, omr_dir).read_bytes()
    _write_meta(d, status="processing", stage=f"Lendo {what}…")
    return read_musicxml_bytes(source.read_bytes())


def _printed_lines(source: Path, xml: bytes) -> list[str]:
    """Text printed on the sheet: the PDF's text layer, else the MusicXML credits."""
    return (pdf_lines(source) if source.suffix.lower() == ".pdf" else []) or credit_lines(xml)


def _process_parts(d: Path, meta: dict) -> tuple[bytes, str]:
    """One file per voice: recognise each, identify the voices, combine (app.merge)."""
    sources = meta["sources"]
    part_files, texts = [], []
    for i, s in enumerate(sources, 1):
        source = d / s["file"]
        what = f"{s.get('voice') or s['filename']} ({i}/{len(sources)})"
        try:
            xml = _read_source(d, source, d / "omr" / str(i), what)
        except (OMRError, MusicXMLError) as e:
            raise type(e)(f"{s['filename']}: {e}") from e
        if source.suffix.lower() in OMR_EXTS:
            xml, _stats = normalize_lyrics(xml)
        lines = _printed_lines(source, xml)
        texts.append(lines)
        part_files.append(PartFile(xml=xml, label=s["filename"], voice=s.get("voice"), printed_voice=voice_from_lines(lines)))
    title = meta.get("user_title") or common_title(texts) or Path(sources[0]["filename"]).stem
    _write_meta(d, stage="Identificando e juntando as vozes…")
    result = merge_scores(part_files, title)
    _store_score(d, result.xml, normalize=False)
    # Sources in score order, each with the voice(s) found in it.
    for s in sources:
        s["voices"] = [{"name": v.name, "abbr": v.abbr, "how": v.how} for v in result.voices if sources[v.file] is s]
    first = {id(sources[v.file]): k for k, v in reversed(list(enumerate(result.voices)))}
    sources = sorted(sources, key=lambda s: first.get(id(s), len(sources)))
    _write_meta(d, sources=sources, warnings=result.warnings)
    return result.xml, title


def process(d: Path) -> None:
    meta = _read_meta(d)
    try:
        if meta.get("sources"):
            xml, title = _process_parts(d, meta)
        else:
            source = d / meta["source"]
            xml = _read_source(d, source, d / "omr", "a partitura")
            _store_score(d, xml, normalize=source.suffix.lower() in OMR_EXTS)
            # OCR'd titles are unreliable (Audiveris picks an arbitrary text line): use the filename.
            title = meta.get("user_title") or (Path(meta["filename"]).stem if source.suffix.lower() in OMR_EXTS else None)
        _write_meta(d, stage="Separando vozes…")
        result = analyze(xml, title_fallback=Path(meta["filename"]).stem)
        if not result["voices"]:
            raise MusicXMLError("Nenhuma nota reconhecida na partitura")
        if title:
            result["title"] = title
        (d / "analysis.json").write_text(json.dumps(result, ensure_ascii=False))
        _write_meta(
            d,
            status="done",
            stage=None,
            title=result["title"],
            voices=len(result["voices"]),
            combinable=can_combine(xml),
            separable=can_separate(xml),
        )
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
        # Scores processed before the combined/separate staff layouts existed.
        if meta.get("status") == "done" and ("combinable" not in meta or "separable" not in meta) and (d / "score.xml").exists():
            try:
                xml = (d / "score.xml").read_bytes()
                _write_meta(d, combinable=can_combine(xml), separable=can_separate(xml))
            except MusicXMLError:
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


async def _read_upload(file: UploadFile) -> tuple[str, bytes]:
    filename = Path(file.filename or "partitura").name
    if Path(filename).suffix.lower() not in OMR_EXTS | XML_EXTS:
        raise HTTPException(
            400, f"{filename}: formato não suportado. Envie PDF, imagem (PNG/JPG/TIFF) ou MusicXML (.xml/.musicxml/.mxl)."
        )
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"{filename}: arquivo maior que 50 MB")
    if not data:
        raise HTTPException(400, f"{filename}: arquivo vazio")
    return filename, data


@app.post("/api/scores", status_code=202)
async def upload(
    file: UploadFile | None = File(None),
    files: list[UploadFile] | None = File(None),
    voices: list[str] = Form([]),
    title: str = Form(""),
) -> dict:
    """Upload a score.

    - One file, no voice: one score (its voices are separated from the staves, app.musicxml).
    - Several files, or a file with a voice: one file per voice of the same piece, combined into
      one score (app.merge). `voices[i]` is the voice of `files[i]`: a voice type ("Soprano"),
      any name ("Solista"), or "" to identify it automatically.
    - `title` (optional) overrides the automatic title.
    """
    uploads = [file] if file else list(files or [])
    if not uploads:
        raise HTTPException(400, "Nenhum arquivo enviado")
    if len(uploads) > MAX_PARTS:
        raise HTTPException(400, f"Envie no máximo {MAX_PARTS} partes")
    if voices and len(voices) != len(uploads):
        raise HTTPException(400, "Informe uma voz por arquivo (vazia = automática)")
    voices = [v.strip()[:MAX_NAME] for v in voices] or [""] * len(uploads)
    received = [await _read_upload(u) for u in uploads]
    score_id = uuid.uuid4().hex[:12]
    d = SCORES_DIR / score_id
    d.mkdir(parents=True)
    fields: dict = {"user_title": title.strip()[:200] or None}
    if len(received) == 1 and not voices[0]:
        filename, data = received[0]
        ext = Path(filename).suffix.lower()
        (d / f"source{ext}").write_bytes(data)
        fields |= {"filename": filename, "source": f"source{ext}"}
    else:
        sources = []
        for i, ((filename, data), voice) in enumerate(zip(received, voices), 1):
            name = f"source-{i}{Path(filename).suffix.lower()}"
            (d / name).write_bytes(data)
            sources.append({"file": name, "filename": filename, "voice": voice or None})
        label = fields["user_title"] or (received[0][0] if len(received) == 1 else f"{len(received)} partes")
        fields |= {"filename": label, "source": sources[0]["file"], "sources": sources}
    meta = _write_meta(d, id=score_id, **fields, status="queued", stage="Na fila…", created=time.time())
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
    d = _dir(score_id)
    f = d / "analysis.json"
    if not f.exists():
        raise HTTPException(409, "Partitura ainda não processada")
    return JSONResponse(_apply_voice_layout(json.loads(f.read_text()), _read_meta(d).get("voice_layout")))


def _apply_voice_layout(analysis: dict, layout: list[dict] | None) -> dict:
    """The user's voice names and mixer order (meta["voice_layout"]) over the analysis.
    Each voice keeps its detected name as `auto_name`."""
    for v in analysis["voices"]:
        v["auto_name"] = v["name"]
    if not layout:
        return analysis
    by_id = {v["id"]: v for v in analysis["voices"]}
    ordered = []
    for item in layout:
        v = by_id.pop(item.get("id"), None)
        if v is not None:
            v["name"] = item.get("name") or v["name"]
            ordered.append(v)
    analysis["voices"] = ordered + list(by_id.values())
    return analysis


class VoiceItem(BaseModel):
    id: str
    name: str = ""


class VoiceLayout(BaseModel):
    voices: list[VoiceItem]  # mixer order; empty name = detected name; empty list = reset


@app.put("/api/scores/{score_id}/voices")
def set_voice_layout(score_id: str, body: VoiceLayout) -> dict:
    """Rename and reorder the voices (metadata only: the score and its analysis are unchanged)."""
    d = _dir(score_id)
    _require_done(d)
    ids = {v["id"] for v in json.loads((d / "analysis.json").read_text())["voices"]}
    layout = [{"id": v.id, "name": v.name.strip()[:MAX_NAME]} for v in body.voices]
    if any(v["id"] not in ids for v in layout) or len({v["id"] for v in layout}) != len(layout):
        raise HTTPException(400, "Vozes inválidas")
    return _write_meta(d, voice_layout=layout or None)


@app.get("/api/scores/{score_id}/musicxml")
def get_musicxml(score_id: str, layout: str = "parts") -> Response:
    """The score as stored, or a drawing layout (see app.layout): `layout=combined`, same-clef
    voices sharing a staff; `layout=separate`, each voice of a shared staff on its own staff,
    named as in the mixer."""
    d = _dir(score_id)
    f = d / "score.xml"
    if not f.exists():
        raise HTTPException(409, "Partitura ainda não processada")
    if layout == "combined":
        with _edit_lock:
            data = f.read_bytes()
        return Response(combine_staves(data), media_type="application/vnd.recordare.musicxml+xml")
    if layout == "separate":
        with _edit_lock:
            data = f.read_bytes()
        names = None
        if (d / "analysis.json").exists():
            analysis = _apply_voice_layout(json.loads((d / "analysis.json").read_text()), _read_meta(d).get("voice_layout"))
            by_id = {v["id"]: v["name"] for v in analysis["voices"]}
            names = [by_id.get(f"v{i + 1}") for i in range(len(by_id))]
        return Response(separate_staves(data, names), media_type="application/vnd.recordare.musicxml+xml")
    meta = _read_meta(d)
    name = (meta.get("title") or Path(meta["filename"]).stem) + ".musicxml"
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
def get_source(score_id: str, part: int = 0) -> FileResponse:
    """The uploaded file; for a score combined from parts, `part` picks which one (0-based)."""
    d = _dir(score_id)
    meta = _read_meta(d)
    sources = meta.get("sources") or [{"file": meta["source"], "filename": meta["filename"]}]
    if not 0 <= part < len(sources):
        raise HTTPException(404, "Parte não encontrada")
    return FileResponse(d / sources[part]["file"], filename=sources[part]["filename"], content_disposition_type="inline")


if VENDOR_DIR.exists():
    app.mount("/vendor", StaticFiles(directory=VENDOR_DIR), name="vendor")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
