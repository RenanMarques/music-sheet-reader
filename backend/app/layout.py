"""Alternative drawing layouts. Only the drawing changes: playback always comes from the analysis
of the stored score.xml.

- combined (`combine_staves`): neighbouring single-voice parts that use the same clef
  (Soprano + Contralto, Tenor + Baixo) on one staff, hymnal style. The combined staff keeps the
  id of its top part and only the top voice's lyrics, so a lyric edit made on it (part, staff 1,
  measure, offset) addresses the top voice's notes in score.xml unchanged.
- separate (`separate_staves`): the other way round, each voice of a shared staff on its own
  staff. The voices are the ones the analysis finds (`musicxml.separate`), so each new staff is
  exactly one voice of the mixer. A new part's id is `<part>__s<staff>v<voice>` (`split_part_id`),
  so a lyric edit made on it can be addressed to the original (part, staff); lyrics are drawn on
  the highest voice singing at each onset.
"""

from __future__ import annotations

import copy
import re
from fractions import Fraction
from math import gcd, lcm
from xml.etree import ElementTree as ET

from .lyrics import _group, _host, _insert_lyric, _iter_notes
from .musicxml import _parse_root, separate
from .voices import find_type

# MusicXML <note> child order (the schema is a sequence): new children go after these.
_BEFORE_VOICE = ("grace", "chord", "pitch", "rest", "unpitched", "cue", "duration", "tie", "instrument", "footnote", "level")
_BEFORE_STEM = _BEFORE_VOICE + ("voice", "type", "dot", "accidental", "time-modification")


def _clef(part: ET.Element) -> tuple | None:
    clef = part.find("measure/attributes/clef")
    if clef is None:
        return None
    return (clef.findtext("sign", "").strip(), clef.findtext("line", "").strip(), int(clef.findtext("clef-octave-change") or 0))


def _single_staff(part: ET.Element) -> bool:
    return all(int(s.text or 1) <= 1 for s in part.iter("staves"))


def _groups(root: ET.Element) -> list[list[ET.Element]]:
    """Consecutive single-staff parts with the same clef and number of measures."""
    groups: list[list[ET.Element]] = []
    prev_key = None
    for part in root.findall("part"):
        key = (_clef(part), len(part.findall("measure"))) if _single_staff(part) and _clef(part) else None
        if key is not None and key == prev_key:
            groups[-1].append(part)
        else:
            groups.append([part])
        prev_key = key
    return groups


def can_combine(data: bytes) -> bool:
    return any(len(g) > 1 for g in _groups(_parse_root(data)))


def _set_child(note: ET.Element, tag: str, text: str, after: tuple[str, ...]) -> None:
    el = note.find(tag)
    if el is None:
        el = ET.Element(tag)
        idx = max((i for i, c in enumerate(note) if c.tag in after), default=-1)
        note.insert(idx + 1, el)
    el.text = text


def _divisions_values(part: ET.Element) -> list[int]:
    return [max(1, int(float(d.text))) for d in part.iter("divisions") if (d.text or "").strip()] or [1]


def _combine_group(parts: list[ET.Element]) -> ET.Element:
    common = lcm(*[d for p in parts for d in _divisions_values(p)])
    stems = len(parts) == 2
    out = ET.Element("part", id=parts[0].get("id"))
    current = [1] * len(parts)  # divisions in force per source part
    measure_lists = [p.findall("measure") for p in parts]
    for i, base in enumerate(measure_lists[0]):
        m = ET.Element("measure", dict(base.attrib))
        right_bars = []
        for j, src in enumerate(ms[i] for ms in measure_lists):
            pos = Fraction(0)
            for el in src:
                if el.tag == "attributes" and el.findtext("divisions"):
                    current[j] = max(1, int(float(el.findtext("divisions"))))
                scale = Fraction(common, current[j])
                if el.tag not in ("note", "backup", "forward"):
                    if j > 0:
                        continue  # clef/key/time, directions and barlines come from the top voice
                    el = copy.deepcopy(el)
                    if el.tag == "attributes" and el.find("divisions") is not None:
                        el.find("divisions").text = str(common)
                    if el.tag == "barline" and el.get("location", "right") == "right":
                        right_bars.append(el)
                        continue
                    m.append(el)
                    continue
                el = copy.deepcopy(el)
                carrier = _is_carrier(el)
                dur = el.find("duration")
                if dur is not None and (dur.text or "").strip():
                    dur.text = str(int(Fraction(int(float(dur.text))) * scale))
                    d = Fraction(int(dur.text))
                else:
                    d = Fraction(0)
                if el.tag == "backup":
                    pos -= d
                elif el.tag == "forward":
                    pos += d
                    _set_child(el, "voice", str(CARRIER_VOICE + j if carrier else j + 1), ("duration", "footnote", "level"))
                elif carrier:  # a lyric line's invisible voice (place_lyrics): kept, with its words
                    pos += d
                    _set_child(el, "voice", str(CARRIER_VOICE + j), _BEFORE_VOICE)
                else:
                    if el.find("chord") is None and el.find("grace") is None:
                        pos += d
                    _set_child(el, "voice", str(j + 1), _BEFORE_VOICE)
                    if stems and el.find("rest") is None:
                        _set_child(el, "stem", "up" if j == 0 else "down", _BEFORE_STEM)
                    if j > 0:
                        for lyric in el.findall("lyric"):
                            el.remove(lyric)
                m.append(el)
            if j < len(parts) - 1 and pos > 0:
                backup = ET.SubElement(m, "backup")
                ET.SubElement(backup, "duration").text = str(int(pos))
        m.extend(right_bars)
        out.append(m)
    return out


def combine_staves(data: bytes) -> bytes:
    """MusicXML with same-clef neighbouring voices on shared staves (unchanged if there are none)."""
    root = _parse_root(data)
    groups = _groups(root)
    if all(len(g) == 1 for g in groups):
        return data
    part_list = root.find("part-list")
    score_parts = {sp.get("id"): sp for sp in part_list.findall("score-part")}
    for g in groups:
        if len(g) == 1:
            continue
        names = [(score_parts[p.get("id")].findtext("part-name") or "").strip() for p in g if p.get("id") in score_parts]
        abbrs = [(score_parts[p.get("id")].findtext("part-abbreviation") or "").strip() for p in g if p.get("id") in score_parts]
        top = score_parts.get(g[0].get("id"))
        if top is not None:
            if top.find("part-name") is not None and all(names):
                top.find("part-name").text = " / ".join(names)
            if top.find("part-abbreviation") is not None and all(abbrs):
                top.find("part-abbreviation").text = " ".join(abbrs)
            for p in g[1:]:
                sp = score_parts.get(p.get("id"))
                if sp is not None:
                    part_list.remove(sp)
        combined = _combine_group(g)
        idx = list(root).index(g[0])
        for p in g:
            root.remove(p)
        root.insert(idx, combined)
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


# --------------------------------------------------------------------------- separate staves

SPLIT_MARK = "__s"
_NOTE_TYPES = [("whole", Fraction(4)), ("half", Fraction(2)), ("quarter", Fraction(1)), ("eighth", Fraction(1, 2)),
               ("16th", Fraction(1, 4)), ("32nd", Fraction(1, 8)), ("64th", Fraction(1, 16))]
_VALUES = sorted(((base * (2 - Fraction(1, 2**dots)), name, dots) for name, base in _NOTE_TYPES for dots in (0, 1, 2)),
                 reverse=True)
# Children of <attributes> that may be numbered per staff.
_PER_STAFF = ("key", "time", "clef", "staff-details", "transpose", "measure-style", "part-symbol")
# Measure-level elements drawn once per original part (on its first new staff), not per voice.
_ONCE = ("direction", "sound", "harmony", "figured-bass", "listening")


def split_part_id(part: str, staff: str, voice: int) -> str:
    return f"{part}{SPLIT_MARK}{staff}v{voice}"


def can_separate(data: bytes) -> bool:
    return any(g["count"] > 1 for g in separate(_parse_root(data)).groups)


def _value(d: Fraction) -> tuple[str, int] | None:
    return next(((name, dots) for v, name, dots in _VALUES if v == d), None)


def _set_value(note: ET.Element, d: Fraction, divisions: int) -> None:
    """Duration (and type/dots, when expressible) of a note shortened to d quarters."""
    note.find("duration").text = str(int(d * divisions))
    value = _value(d)
    if value is None:
        return
    for dot in note.findall("dot"):
        note.remove(dot)
    _set_child(note, "type", value[0], _BEFORE_VOICE + ("voice",))
    idx = list(note).index(note.find("type"))
    for k in range(value[1]):
        note.insert(idx + 1 + k, ET.Element("dot"))


def _rest(d: Fraction, divisions: int, whole_measure: bool = False) -> ET.Element:
    note = ET.Element("note")
    rest = ET.SubElement(note, "rest")
    if whole_measure:
        rest.set("measure", "yes")
    ET.SubElement(note, "duration").text = str(int(d * divisions))
    ET.SubElement(note, "voice").text = "1"
    value = None if whole_measure else _value(d)
    if value:
        ET.SubElement(note, "type").text = value[0]
        for _ in range(value[1]):
            ET.SubElement(note, "dot")
    return note


def _rest_chunks(d: Fraction) -> list[Fraction]:
    """A gap as note values (greedy, longest first); a remainder that is no plain note value
    (tuplets) is one untyped rest."""
    out = []
    while d > 0:
        v = next((v for v, _, _ in _VALUES if v <= d), None)
        if v is None:
            out.append(d)
            break
        out.append(v)
        d -= v
    return out


def _untie_start(note: ET.Element) -> None:
    for tie in note.findall("tie"):
        if tie.get("type") == "start":
            note.remove(tie)
    for notations in note.findall("notations"):
        for tied in notations.findall("tied"):
            if tied.get("type") == "start":
                notations.remove(tied)
        if not len(notations):
            note.remove(notations)


def _fix_beams(notes: list[ET.Element]) -> None:
    """Keep only beam groups that are still complete (begin … end) in this voice."""
    valid: set[int] = set()
    run: list[int] = []
    for i, note in enumerate(notes):
        b = next((x.text for x in note.findall("beam") if x.get("number", "1") == "1"), None)
        if b == "begin":
            run = [i]
        elif b == "continue" and run:
            run.append(i)
        elif b == "end" and run:
            valid.update(run + [i])
            run = []
        else:
            run = []
    for i, note in enumerate(notes):
        if i not in valid:
            for beam in note.findall("beam"):
                note.remove(beam)


def _plan_voice(line, metas) -> dict[int, list[list]]:
    """Source notes a voice draws, per measure: [element, onset, duration, untie], monophonic.
    Notes the separation shortened (and tie continuations past that point) are cut."""
    by_measure: dict[int, list[list]] = {}
    for e in line:
        end = e.onset + e.duration
        kept = []
        for el, mi, on, d in e.src:
            if metas[mi].start + on >= end:
                break
            dd = min(d, end - metas[mi].start - on, metas[mi].length - on)
            if dd > 0:
                kept.append([el, on, dd, dd != d, mi])
        if kept and len(kept) < len(e.src):
            kept[-1][3] = True
        for el, on, dd, untie, mi in kept:
            by_measure.setdefault(mi, []).append([el, on, dd, untie])
    for items in by_measure.values():
        items.sort(key=lambda x: x[1])
        out = []
        for it in items:
            if out and out[-1][1] == it[1]:
                continue  # same onset twice in one voice (a chord on a one-voice staff): keep the top
            if out and out[-1][1] + out[-1][2] > it[1]:
                out[-1][2] = it[1] - out[-1][1]
                out[-1][3] = True
            out.append(it)
        items[:] = out
    return by_measure


def _attributes_for_staff(attrs: ET.Element, staff: str) -> ET.Element:
    attrs = copy.deepcopy(attrs)
    for child in list(attrs):
        if child.tag == "staves":
            attrs.remove(child)
        elif child.tag in _PER_STAFF and child.get("number") is not None:
            if child.get("number") != staff:
                attrs.remove(child)
            else:
                del child.attrib["number"]
    return attrs


def _separate_part(part: ET.Element, group: dict, v: int, first: bool, staff_first: bool, metas, common: int) -> ET.Element:
    """One voice of a part's staff as a part of its own. `first`: the first new part made from
    this part (gets its unnumbered directions); `staff_first`: the first drawn voice of its staff."""
    staff = group["staff"]
    plan = group["plans"][v]
    out = ET.Element("part", id=split_part_id(part.get("id"), staff, v + 1))
    divisions = 1
    for mi, measure in enumerate(part.findall("measure")):
        m = ET.SubElement(out, "measure", dict(measure.attrib))
        length = metas[mi].length if mi < len(metas) else Fraction(0)
        # Non-note elements with their position in the measure.
        placed: list[tuple[Fraction, int, ET.Element]] = []
        right_bars = []
        pos = Fraction(0)
        for el in measure:
            if el.tag == "attributes" and el.findtext("divisions"):
                divisions = max(1, int(float(el.findtext("divisions"))))
            if el.tag in ("note", "backup", "forward"):
                d = Fraction(int(float(el.findtext("duration") or 0)), divisions)
                if el.tag == "backup":
                    pos = max(Fraction(0), pos - d)
                elif el.tag == "forward" or (el.find("chord") is None and el.find("grace") is None):
                    pos += d
                continue
            if el.tag == "attributes":
                el = _attributes_for_staff(el, staff)
                if el.find("divisions") is not None:
                    el.find("divisions").text = str(common)
            elif el.tag in _ONCE:
                own = el.findtext("staff")
                if not staff_first or (own.strip() != staff if own else not first):
                    continue
                el = copy.deepcopy(el)
                if el.find("staff") is not None:
                    el.remove(el.find("staff"))
            else:
                el = copy.deepcopy(el)
            if el.tag == "barline" and el.get("location", "right") == "right":
                right_bars.append(el)
            else:
                placed.append((pos if el.tag != "barline" else Fraction(0), 0, el))
        # The voice's notes, gaps filled with rests.
        items = plan.get(mi, [])
        if not items:
            placed.append((Fraction(0), 1, _rest(length, common, whole_measure=True)))
        notes = []
        cursor = Fraction(0)
        for src, on, d, untie in items + [[None, length, 0, False]]:
            gap_at = cursor
            for chunk in _rest_chunks(on - cursor) if items else []:
                placed.append((gap_at, 1, _rest(chunk, common)))
                gap_at += chunk
            if src is None:
                break
            note = copy.deepcopy(src)
            for tag in ("chord", "staff", "stem", "instrument", "lyric"):
                for child in note.findall(tag):
                    note.remove(child)
            _set_child(note, "voice", "1", _BEFORE_VOICE)
            note.find("duration").text = str(int(d * common))
            if untie:
                _set_value(note, d, common)
                _untie_start(note)
            placed.append((on, 1, note))
            notes.append(note)
            cursor = on + d
        _fix_beams(notes)
        placed.sort(key=lambda x: (x[0], x[1]))
        m.extend(el for _, _, el in placed)
        m.extend(right_bars)
    return out


def separate_staves(data: bytes, names: list[str] | None = None, hidden: set[int] = frozenset(), split_all: bool = True) -> bytes:
    """MusicXML with each voice of a shared staff on its own staff (unchanged if there is nothing to do);
    see `_separate_tree`."""
    return _separate_and_place(data, names, hidden, split_all)[0]


def _separate_and_place(data: bytes, names, hidden, split_all) -> tuple[bytes, list[dict]]:
    root = _parse_root(data)
    if not _separate_tree(root, names, hidden, split_all):
        return data, []
    moved = place_lyrics(_parse_root(data), root)
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8"), moved


def _separate_tree(root: ET.Element, names: list[str] | None, hidden: set[int], split_all: bool) -> bool:
    """Splits shared staves of `root` in place, one part per voice; returns whether anything changed.
    Lyrics are left out: `place_lyrics` draws them afterwards.

    `names`: one per analysis voice (in analysis order), e.g. the user's names; default: detected.
    `hidden`: analysis voice indexes left out of the drawing. Parts whose voices are all hidden are
    removed; a staff where only some are hidden is split, so the others can be drawn alone.
    `split_all=False` splits only those staves (the drawing otherwise keeps the stored staves).
    Hiding every voice is ignored (an empty score cannot be drawn)."""
    sep = separate(root)
    total = sum(len(g["voices"]) for g in sep.groups)
    hidden = {i for i in hidden if 0 <= i < total}
    if len(hidden) == total:
        hidden = set()
    if not hidden and not (split_all and any(g["count"] > 1 for g in sep.groups)):
        return False
    names = [(names[i] if names and i < len(names) and names[i] else n) for i, n in enumerate(sep.names)]
    part_list = root.find("part-list")
    score_parts = {sp.get("id"): sp for sp in part_list.findall("score-part")}
    metas = sep.metas
    vi = 0
    by_part: dict[str, list[dict]] = {}
    for g in sep.groups:
        g["names"] = names[vi : vi + len(g["voices"])]
        g["shown"] = [v for v in range(len(g["voices"])) if vi + v not in hidden]
        vi += len(g["voices"])
        by_part.setdefault(g["part"], []).append(g)

    for part in root.findall("part"):
        groups = by_part.get(part.get("id"), [])
        if not groups:
            continue  # no notes at all: drawn as stored
        sp = score_parts.get(part.get("id"))
        if not any(g["shown"] for g in groups):
            root.remove(part)
            if sp is not None:
                part_list.remove(sp)
            continue
        partly_hidden = any(len(g["shown"]) < len(g["voices"]) for g in groups)
        if not partly_hidden and not (split_all and any(g["count"] > 1 for g in groups)):
            continue
        divs = _divisions_values(part)
        fractions = [mt.length for mt in metas]
        new_parts = []
        for g in groups:
            g["plans"] = [_plan_voice(line, metas) for line in g["voices"]]
            for v in g["shown"]:
                for items in g["plans"][v].values():
                    for _, on, d, _ in items:
                        fractions += [on, d]
        common = lcm(*divs, *[f.denominator for f in fractions])
        for g in groups:
            for v in g["shown"]:
                new_parts.append((g, v, _separate_part(part, g, v, not new_parts, v == g["shown"][0], metas, common)))
        # Replace the part (and its score-part) by one per shown voice.
        idx = list(root).index(part)
        root.remove(part)
        for k, (_, _, p) in enumerate(new_parts):
            root.insert(idx + k, p)
        if sp is not None:
            sp_idx = list(part_list).index(sp)
            part_list.remove(sp)
            for k, (g, v, p) in enumerate(new_parts):
                new_sp = copy.deepcopy(sp)
                new_sp.set("id", p.get("id"))
                for child in list(new_sp):
                    if child.tag in ("part-name-display", "part-abbreviation-display", "score-instrument",
                                     "midi-instrument", "midi-device", "player"):
                        new_sp.remove(child)
                name = g["names"][v]
                if new_sp.find("part-name") is None:
                    new_sp.insert(0, ET.Element("part-name"))
                new_sp.find("part-name").text = name
                t = find_type(name)
                abbr = t.abbr + "".join(re.findall(r"\d+", name)) if t else None  # "Soprano 2" -> "S2"
                pa = new_sp.find("part-abbreviation")
                if abbr:
                    if pa is None:
                        pa = ET.Element("part-abbreviation")
                        new_sp.insert(list(new_sp).index(new_sp.find("part-name")) + 1, pa)
                    pa.text = abbr
                elif pa is not None:
                    new_sp.remove(pa)
                part_list.insert(sp_idx + k, new_sp)
    return True


def _origin(part: str, staff: str) -> tuple[str, str]:
    """The stored (part, staff) a drawn staff comes from (split parts: `P1__s1v2` -> P1, 1)."""
    m = re.fullmatch(rf"(.*){SPLIT_MARK}([^v]+)v\d+", part)
    return (m.group(1), m.group(2)) if m else (part, staff)


def _lyric_lines(root: ET.Element) -> tuple[list[tuple], dict[tuple, dict[str, dict]]]:
    """Staves in score order and, per staff, its lyric lines: {verse: {(measure, onset): <lyric>}}."""
    order: list[tuple] = []
    lines: dict[tuple, dict[str, dict]] = {}
    for n in _iter_notes(root):
        key = (n.part, n.staff)
        if key not in lines:
            order.append(key)
            lines[key] = {}
        for ly in n.el.findall("lyric"):
            lines[key].setdefault(ly.get("number", "1"), {}).setdefault((n.measure, n.onset), ly)
    return order, lines


# --------------------------------------------------------------------------- lyrics on drawn staves

CARRIER_VOICE = 5  # MusicXML voice of the invisible notes that carry a lyric line (and up, when combined)
_STEPS = "CDEFGAB"
_CLEF_NOTE = {"G": ("G", 4), "F": ("F", 3), "C": ("C", 4)}


def _diatonic(step: str, octave: int) -> int:
    return octave * 7 + _STEPS.index(step)


def _middle_line(part: ET.Element, staff: str) -> int:
    """Diatonic position of the middle line of a staff (from its first clef)."""
    for clef in part.iter("clef"):
        if clef.get("number", staff) != staff:
            continue
        sign = clef.findtext("sign", "G").strip()
        if sign not in _CLEF_NOTE:
            return _diatonic("B", 4)  # percussion/TAB: any position will do
        step, octave = _CLEF_NOTE[sign]
        line = int(clef.findtext("line") or {"G": 2, "F": 4, "C": 3}[sign])
        return _diatonic(step, octave) + (3 - line) * 2 + 7 * int(clef.findtext("clef-octave-change") or 0)
    return _diatonic("B", 4)


def _is_carrier(el: ET.Element) -> bool:
    voice = (el.findtext("voice") or "").strip()
    return voice.isdigit() and int(voice) >= CARRIER_VOICE and (el.tag == "forward" or el.get("print-object") == "no")


def _set_stems(part: ET.Element, staff: str, middle: int) -> None:
    """Explicit stems for the visible notes of a staff that gets a carrier voice (with two voices,
    the drawing would otherwise turn every stem up): away from the farthest notehead, as usual."""
    chords: list[list[ET.Element]] = []
    for measure in part.findall("measure"):
        for el in measure.findall("note"):
            if el.find("rest") is not None or _is_carrier(el) or (el.findtext("staff") or "1").strip() != staff:
                continue
            if el.find("chord") is not None and chords:
                chords[-1].append(el)
            else:
                chords.append([el])
    for chord in chords:
        if any(n.find("stem") is not None for n in chord):
            continue
        pos = [_diatonic(n.findtext("pitch/step", "B").strip(), int(n.findtext("pitch/octave", "4"))) for n in chord if n.find("pitch") is not None]
        if not pos:
            continue
        up, down = max(pos) - middle, middle - min(pos)
        for n in chord:
            _set_child(n, "stem", "down" if up >= down else "up", _BEFORE_STEM)


def _rescale(part: ET.Element, k: int) -> None:
    for el in part.iter():
        if el.tag in ("divisions", "duration") and (el.text or "").strip():
            el.text = str(int(float(el.text)) * k)


def _add_carrier(part: ET.Element, staff: str, verses: dict[str, dict]) -> None:
    """A lyric line on invisible notes of its own voice, in the rhythm of the syllables: every
    syllable is drawn even where the staff's own notes don't start, hyphens included (they are
    only drawn within one voice)."""
    multi = any(int(s.text or 1) > 1 for s in part.iter("staves"))
    middle = _middle_line(part, staff)
    by_measure: dict[int, dict[Fraction, dict[str, ET.Element]]] = {}
    for verse, syllables in verses.items():
        for (mi, on), ly in syllables.items():
            by_measure.setdefault(mi, {}).setdefault(on, {})[verse] = ly
    measures = part.findall("measure")
    # Divisions in force per measure, and a common factor so every syllable onset is a whole number.
    divs, d, k = [], 1, 1
    for measure in measures:
        for att in measure.findall("attributes"):
            if att.findtext("divisions"):
                d = max(1, int(float(att.findtext("divisions"))))
        divs.append(d)
    for mi, onsets in by_measure.items():
        if mi < len(measures):
            for on in onsets:
                k = lcm(k, on.denominator // gcd(on.denominator, divs[mi]))
    if k > 1:
        _rescale(part, k)
        divs = [x * k for x in divs]
    voice = str(CARRIER_VOICE)
    for mi, onsets in sorted(by_measure.items()):
        if mi >= len(measures):
            continue
        measure, div = measures[mi], divs[mi]
        # Where the measure's content ends (insertion point: before the closing barlines).
        children = list(measure)
        insert_at = next((i for i, c in enumerate(children) if c.tag == "barline" and c.get("location", "right") == "right"
                          and all(x.tag == "barline" for x in children[i:])), len(children))
        pos = end = Fraction(0)
        starts: dict[Fraction, ET.Element] = {}
        for el in children[:insert_at]:
            if el.tag in ("backup", "forward", "note"):
                dur = Fraction(int(float(el.findtext("duration") or 0)), div)
                if el.tag == "backup":
                    pos = max(Fraction(0), pos - dur)
                elif el.tag == "forward":
                    pos += dur
                elif el.find("chord") is None and el.find("grace") is None:
                    if el.find("rest") is None and (el.findtext("staff") or "1").strip() == staff:
                        starts.setdefault(pos, el)
                    pos += dur
                end = max(end, pos)
        times = sorted(on for on in onsets if on < end)
        if not times:
            continue
        block: list[ET.Element] = []
        if pos > 0:
            backup = ET.Element("backup")
            ET.SubElement(backup, "duration").text = str(int(pos * div))
            block.append(backup)
        if times[0] > 0:
            fwd = ET.Element("forward")
            ET.SubElement(fwd, "duration").text = str(int(times[0] * div))
            ET.SubElement(fwd, "voice").text = voice
            if multi:
                ET.SubElement(fwd, "staff").text = staff
            block.append(fwd)
        for i, on in enumerate(times):
            length = (times[i + 1] if i + 1 < len(times) else end) - on
            note = ET.Element("note", {"print-object": "no"})
            pitch = ET.SubElement(note, "pitch")
            same = starts.get(on)
            if same is not None and same.find("pitch") is not None:  # a unison never pushes a visible notehead aside
                for tag in ("step", "alter", "octave"):
                    if same.find(f"pitch/{tag}") is not None:
                        ET.SubElement(pitch, tag).text = same.findtext(f"pitch/{tag}")
            else:
                ET.SubElement(pitch, "step").text = _STEPS[middle % 7]
                ET.SubElement(pitch, "octave").text = str(middle // 7)
            ET.SubElement(note, "duration").text = str(int(length * div))
            ET.SubElement(note, "voice").text = voice
            value = _value(length)
            if value:
                ET.SubElement(note, "type").text = value[0]
                for _ in range(value[1]):
                    ET.SubElement(note, "dot")
            if multi:
                ET.SubElement(note, "staff").text = staff
            for _, ly in sorted(onsets[on].items(), key=lambda x: _verse_order(x[0])):
                note.append(copy.deepcopy(ly))
            block.append(note)
        for j, el in enumerate(block):
            measure.insert(insert_at + j, el)
    _set_stems(part, staff, middle)


def _verse_order(verse: str):
    return (0, int(verse)) if verse.isdigit() else (1, verse)


def place_lyrics(stored: ET.Element, drawn: ET.Element) -> list[dict]:
    """Draws the lyric lines of every stored staff that is not drawn as stored.

    - A split staff (some or all of its voices on staves of their own): on the top drawn one.
    - A staff whose voices are all hidden: on the nearest drawn staff (below, else above), for
      every verse that staff has no line of its own (a line with under half the syllables is OCR
      noise and is replaced), so any voice left on the page shows the words.

    The syllables go on the target staff's notes when every one of them has a note starting there
    in one voice; otherwise the line gets an invisible voice of its own (`_add_carrier`), so no
    syllable is lost where the rhythms differ. Returns where each line moved to another staff
    came from, for lyric edits: [{part, staff, verse (drawn), from_part, from_staff}]."""
    order, lines = _lyric_lines(stored)
    drawn_order, drawn_lines = _lyric_lines(drawn)
    origins = {d: _origin(*d) for d in drawn_order}
    index = {k: i for i, k in enumerate(order)}
    plan: dict[tuple, dict[str, dict]] = {}
    moved = []
    for src in order:
        if not lines[src]:
            continue
        mine = [d for d in drawn_order if origins[d] == src]
        if src in mine:
            continue  # drawn as stored, lyrics included
        if mine:
            target, foreign = mine[0], False
        else:
            below = [d for d in drawn_order if index.get(origins[d], -1) > index[src]]
            above = [d for d in drawn_order if -1 < index.get(origins[d], -1) < index[src]]
            target, foreign = (below[0] if below else (above[-1] if above else None)), True
            if target is None:
                continue
        for verse, syllables in lines[src].items():
            if verse in plan.get(target, {}):
                continue  # already gets this verse from a staff above
            if foreign:
                own = drawn_lines[target].get(verse)
                if own and len(own) >= len(syllables) / 2:
                    continue  # the staff has its own words for this verse
                if own:  # a much shorter line: OCR noise (credits read as lyrics), replaced
                    for n in _iter_notes(drawn):
                        if (n.part, n.staff) == target:
                            for ly in n.el.findall("lyric"):
                                if ly.get("number", "1") == verse:
                                    n.el.remove(ly)
                moved.append({"part": target[0], "staff": target[1], "verse": verse, "from_part": src[0], "from_staff": src[1]})
            plan.setdefault(target, {})[verse] = syllables
    if not plan:
        return moved
    groups = _group(drawn)
    parts = {p.get("id"): p for p in drawn.findall("part")}
    for target, verses in plan.items():
        hosts = {key: _host(groups.get((*target, *key), [])) for syl in verses.values() for key in syl}
        if all(hosts.values()) and len({h.voice for h in hosts.values()}) == 1:
            for syllables in verses.values():
                for key, ly in syllables.items():
                    _insert_lyric(hosts[key].el, copy.deepcopy(ly))
        else:
            _add_carrier(parts[target[0]], target[1], verses)
    return moved


def draw(data: bytes, layout: str = "parts", names: list[str] | None = None, hidden: set[int] = frozenset()) -> tuple[bytes, list[dict]]:
    """The MusicXML to draw: `layout` ("parts" as stored, "combined", "separate") without the
    `hidden` voices (analysis indexes), and where lyric lines moved off hidden staves came from
    (`place_lyrics`)."""
    moved: list[dict] = []
    if hidden or layout == "separate":
        data, moved = _separate_and_place(data, names, hidden, split_all=layout == "separate")
    return (combine_staves(data) if layout == "combined" else data), moved
