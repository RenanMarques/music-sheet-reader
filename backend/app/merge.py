"""Combine separate per-voice scores (one file per voice) into a single MusicXML with one part
per voice.

- Voices are identified by app.voices: the user's choice for each file, else the voice printed on
  the sheet, else by comparing the parts' pitches. Parts are ordered from the highest voice down.
- OMR sometimes reads a treble clef as "8va" (or misses the "8" of a tenor clef), putting a whole
  part an octave off. A part whose voice is known and whose median pitch is an octave away from
  that voice's usual range is shifted back; a part of unknown voice in treble "8va" (never used
  for singers) is shifted down. The clef's octave change is adjusted so the drawing is unchanged.
- OMR also drops measures now and then, so measures are aligned section by section: section
  barlines (double bars, repeats, voltas) found in every part act as anchors, and a part that is
  short inside a section gets whole-measure rests before the section's last measure. A missed
  measure then only disturbs its own section instead of shifting the rest of the piece.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from fractions import Fraction
from statistics import median
from xml.etree import ElementTree as ET

from .musicxml import MusicXMLError, _midi, _parse_root
from .voices import VOICE_TYPES, Line, VoiceType, assign, clef_is_low, find_type, numbered

VOICE_ORDER = {v.name: i for i, v in enumerate(VOICE_TYPES)}

# An octave error moves the median ~12 semitones; real voices stay well within this of their center.
_OCTAVE_ERROR_MIN = 9


@dataclass
class PartFile:
    """One uploaded sheet, as MusicXML."""

    xml: bytes
    label: str  # file name, for messages
    voice: str | None = None  # the user's choice: a voice type ("Soprano") or any name ("Solista")
    printed_voice: VoiceType | None = None  # voice printed on the sheet (app.sheettext)


@dataclass
class MergedVoice:
    file: int  # index in the files given to merge_scores
    name: str
    abbr: str | None
    how: str  # "escolhida" | "texto da partitura" | "alturas"


@dataclass
class MergeResult:
    xml: bytes
    voices: list[MergedVoice]  # in score order
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- octave check


def _shift_octaves(part: ET.Element, shift: int) -> None:
    for p in part.iter("pitch"):
        p.find("octave").text = str(int(p.findtext("octave")) + shift)
    # Keep the drawing: a note written on the same line now sounds `shift` octaves away.
    for clef in part.iter("clef"):
        oc = clef.find("clef-octave-change")
        value = (int(oc.text) if oc is not None and (oc.text or "").strip().lstrip("-").isdigit() else 0) + shift
        if oc is None:
            oc = ET.SubElement(clef, "clef-octave-change")
        if value:
            oc.text = str(value)
        else:
            clef.remove(oc)


def _pitches(part: ET.Element) -> list[int]:
    return [_midi(p) for p in part.iter("pitch")]


def fix_octave(part: ET.Element, center: float) -> int:
    """Shift a part of known voice back into its usual range if OMR put it an octave off.
    Returns the shift applied, in octaves."""
    pitches = _pitches(part)
    if not pitches:
        return 0
    diff = center - median(pitches)
    if abs(diff) < _OCTAVE_ERROR_MIN:
        return 0
    shift = round(diff / 12)
    if abs(shift) != 1:
        return 0  # not an octave misreading: the voice is wrong, not the notes
    _shift_octaves(part, shift)
    return shift


def _first_clef(part: ET.Element) -> tuple[str, int, int] | None:
    clef = part.find("measure/attributes/clef")
    if clef is None:
        return None
    try:
        return (
            clef.findtext("sign", "").strip(),
            int(clef.findtext("line") or 0),
            int(clef.findtext("clef-octave-change") or 0),
        )
    except ValueError:
        return None


def fix_treble_8va(part: ET.Element) -> int:
    """Singers' parts never use a treble "8va" clef: OMR misread it. Returns the shift (octaves)."""
    clef = _first_clef(part)
    if clef and clef[0] == "G" and clef[2] > 0:
        _shift_octaves(part, -clef[2])
        return -clef[2]
    return 0


def low_clef(part: ET.Element) -> bool | None:
    """Whether the part is written for a low voice (bass clef, treble 8vb, tenor clef)."""
    clef = _first_clef(part)
    return clef_is_low(*clef) if clef else None


# --------------------------------------------------------------------------- measure alignment

_PLAIN_BARS = {"regular", "none", "dashed", "dotted", "short", "tick", "heavy"}


def _boundaries(measures: list[ET.Element]) -> list[int]:
    """Indices i such that a section barline (double bar, repeat, volta) falls right after measure i."""
    out = set()
    for i, m in enumerate(measures):
        for bl in m.findall("barline"):
            plain = (bl.findtext("bar-style") or "regular").strip() in _PLAIN_BARS
            if plain and bl.find("repeat") is None and bl.find("ending") is None:
                continue
            b = i - 1 if bl.get("location") == "left" else i if bl.get("location", "right") == "right" else None
            if b is not None and 0 <= b < len(measures) - 1:
                out.add(b)
    return sorted(out)


def _sections(measures: list[ET.Element], bounds: list[int]) -> list[list[ET.Element]]:
    out, start = [], 0
    for b in bounds + [len(measures) - 1]:
        out.append(measures[start : b + 1])
        start = b + 1
    return out


def _rest_measure(part: ET.Element, before: ET.Element | None) -> ET.Element:
    """A whole-measure rest using the divisions/time signature in force at `before`."""
    divisions, time = 1, None
    for m in part.findall("measure"):
        if m is before:
            break
        for attrs in m.findall("attributes"):
            if attrs.findtext("divisions"):
                divisions = max(1, int(float(attrs.findtext("divisions"))))
            if attrs.find("time") is not None:
                time = attrs.find("time")
    quarters = Fraction(4)
    if time is not None:
        try:
            beats = sum(int(b) for b in (time.findtext("beats") or "4").split("+"))
            quarters = Fraction(beats * 4, int(time.findtext("beat-type") or "4"))
        except ValueError:
            pass
    m = ET.Element("measure")
    note = ET.SubElement(m, "note")
    ET.SubElement(note, "rest", measure="yes")
    ET.SubElement(note, "duration").text = str(int(quarters * divisions))
    ET.SubElement(note, "voice").text = "1"
    return m


def align_measures(parts: list[tuple[str, ET.Element]]) -> list[str]:
    """Give every part the same number of measures (OSMD and the analysis need it).
    Returns warnings (pt-BR) describing the measures that were filled in."""
    measures = [p.findall("measure") for _, p in parts]
    if len({len(ms) for ms in measures}) == 1:
        return []
    bounds = [_boundaries(ms) for ms in measures]
    if len({len(b) for b in bounds}) == 1:
        sections = [_sections(ms, b) for ms, b in zip(measures, bounds)]
    else:  # parts disagree on the section barlines: a single section, filled at the end
        sections = [[ms] for ms in measures]

    n_sections = len(sections[0])
    targets = [max(len(s[k]) for s in sections) for k in range(n_sections)]
    # Measure numbers come from a part that needed no filling (else they are renumbered).
    ref = next((i for i, s in enumerate(sections) if all(len(s[k]) == targets[k] for k in range(n_sections))), None)

    warnings = []
    new_lists = []
    for (label, part), secs in zip(parts, sections):
        out = []
        position = 0
        for k, sec in enumerate(secs):
            missing = targets[k] - len(sec)
            if missing:
                last = sec[-1] if sec else None
                pads = [_rest_measure(part, last) for _ in range(missing)]
                sec = sec[:-1] + pads + sec[-1:] if sec else pads
                first, end = position + 1, position + targets[k]
                warnings.append(
                    f"{label}: {missing} compasso(s) não reconhecido(s) entre os compassos {{{first}}} e {{{end}}}; "
                    "completado(s) com pausa."
                )
            out.extend(sec)
            position += targets[k]
        new_lists.append(out)
        for m in part.findall("measure"):
            part.remove(m)
        part.extend(out)

    # Consistent measure numbers across parts.
    if ref is not None:
        numbers = [(m.get("number"), m.get("implicit")) for m in new_lists[ref]]
    else:
        first = new_lists[0][0]
        implicit = first.get("implicit") == "yes"
        numbers = [(str(i + (0 if implicit else 1)), "yes" if implicit and i == 0 else None) for i in range(len(new_lists[0]))]
    for ms in new_lists:
        for m, (number, implicit) in zip(ms, numbers):
            m.set("number", number or "")
            if implicit:
                m.set("implicit", implicit)
            elif "implicit" in m.attrib:
                del m.attrib["implicit"]
    return [_fill_numbers(w, numbers) for w in warnings]


def _fill_numbers(warning: str, numbers: list[tuple[str | None, str | None]]) -> str:
    return re.sub(r"\{(\d+)\}", lambda m: numbers[int(m.group(1)) - 1][0] or m.group(1), warning)


# --------------------------------------------------------------------------- split parts

# Share of measures (with notes) that may have notes in more than one part, for the parts of
# one file to still count as a single voice split by the OMR.
_SPLIT_OVERLAP = 0.1


def _pitched(measure: ET.Element) -> int:
    return sum(1 for n in measure.iter("note") if n.find("pitch") is not None)


def _divisions_at_start(measures: list[ET.Element]) -> list[int]:
    """Divisions in force at the start of each measure."""
    out, divisions = [], 1
    for m in measures:
        out.append(divisions)
        for attrs in m.findall("attributes"):
            if attrs.findtext("divisions"):
                divisions = max(1, int(float(attrs.findtext("divisions"))))
    return out


def fold_split_parts(parts: list[ET.Element]) -> ET.Element | None:
    """Audiveris names parts after the label printed left of each system, so a voice sheet whose
    label it reads differently on each line ("C" as "0", "U", "O") comes out as several parts,
    each with notes only in its own systems. Fold such complementary parts into one, taking each
    measure from the part that has notes in it. Returns None if the parts really overlap."""
    measure_lists = [p.findall("measure") for p in parts]
    n = max(len(ms) for ms in measure_lists)
    counts = [[(_pitched(ms[i]), len(ms[i].findall("note"))) if i < len(ms) else (0, 0) for i in range(n)] for ms in measure_lists]
    with_notes = [sum(1 for c in col if c[0]) for col in zip(*counts)]
    if sum(1 for w in with_notes if w > 1) > _SPLIT_OVERLAP * max(1, sum(1 for w in with_notes if w)):
        return None

    starts = [_divisions_at_start(ms) for ms in measure_lists]
    folded = copy.deepcopy(parts[0])
    for m in folded.findall("measure"):
        folded.remove(m)
    running = None
    for i in range(n):
        best = max((k for k in range(len(parts)) if i < len(measure_lists[k])), key=lambda k: (counts[k][i], -k))
        m = copy.deepcopy(measure_lists[best][i])
        divisions = starts[best][i]
        first_attrs = m.find("attributes")
        sets_divisions = first_attrs is not None and first_attrs.findtext("divisions") and list(m).index(first_attrs) == 0
        if divisions != running and not sets_divisions:
            attrs = ET.Element("attributes")
            ET.SubElement(attrs, "divisions").text = str(divisions)
            m.insert(0, attrs)
        running = divisions
        for attrs in m.findall("attributes"):
            if attrs.findtext("divisions"):
                running = max(1, int(float(attrs.findtext("divisions"))))
        folded.append(m)
    return folded


# --------------------------------------------------------------------------- merge


def _octave_warning(label: str, shift: int) -> str:
    return f"{label}: notas lidas {abs(shift)} oitava(s) {'acima' if shift < 0 else 'abaixo'}; corrigido."


def merge_scores(files: list[PartFile], title: str) -> MergeResult:
    """Merge single-voice MusicXML files into one score, one part per voice, highest voice first."""
    roots = [_parse_root(f.xml) for f in files]

    # 1. Parts of each file (a voice the OMR split per system is folded back into one part).
    entries = []  # (file index, score-part, part)
    for fi, (root, f) in enumerate(zip(roots, files)):
        score_parts = {sp.get("id"): sp for sp in root.iterfind("part-list/score-part")}
        src_parts = [p for p in root.findall("part") if p.find("measure") is not None]
        if not src_parts:
            raise MusicXMLError(f"{f.label}: nenhum compasso reconhecido")
        if len(src_parts) > 1:
            folded = fold_split_parts(src_parts)
            if folded is not None:
                src_parts = [folded]
        for src in src_parts:
            entries.append((fi, score_parts.get(src.get("id")), copy.deepcopy(src)))

    # 2. Known voices (user's choice, printed on the sheet) and octave errors.
    warnings: list[str] = []
    lines: list[Line] = []
    hows: list[str] = []
    custom: list[str | None] = []  # names the user typed that are not a voice type
    shifts: list[int] = []
    for fi, _, part in entries:
        f = files[fi]
        chosen = find_type(f.voice)
        fixed = chosen or (None if f.voice else f.printed_voice)
        how = "escolhida" if f.voice else "texto da partitura" if fixed else "alturas"
        if fixed is None:
            shift = fix_treble_8va(part)
        elif low_clef(part) in (None, fixed.low) or fixed.name == "Tenor":
            # Right clef, wrong octave: an OMR misreading (treble "8va", tenor's "8" missed).
            shift = fix_octave(part, fixed.center)
        else:
            shift = 0  # the clef contradicts the voice: leave the notes, warn below
        shifts.append(shift)
        pitches = _pitches(part)
        lines.append(Line(median=float(median(pitches)) if pitches else 60.0, low_clef=low_clef(part), fixed=fixed))
        hows.append(how)
        custom.append(f.voice.strip() if f.voice and not chosen else None)

    # 3. The other voices, by comparing pitches.
    typed = [i for i, c in enumerate(custom) if c is None]  # names the user typed take no part
    types: list[VoiceType | None] = [None] * len(entries)
    for i, t in zip(typed, assign([lines[i] for i in typed])):
        types[i] = t
    names = numbered([c or t.name for c, t in zip(custom, types)], [ln.median for ln in lines])
    abbrs = [t.abbr + names[i][len(t.name):].strip() if t else None for i, t in enumerate(types)]
    warnings += [_octave_warning(names[i], shift) for i, shift in enumerate(shifts) if shift]

    # Voices the user/sheet named against what the pitches say (e.g. the Baixo above the Tenor).
    known = sorted((i for i in range(len(entries)) if lines[i].fixed), key=lambda i: VOICE_ORDER[lines[i].fixed.name])
    for a, b in zip(known, known[1:]):
        if lines[a].fixed.name != lines[b].fixed.name and lines[a].median + 3 < lines[b].median:
            warnings.append(f"{names[a]} está mais grave que {names[b]}: confira as vozes escolhidas.")

    # 4. Highest voice first (by type; names the user typed go by their pitch).
    def rank(i: int) -> tuple[float, float]:
        return (-(types[i].center if types[i] else lines[i].median), -lines[i].median)

    order = sorted(range(len(entries)), key=lambda i: (rank(i), i))

    out = ET.Element("score-partwise", version=roots[0].get("version", "4.0"))
    ET.SubElement(ET.SubElement(out, "work"), "work-title").text = title
    defaults = roots[0].find("defaults")
    if defaults is not None:
        out.append(copy.deepcopy(defaults))
    part_list = ET.SubElement(out, "part-list")
    parts: list[tuple[str, ET.Element]] = []
    voices: list[MergedVoice] = []
    for k, i in enumerate(order, 1):
        fi, old_sp, part = entries[i]
        new_id = f"P{k}"
        sp = copy.deepcopy(old_sp) if old_sp is not None else ET.Element("score-part")
        sp.set("id", new_id)
        for tag in ("part-name", "part-name-display", "part-abbreviation", "part-abbreviation-display"):
            for child in sp.findall(tag):
                sp.remove(child)
        pn = ET.Element("part-name")
        pn.text = names[i]
        sp.insert(0, pn)
        if abbrs[i]:
            pa = ET.Element("part-abbreviation")
            pa.text = abbrs[i]
            sp.insert(1, pa)
        part.set("id", new_id)
        # Instrument ids must stay unique in the merged score.
        for el in list(sp.iter()) + list(part.iter()):
            if el.tag in ("score-instrument", "midi-instrument", "midi-device", "instrument") and el.get("id"):
                el.set("id", f"{new_id}-{el.get('id')}")
        part_list.append(sp)
        parts.append((names[i], part))
        voices.append(MergedVoice(file=fi, name=names[i], abbr=abbrs[i], how=hows[i]))

    warnings += align_measures(parts)
    for _, part in parts:
        out.append(part)
    ET.indent(out)
    xml = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(out, encoding="utf-8")
    return MergeResult(xml=xml, voices=voices, warnings=warnings)
