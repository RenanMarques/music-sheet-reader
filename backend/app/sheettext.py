"""Text printed on a sheet: the text layer of digital PDFs (pdftotext), or the credits of a
MusicXML file (for scans these come from Audiveris' OCR and are less reliable)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

from .voices import find_type

PDFTOTEXT_TIMEOUT = 30
_NOT_TITLE = re.compile(r"https?:|www\.|@|©|copyright|direitos|autoriza|licen[çc]a", re.I)


def pdf_lines(path: Path) -> list[str]:
    """Non-empty text lines of the first page of a PDF ([] if it has no text layer)."""
    try:
        out = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "1", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            timeout=PDFTOTEXT_TIMEOUT,
            check=False,
        ).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def credit_lines(xml: bytes) -> list[str]:
    """Title/credit texts of a MusicXML file, top of the page first."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    lines = [t.strip() for t in (root.findtext("work/work-title"), root.findtext("movement-title")) if t and t.strip()]
    for credit in root.iter("credit"):
        for words in credit.iter("credit-words"):
            lines += [ln.strip() for ln in (words.text or "").splitlines() if ln.strip()]
    return lines


def _title_case(text: str) -> str:
    if text != text.upper():
        return text
    small = {"a", "o", "as", "os", "e", "de", "da", "do", "das", "dos", "em", "no", "na", "nos", "nas", "ao", "à"}
    words = text.lower().split()
    return " ".join(w if i and w in small else w[:1].upper() + w[1:] for i, w in enumerate(words))


def common_title(texts: list[list[str]], header: int = 10) -> str | None:
    """Title of a piece given as several sheets (one per voice): the first header line that all
    of them print, other than a voice name or a number/credit line."""
    if len(texts) < 2 or not all(texts):
        return None
    for line in texts[0][:header]:
        if len(re.findall(r"[^\W\d_]", line)) < 3 or _NOT_TITLE.search(line) or find_type(line):
            continue
        if all(line in t[:header] for t in texts[1:]):
            return _title_case(line.strip(" .·-"))
    return None
