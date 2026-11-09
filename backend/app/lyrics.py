"""Lyrics clean-up for OMR output and lyric editing on MusicXML.

Audiveris usually gets the hyphenation right (it turns the score's hyphens into
<syllabic> begin/middle/end), but it attaches each syllable to whichever voice owns
the note under it. Renderers only connect syllables of the same voice, so a word
whose syllables sit on soprano *and* alto notes shows dangling or doubled hyphens.
`normalize_lyrics` moves every syllable of a staff onto its top voice and drops
"lyrics" that are really footer credits read by the OCR.

Notes are addressed as (part id, staff number, measure index, onset in quarters
inside the measure) — the same key the frontend gets from OSMD's graphical model.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from xml.etree import ElementTree as ET

# A lyric line (part, staff, verse) with fewer syllables than this share of the
# staff's main line is treated as OCR noise (credits, page footers, stray text).
MIN_LINE_SHARE = 0.2
SYLLABIC = {"single", "begin", "middle", "end"}


class LyricsError(ValueError):
    pass


@dataclass
class NoteRef:
    el: ET.Element
    part: str
    staff: str
    measure: int
    onset: Fraction
    voice: int
    rest: bool
    chord_head: bool


def _iter_notes(root: ET.Element):
    for part in root.findall("part"):
        pid = part.get("id", "P?")
        divisions = 1
        for mi, measure in enumerate(part.findall("measure")):
            pos = Fraction(0)
            last = Fraction(0)
            for el in measure:
                if el.tag == "attributes":
                    d = el.findtext("divisions")
                    if d:
                        divisions = max(1, int(float(d)))
                elif el.tag == "backup":
                    pos = max(Fraction(0), pos - Fraction(int(float(el.findtext("duration", "0"))), divisions))
                elif el.tag == "forward":
                    pos += Fraction(int(float(el.findtext("duration", "0"))), divisions)
                elif el.tag == "note":
                    if el.find("grace") is not None:
                        continue
                    chord = el.find("chord") is not None
                    duration = Fraction(int(float(el.findtext("duration", "0"))), divisions)
                    onset = last if chord else pos
                    if not chord:
                        last = pos
                        pos += duration
                    voice = el.findtext("voice", "1").strip()
                    yield NoteRef(
                        el=el,
                        part=pid,
                        staff=el.findtext("staff", "1").strip() or "1",
                        measure=mi,
                        onset=onset,
                        voice=int(voice) if voice.isdigit() else 1,
                        rest=el.find("rest") is not None,
                        chord_head=not chord,
                    )


def _host(notes: list[NoteRef]) -> NoteRef | None:
    """The note that should carry lyrics at one (part, staff, measure, onset):
    the chord head of the lowest-numbered voice, ignoring rests."""
    sounding = [n for n in notes if not n.rest]
    if not sounding:
        return None
    return min(sounding, key=lambda n: (n.voice, not n.chord_head))


def _group(root: ET.Element) -> dict[tuple, list[NoteRef]]:
    groups: dict[tuple, list[NoteRef]] = {}
    for n in _iter_notes(root):
        groups.setdefault((n.part, n.staff, n.measure, n.onset), []).append(n)
    return groups


def _parse(data: bytes) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise LyricsError(f"MusicXML inválido: {e}") from e


def _serialize(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def normalize_lyrics(data: bytes) -> tuple[bytes, dict]:
    """Move each staff's syllables onto its top voice and drop noise lines.
    Returns the new MusicXML and stats {"moved", "dropped"}."""
    root = _parse(data)
    groups = _group(root)
    moved = dropped = 0

    # 1. Drop lyric lines that are much shorter than the staff's main line.
    lines: Counter = Counter()
    for (part, staff, _, _), notes in groups.items():
        for n in notes:
            for ly in n.el.findall("lyric"):
                lines[(part, staff, ly.get("number", "1"))] += 1
    main: Counter = Counter()
    for (part, staff, _), count in lines.items():
        main[(part, staff)] = max(main[(part, staff)], count)
    biggest = max(main.values(), default=0)
    for notes in groups.values():
        for n in notes:
            for ly in n.el.findall("lyric"):
                count = lines[(n.part, n.staff, ly.get("number", "1"))]
                # Compare with the staff's own main line and with the score's main line, so a
                # staff whose only "lyrics" are credits is dropped as a whole.
                if count < MIN_LINE_SHARE * main[(n.part, n.staff)] or count < MIN_LINE_SHARE * biggest:
                    n.el.remove(ly)
                    dropped += 1

    # 2. Move syllables onto the top voice at the same onset.
    for notes in groups.values():
        host = _host(notes)
        if host is None:
            continue
        taken = {ly.get("number", "1") for ly in host.el.findall("lyric")}
        for n in notes:
            if n is host:
                continue
            for ly in n.el.findall("lyric"):
                n.el.remove(ly)
                number = ly.get("number", "1")
                if number in taken:
                    dropped += 1  # the top voice already sings this verse here
                    continue
                _insert_lyric(host.el, ly)
                taken.add(number)
                moved += 1

    return _serialize(root), {"moved": moved, "dropped": dropped}


# Children of <note> that must come after <lyric> (MusicXML element order).
_AFTER_LYRIC = ("play", "listen")


def _insert_lyric(note: ET.Element, lyric: ET.Element) -> None:
    """Insert keeping lyrics in verse order and before <play>/<listen>."""
    children = list(note)
    number = _verse_key(lyric.get("number", "1"))
    index = len(children)
    for i, child in enumerate(children):
        if child.tag in _AFTER_LYRIC or (child.tag == "lyric" and _verse_key(child.get("number", "1")) > number):
            index = i
            break
    note.insert(index, lyric)


def _verse_key(number: str):
    return (0, int(number)) if number.isdigit() else (1, number)


def _parse_text(text: str) -> tuple[str, bool]:
    """'Can-' -> ('Can', True): a trailing hyphen means the word continues."""
    text = " ".join(text.split())
    continues = text.endswith("-")
    return text.rstrip("-").rstrip(), continues


def apply_lyric_edits(data: bytes, edits: list[dict]) -> bytes:
    """Apply edits: {part, staff, measure, offset (quarters), verse, text}.

    `text` follows the usual lyric-entry convention: a trailing "-" means the word
    continues on the next syllable; an empty text removes the syllable. The
    begin/middle/end/single of the edited syllable and of its neighbours in the
    same verse are recomputed so hyphens stay consistent.
    """
    root = _parse(data)
    groups = _group(root)
    touched: set[tuple] = set()
    for edit in edits:
        try:
            key = (
                str(edit["part"]),
                str(edit.get("staff", "1")),
                int(edit["measure"]),
                Fraction(edit["offset"]).limit_denominator(960),
            )
            verse = str(edit.get("verse", "1"))
            text, continues = _parse_text(str(edit.get("text", "")))
        except (KeyError, TypeError, ValueError) as e:
            raise LyricsError(f"Edição inválida: {edit!r}") from e
        notes = groups.get(key)
        if not notes:
            raise LyricsError(f"Nenhuma nota no compasso {key[2] + 1} (tempo {float(key[3]):g}) da parte {key[0]}")
        existing = next(
            ((n, ly) for n in notes for ly in n.el.findall("lyric") if ly.get("number", "1") == verse), None
        )
        if not text:
            if existing:
                existing[0].el.remove(existing[1])
            touched.add((key[0], key[1], verse))
            continue
        if existing:
            lyric = existing[1]
        else:
            host = _host(notes)
            if host is None:
                raise LyricsError("Não é possível pôr letra numa pausa")
            lyric = ET.Element("lyric", {"number": verse})
            _insert_lyric(host.el, lyric)
        # Rewrite the syllable: one <syllabic> + one <text>, keeping <extend>/<end-line>/etc.
        keep = [c for c in lyric if c.tag not in ("syllabic", "text", "elision")]
        for c in list(lyric):
            lyric.remove(c)
        ET.SubElement(lyric, "syllabic").text = "begin" if continues else "single"
        ET.SubElement(lyric, "text").text = text
        lyric.extend(keep)
        touched.add((key[0], key[1], verse))

    for part, staff, verse in touched:
        _fix_syllabic_chain(root, part, staff, verse)
    return _serialize(root)


def _fix_syllabic_chain(root: ET.Element, part: str, staff: str, verse: str) -> None:
    """Recompute syllabic from each syllable's own "continues" flag and its predecessor:
    a syllable continues a word iff the previous one (in time order) continued."""
    seq = []
    for n in _iter_notes(root):
        if n.part != part or n.staff != staff:
            continue
        for ly in n.el.findall("lyric"):
            if ly.get("number", "1") == verse:
                seq.append(((n.measure, n.onset), ly))
    seq.sort(key=lambda x: x[0])
    prev_continues = False
    for _, ly in seq:
        syl = ly.find("syllabic")
        value = syl.text if syl is not None else "single"
        continues = value in ("begin", "middle")
        new = ("middle" if continues else "end") if prev_continues else ("begin" if continues else "single")
        if syl is None:
            syl = ET.Element("syllabic")
            ly.insert(0, syl)
        syl.text = new
        prev_continues = continues
