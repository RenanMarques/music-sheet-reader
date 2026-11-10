"""Voice identification: which voice (Soprano, Contralto, Tenor, ...) each part or separated line
of a score is.

Signals, strongest first: the user's choice; the voice printed on the sheet (". SOPRANO ."
header line); and a comparison of the parts' pitches with each other. The comparison sorts the
parts from highest to lowest median pitch and assigns voice types monotonically (a higher part
never gets a lower voice than the next one), minimising the distance to each type's usual median
pitch. Any number of parts works: two parts close together become "Soprano 1" / "Soprano 2".
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class VoiceType:
    name: str
    abbr: str
    center: float  # typical median MIDI pitch of the voice in choral writing
    low: bool  # usually written in bass clef (or treble 8vb)
    extra: float = 0.0  # cost of choosing this less common type


VOICE_TYPES = [
    VoiceType("Soprano", "S", 69, False),
    VoiceType("Mezzo-soprano", "Mz", 66, False, extra=3),
    VoiceType("Contralto", "C", 64, False),
    VoiceType("Tenor", "T", 57, True),
    VoiceType("Barítono", "Br", 53, True, extra=3),
    VoiceType("Baixo", "B", 50, True),
]
_ALIASES = {
    "soprano": "Soprano", "sopranos": "Soprano", "soprani": "Soprano",
    "mezzo": "Mezzo-soprano", "mezzosoprano": "Mezzo-soprano", "mezzo-soprano": "Mezzo-soprano",
    "contralto": "Contralto", "contraltos": "Contralto", "alto": "Contralto", "altos": "Contralto",
    "tenor": "Tenor", "tenores": "Tenor", "tenors": "Tenor",
    "barítono": "Barítono", "baritono": "Barítono", "baritone": "Barítono", "barítonos": "Barítono",
    "baixo": "Baixo", "baixos": "Baixo", "bass": "Baixo", "basses": "Baixo", "basso": "Baixo",
}
_BY_NAME = {v.name: v for v in VOICE_TYPES}
_FILLER = {"voz", "vozes", "naipe", "voice", "part", "parte", "de", "do", "da"}

# A second part of the same type costs this much: prefer distinct types unless the pitches say so.
_DUPLICATE = 6.0
# A treble-clef part as a low voice or a bass-clef part as a high one (OMR does miss the "8" of a
# tenor's treble 8vb clef, hence the smaller cost for Tenor).
_CLEF_MISMATCH = 4.0
_TENOR_IN_TREBLE = 1.5


def find_type(name: str | None) -> VoiceType | None:
    """The voice type a name refers to ("Soprano", "soprano 2", "Alto", "Bass") or None."""
    if not name:
        return None
    words = [w for w in re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)?", name.casefold()) if w not in _FILLER]
    if len(words) == 1 and words[0] in _ALIASES:
        return _BY_NAME[_ALIASES[words[0]]]
    if words == ["mezzo", "soprano"]:
        return _BY_NAME["Mezzo-soprano"]
    return None


def voice_from_lines(lines: list[str], header: int = 10) -> VoiceType | None:
    """The voice named by a header line of the sheet (". SOPRANO .", "Voz: Tenor")."""
    for line in lines[:header]:
        voice = find_type(line)
        if voice is not None:
            return voice
    return None


def clef_is_low(sign: str, line: int, octave_change: int) -> bool | None:
    """Whether a clef is written for a low voice (bass clef, treble 8vb, tenor clef)."""
    if sign == "F" or (sign == "G" and octave_change < 0) or (sign == "C" and line >= 4):
        return True
    if sign == "G":
        return False
    return None


@dataclass
class Line:
    """A part (or a separated voice line) to identify."""

    median: float
    low_clef: bool | None = None  # bass clef / treble 8vb; None when unknown
    fixed: VoiceType | None = None  # already known (user's choice or printed on the sheet)


def _cost(line: Line, voice: VoiceType) -> float:
    cost = abs(line.median - voice.center) + voice.extra
    if line.low_clef is not None and line.low_clef != voice.low:
        cost += _TENOR_IN_TREBLE if voice.name == "Tenor" and not line.low_clef else _CLEF_MISMATCH
    return cost


def _monotone(lines: list[Line], idx: list[int], own) -> tuple[float, list[int]]:
    """Cheapest monotone assignment of types to lines `idx` (highest first): returns (cost, type
    index per line). A type repeated on consecutive lines costs _DUPLICATE more."""
    n_types = len(VOICE_TYPES)
    inf = float("inf")
    best = [[inf] * n_types for _ in idx]
    back = [[-1] * n_types for _ in idx]
    for k, i in enumerate(idx):
        for t in range(n_types):
            c_own = own(i, t)
            if c_own == inf:
                continue
            if k == 0:
                best[k][t] = c_own
                continue
            for p in range(t + 1):  # the previous (higher) line has a higher or equal type
                c = best[k - 1][p] + c_own + (_DUPLICATE if p == t else 0)
                if c < best[k][t]:
                    best[k][t], back[k][t] = c, p
    t = min(range(n_types), key=lambda x: best[-1][x])
    cost, path = best[-1][t], []
    for k in range(len(idx) - 1, -1, -1):
        path.append(t)
        t = back[k][t]
    return cost, path[::-1]


def assign(lines: list[Line]) -> list[VoiceType]:
    """Voice type for each line: fixed ones keep theirs, the others are assigned by comparing
    pitches, in order with the fixed ones (a line above a known Soprano is a Soprano too)."""
    if not lines:
        return []
    by_pitch = sorted(range(len(lines)), key=lambda i: -lines[i].median)

    def own(i: int, t: int) -> float:
        fixed = lines[i].fixed
        if fixed is not None:
            return 0.0 if VOICE_TYPES[t] == fixed else float("inf")
        return _cost(lines[i], VOICE_TYPES[t])

    cost, path = _monotone(lines, by_pitch, own)
    if cost < float("inf"):
        result: list[VoiceType] = [VOICE_TYPES[0]] * len(lines)
        for i, t in zip(by_pitch, path):
            result[i] = VOICE_TYPES[t]
        return result

    # The known voices contradict their pitches (e.g. a "Baixo" above the "Tenor"): keep them and
    # order only the others, avoiding the types already taken.
    taken = {ln.fixed.name for ln in lines if ln.fixed}
    free = [i for i in by_pitch if lines[i].fixed is None]
    result = [ln.fixed for ln in lines]  # type: ignore[misc]
    if free:
        _, path = _monotone(
            lines, free, lambda i, t: _cost(lines[i], VOICE_TYPES[t]) + (_DUPLICATE if VOICE_TYPES[t].name in taken else 0)
        )
        for i, t in zip(free, path):
            result[i] = VOICE_TYPES[t]
    return result  # type: ignore[return-value]


def numbered(names: list[str], medians: list[float]) -> list[str]:
    """Repeated names get numbers from the highest line down: Soprano 1, Soprano 2."""
    out = list(names)
    for name in set(names):
        idx = [i for i, n in enumerate(names) if n == name]
        if len(idx) > 1:
            for k, i in enumerate(sorted(idx, key=lambda i: -medians[i]), 1):
                out[i] = f"{name} {k}"
    return out
