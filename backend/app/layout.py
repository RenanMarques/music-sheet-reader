"""Alternative drawing layout: neighbouring single-voice parts that use the same clef
(Soprano + Contralto, Tenor + Baixo) combined on one staff, hymnal style.

Only the drawing changes: playback still comes from the analysis of the stored score.xml, which
keeps one part per voice. The combined staff keeps the id of its top part and only the top
voice's lyrics, so a lyric edit made on it (part, staff 1, measure, offset) addresses the top
voice's notes in score.xml unchanged.
"""

from __future__ import annotations

import copy
from fractions import Fraction
from math import lcm
from xml.etree import ElementTree as ET

from .musicxml import _parse_root

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
