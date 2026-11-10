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
from math import lcm
from xml.etree import ElementTree as ET

from .lyrics import _insert_lyric
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
                    _set_child(el, "voice", str(j + 1), ("duration", "footnote", "level"))
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


def _separate_part(part: ET.Element, group: dict, v: int, first: bool, staff_first: bool, metas, common: int, lyrics, owner) -> ET.Element:
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
            if owner.get((mi, on)) == v:
                for ly in lyrics.get((mi, on), {}).values():
                    _insert_lyric(note, copy.deepcopy(ly))
            placed.append((on, 1, note))
            notes.append(note)
            cursor = on + d
        _fix_beams(notes)
        placed.sort(key=lambda x: (x[0], x[1]))
        m.extend(el for _, _, el in placed)
        m.extend(right_bars)
    return out


def separate_staves(data: bytes, names: list[str] | None = None, hidden: set[int] = frozenset(), split_all: bool = True) -> bytes:
    """MusicXML with each voice of a shared staff on its own staff (unchanged if there is nothing to do).

    `names`: one per analysis voice (in analysis order), e.g. the user's names; default: detected.
    `hidden`: analysis voice indexes left out of the drawing. Parts whose voices are all hidden are
    removed; a staff where only some are hidden is split, so the others can be drawn alone.
    `split_all=False` splits only those staves (the drawing otherwise keeps the stored staves).
    Hiding every voice is ignored (an empty score cannot be drawn)."""
    root = _parse_root(data)
    sep = separate(root)
    total = sum(len(g["voices"]) for g in sep.groups)
    hidden = {i for i in hidden if 0 <= i < total}
    if len(hidden) == total:
        hidden = set()
    if not hidden and not (split_all and any(g["count"] > 1 for g in sep.groups)):
        return data
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
            # Lyrics of every note at an onset, drawn on the highest shown voice singing there.
            lyrics: dict[tuple, dict] = {}
            owner: dict[tuple, int] = {}
            for v in g["shown"]:
                for mi, items in g["plans"][v].items():
                    for el, on, d, _ in items:
                        owner.setdefault((mi, on), v)
                        fractions += [on, d]
            for line in g["voices"]:
                for e in line:
                    for el, mi, on, _ in e.src:
                        for ly in el.findall("lyric"):
                            lyrics.setdefault((mi, on), {}).setdefault(ly.get("number", "1"), ly)
            g["lyrics"], g["owner"] = lyrics, owner
        common = lcm(*divs, *[f.denominator for f in fractions])
        for g in groups:
            for v in g["shown"]:
                new_parts.append((g, v, _separate_part(part, g, v, not new_parts, v == g["shown"][0], metas, common, g["lyrics"], g["owner"])))
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
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def draw(data: bytes, layout: str = "parts", names: list[str] | None = None, hidden: set[int] = frozenset()) -> bytes:
    """The MusicXML to draw: `layout` ("parts" as stored, "combined", "separate") without the
    `hidden` voices (analysis indexes)."""
    if layout == "separate":
        return separate_staves(data, names, hidden)
    if hidden:
        data = separate_staves(data, names, hidden, split_all=False)
    return combine_staves(data) if layout == "combined" else data
