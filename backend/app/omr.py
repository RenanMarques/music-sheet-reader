"""Optical music recognition through the Audiveris CLI (PDF/image -> MusicXML)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

AUDIVERIS_BIN = os.environ.get("AUDIVERIS_BIN", "/opt/audiveris/bin/Audiveris")
OMR_TIMEOUT = int(os.environ.get("OMR_TIMEOUT", "900"))


class OMRError(RuntimeError):
    pass


def run_audiveris(source: Path, out_dir: Path) -> Path:
    """Run Audiveris in batch mode and return the exported (uncompressed) MusicXML file.

    Audiveris writes `<stem>.xml`, or `<stem>.mvt<N>.xml` when it detects several
    movements; in that case the first movement is returned.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        AUDIVERIS_BIN,
        "-batch",
        "-export",
        "-output",
        str(out_dir),
        "-constant",
        "org.audiveris.omr.sheet.BookManager.useCompression=false",
        "--",
        str(source),
    ]
    env = {**os.environ, "HOME": os.environ.get("HOME") or "/tmp"}
    log_path = out_dir / "audiveris.log"
    try:
        with open(log_path, "w") as log:
            proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=OMR_TIMEOUT, env=env)
    except FileNotFoundError as e:
        raise OMRError(f"Audiveris não encontrado em {AUDIVERIS_BIN}") from e
    except subprocess.TimeoutExpired as e:
        raise OMRError(f"Reconhecimento excedeu {OMR_TIMEOUT}s") from e

    stem = source.stem
    candidates = sorted(out_dir.glob(f"{stem}.xml")) + sorted(out_dir.glob(f"{stem}.mvt*.xml"))
    if not candidates:
        tail = log_tail(log_path)
        raise OMRError(f"Audiveris não exportou MusicXML (código {proc.returncode}). Log:\n{tail}")
    return candidates[0]


def log_tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""
