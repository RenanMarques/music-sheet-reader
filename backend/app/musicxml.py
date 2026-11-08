"""MusicXML parsing and voice separation.

Turns a (possibly OMR-generated, imperfect) MusicXML score into a playback model:
a list of monophonic voices, each a list of timed MIDI notes.

OMR output does not encode voices reliably (e.g. Audiveris often writes soprano and
alto of a hymn as chords in a single voice), so the MusicXML <voice> numbers are
ignored. Instead, notes are grouped by (part, staff), the number of simultaneous
voices on each staff is estimated, and notes are assigned to voices by pitch order
and proximity (see `separate_voices`).

Times are in quarter notes (Fraction while parsing, float in the output).
"""

from __future__ import annotations

import io
import itertools
import zipfile
from dataclasses import dataclass, field, replace
from fractions import Fraction
from statistics import mean
from xml.etree import ElementTree as ET

STEP_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
DEFAULT_TEMPO = 90.0
MAX_VOICES_PER_STAFF = 4
GENERIC_PART_NAMES = {"", "voice", "piano", "music", "part", "staff"}
NOTE_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


class MusicXMLError(ValueError):
    pass


@dataclass
class NoteEvent:
    onset: Fraction  # quarters, score time (repeats not expanded)
    duration: Fraction
    midi: int
    part: str
    staff: str
    measure: int  # 0-based index of the measure in the part
    tie_start: bool = False
    tie_stop: bool = False
    is_rest: bool = False

    @property
    def end(self) -> Fraction:
        return self.onset + self.duration


@dataclass
class MeasureInfo:
    number: str
    start: Fraction = Fraction(0)
    length: Fraction = Fraction(0)
    forward_repeat: bool = False
    backward_repeat: bool = False
    repeat_times: int = 2
    ending_numbers: set[int] = field(default_factory=set)
    ending_start: bool = False
    ending_stop: bool = False


# --------------------------------------------------------------------------- loading


def read_musicxml_bytes(data: bytes) -> bytes:
    """Return plain MusicXML bytes, unpacking a compressed .mxl if needed."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root_path = None
            if "META-INF/container.xml" in z.namelist():
                container = ET.fromstring(z.read("META-INF/container.xml"))
                rootfile = container.find(".//{*}rootfile")
                if rootfile is not None:
                    root_path = rootfile.get("full-path")
            if root_path is None:
                candidates = [
                    n for n in z.namelist() if n.endswith((".xml", ".musicxml")) and not n.startswith("META-INF")
                ]
                if not candidates:
                    raise MusicXMLError("Arquivo .mxl sem MusicXML dentro")
                root_path = candidates[0]
            return z.read(root_path)
    return data


def _parse_root(data: bytes) -> ET.Element:
    try:
        root = ET.fromstring(read_musicxml_bytes(data))
    except ET.ParseError as e:
        raise MusicXMLError(f"MusicXML inválido: {e}") from e
    if root.tag == "score-timewise":
        raise MusicXMLError("MusicXML 'timewise' não é suportado; exporte como 'partwise'")
    if root.tag != "score-partwise":
        raise MusicXMLError(f"Elemento raiz inesperado: {root.tag}")
    return root


# --------------------------------------------------------------------------- parsing


def _midi(pitch: ET.Element) -> int:
    step = pitch.findtext("step", "C").strip().upper()
    octave = int(pitch.findtext("octave", "4"))
    alter = round(float(pitch.findtext("alter", "0") or 0))
    return 12 * (octave + 1) + STEP_SEMITONES.get(step, 0) + alter


def _parse_part(part: ET.Element):
    """Parse one part. Returns (events with measure-local onsets, per-measure content length,
    per-measure metadata collected from this part)."""
    divisions = 1
    events: list[NoteEvent] = []
    lengths: list[Fraction] = []
    metas: list[MeasureInfo] = []
    active_ending: set[int] = set()

    for mi, measure in enumerate(part.findall("measure")):
        meta = MeasureInfo(number=measure.get("number", str(mi + 1)))
        if active_ending:
            meta.ending_numbers = set(active_ending)
        pos = Fraction(0)
        max_pos = Fraction(0)
        last_onset = Fraction(0)

        for el in measure:
            tag = el.tag
            if tag == "attributes":
                d = el.findtext("divisions")
                if d:
                    divisions = max(1, int(float(d)))
            elif tag == "backup":
                pos -= Fraction(int(float(el.findtext("duration", "0"))), divisions)
                pos = max(pos, Fraction(0))
            elif tag == "forward":
                pos += Fraction(int(float(el.findtext("duration", "0"))), divisions)
                max_pos = max(max_pos, pos)
            elif tag == "barline":
                repeat = el.find("repeat")
                if repeat is not None:
                    if repeat.get("direction") == "forward":
                        meta.forward_repeat = True
                    elif repeat.get("direction") == "backward":
                        meta.backward_repeat = True
                        times = repeat.get("times")
                        if times and times.isdigit():
                            meta.repeat_times = max(2, int(times))
                ending = el.find("ending")
                if ending is not None:
                    nums = {int(n) for n in (ending.get("number") or "").replace(" ", "").split(",") if n.isdigit()}
                    if ending.get("type") == "start":
                        active_ending = nums or {1}
                        meta.ending_numbers = set(active_ending)
                        meta.ending_start = True
                    elif ending.get("type") in ("stop", "discontinue"):
                        meta.ending_numbers = meta.ending_numbers or nums
                        meta.ending_stop = True
                        active_ending = set()
            elif tag == "note":
                if el.find("grace") is not None or el.find("cue") is not None:
                    continue
                dur_text = el.findtext("duration")
                if dur_text is None:
                    continue
                duration = Fraction(int(float(dur_text)), divisions)
                if el.find("chord") is not None:
                    onset = last_onset
                else:
                    onset = pos
                    last_onset = pos
                    pos += duration
                    max_pos = max(max_pos, pos)
                pitch = el.find("pitch")
                staff = el.findtext("staff", "1").strip() or "1"
                if duration <= 0:
                    continue
                if el.find("rest") is not None:
                    events.append(NoteEvent(onset, duration, -1, part.get("id", "P?"), staff, mi, is_rest=True))
                    continue
                if pitch is None:
                    continue
                ties = [t.get("type") for t in el.findall("tie")] + [
                    t.get("type") for t in el.findall("notations/tied")
                ]
                events.append(
                    NoteEvent(
                        onset=onset,
                        duration=duration,
                        midi=_midi(pitch),
                        part=part.get("id", "P?"),
                        staff=staff,
                        measure=mi,
                        tie_start="start" in ties,
                        tie_stop="stop" in ties,
                    )
                )
        lengths.append(max_pos)
        metas.append(meta)
    return events, lengths, metas


def _first_text(root: ET.Element, paths: list[str]) -> str | None:
    for p in paths:
        for el in root.iterfind(p):
            if el.text and el.text.strip():
                return el.text.strip()
    return None


def _merge_ties(events: list[NoteEvent]) -> list[NoteEvent]:
    events = sorted(events, key=lambda e: (e.onset, -e.midi))
    open_ties: dict[tuple, NoteEvent] = {}
    out: list[NoteEvent] = []
    for e in events:
        key = (e.part, e.staff, e.midi)
        prev = open_ties.get(key)
        if e.tie_stop and prev is not None and abs(prev.end - e.onset) <= Fraction(1, 64):
            prev.duration = e.end - prev.onset
            if not e.tie_start:
                prev.tie_start = False
                del open_ties[key]
            continue
        out.append(e)
        if e.tie_start:
            open_ties[key] = e
        else:
            open_ties.pop(key, None)
    return out


def expand_repeats(metas: list[MeasureInfo]) -> list[int]:
    """Return the playback order of measure indices, following repeat barlines and
    volta endings (D.C./D.S./Fine are not handled)."""
    n = len(metas)
    order: list[int] = []
    i = 0
    section_start = 0
    pass_no = 1
    repeat_counts: dict[int, int] = {}
    jumped_to: int | None = None
    prev_in_ending = False
    limit = n * 10 + 10

    while i < n and len(order) < limit:
        m = metas[i]
        if m.forward_repeat and jumped_to != i:
            section_start = i
            pass_no = 1
        jumped_to = None
        in_ending = bool(m.ending_numbers)
        if prev_in_ending and not in_ending:
            section_start = i
            pass_no = 1
        prev_in_ending = in_ending
        if in_ending and pass_no not in m.ending_numbers:
            i += 1
            continue
        order.append(i)
        if m.backward_repeat:
            done = repeat_counts.get(i, 1)
            if done < m.repeat_times:
                repeat_counts[i] = done + 1
                pass_no += 1
                i = section_start
                jumped_to = i
                prev_in_ending = False
                continue
            repeat_counts[i] = 1
            if not in_ending:
                section_start = i + 1
                pass_no = 1
        i += 1
    return order


# --------------------------------------------------------------------------- voices


def estimate_voice_count(events: list[NoteEvent]) -> int:
    """Number of simultaneous voices on a staff: the largest polyphony that occurs at a
    meaningful share (>= 20%) of the note onsets. Robust to occasional OMR noise."""
    if not events:
        return 0
    onsets = sorted({e.onset for e in events})
    counts = [sum(1 for e in events if e.onset <= t < e.end) for t in onsets]
    best = 1
    for c in range(2, MAX_VOICES_PER_STAFF + 1):
        if sum(1 for x in counts if x >= c) / len(counts) >= 0.2:
            best = c
    return best


def _best_monotone_map(
    pitches: list[int], voices: list[int], last_pitch: dict[int, float], allow_double: bool = True
) -> dict[int, list[int]]:
    """Assign notes (pitches sorted high->low) to free voices (sorted high->low) keeping
    order and minimising distance to each voice's previous pitch.

    - len(pitches) <= len(voices): every note gets >= 1 voice; extra voices double a note (unison).
    - len(pitches) >  len(voices): every voice gets exactly one note; extra notes are dropped.
    - allow_double=False: each note gets exactly one voice; the other free voices rest.
    Returns {note_index: [voice, ...]}.
    """
    n, k = len(pitches), len(voices)
    best_cost, best_assign = None, None
    if n <= k and not allow_double:
        for chosen in itertools.combinations(voices, n):
            assign = {j: [v] for j, v in enumerate(chosen)}
            cost = sum(abs(pitches[j] - last_pitch[vs[0]]) for j, vs in assign.items())
            if best_cost is None or cost < best_cost:
                best_cost, best_assign = cost, assign
    elif n <= k:
        # Surjective monotone maps: choose n-1 cut points among k-1 gaps.
        for cuts in itertools.combinations(range(1, k), n - 1):
            bounds = (0, *cuts, k)
            assign = {j: voices[bounds[j] : bounds[j + 1]] for j in range(n)}
            cost = sum(abs(pitches[j] - last_pitch[v]) for j, vs in assign.items() for v in vs)
            if best_cost is None or cost < best_cost:
                best_cost, best_assign = cost, assign
    else:
        for chosen in itertools.combinations(range(n), k):
            assign = {j: [voices[x]] for x, j in enumerate(chosen)}
            cost = sum(abs(pitches[j] - last_pitch[vs[0]]) for j, vs in assign.items())
            if best_cost is None or cost < best_cost:
                best_cost, best_assign = cost, assign
    return best_assign or {}


def separate_voices(events: list[NoteEvent], k: int, rests: list[NoteEvent] = ()) -> list[list[NoteEvent]]:
    """Split the notes of one staff into k monophonic voices (index 0 = highest).

    When fewer notes than voices sound, the note is doubled (a notehead shared by two
    voices, typical in hymns) unless an explicit rest on the staff says a voice is silent.
    """
    if k <= 1:
        return [sorted(events, key=lambda e: (e.onset, -e.midi))]
    all_pitches = sorted(e.midi for e in events)
    last_pitch: dict[int, float] = {}
    for v in range(k):
        q = 1 - (v + 0.5) / k  # voice 0 -> high quantile
        last_pitch[v] = all_pitches[min(len(all_pitches) - 1, int(q * len(all_pitches)))]
    busy_until = {v: Fraction(-1) for v in range(k)}
    doubled = {v: False for v in range(k)}  # voice is singing a unison copy of another voice's note
    voices: list[list[NoteEvent]] = [[] for _ in range(k)]

    by_onset: dict[Fraction, list[NoteEvent]] = {}
    for e in events:
        by_onset.setdefault(e.onset, []).append(e)

    def cut(v: int, t: Fraction) -> None:
        prev = voices[v][-1]
        prev.duration = max(Fraction(1, 64), t - prev.onset)
        busy_until[v] = t
        doubled[v] = False

    for t in sorted(by_onset):
        new = sorted(by_onset[t], key=lambda e: -e.midi)
        free = [v for v in range(k) if busy_until[v] <= t]
        # A voice doubling a sustained unison leaves it when a new note needs a voice
        # (e.g. soprano holds C5 while the alto, which shared that notehead, moves on).
        while len(new) > len(free):
            shared = [v for v in range(k) if v not in free and doubled[v]]
            if not shared:
                break
            target = new[len(free)] if len(free) < len(new) else new[-1]
            v = min(shared, key=lambda v: (abs(last_pitch[v] - target.midi), -v if target.midi <= last_pitch[v] else v))
            cut(v, t)
            free = sorted(free + [v])
        if not free:
            # Every voice is sustaining: the OMR probably made a note too long; cut the closest.
            for e in new:
                cut(min(range(k), key=lambda v: abs(last_pitch[v] - e.midi)), t)
            free = list(range(k))
        resting = any(r.onset <= t < r.end for r in rests)
        assign = _best_monotone_map([e.midi for e in new], free, last_pitch, allow_double=not resting)
        for j, vs in assign.items():
            for i, v in enumerate(vs):
                e = new[j] if i == 0 else replace(new[j])
                voices[v].append(e)
                busy_until[v] = e.end
                last_pitch[v] = e.midi
                doubled[v] = len(vs) > 1
    return voices


# --------------------------------------------------------------------------- public API


def _voice_names(groups: list[dict], total: int) -> list[str]:
    if total == 4:
        return ["Soprano", "Contralto", "Tenor", "Baixo"]
    if total == 1:
        return ["Melodia"]
    names = []
    part_names = [g["part_name"] for g in groups]
    for g in groups:
        distinct = part_names.count(g["part_name"]) == 1 and g["part_name"].lower() not in GENERIC_PART_NAMES
        for vi in range(g["count"]):
            if distinct and g["count"] == 1:
                names.append(g["part_name"])
            else:
                names.append(f"Voz {len(names) + 1}")
    return names


def note_name(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def analyze(data: bytes, title_fallback: str = "Partitura") -> dict:
    """Parse MusicXML bytes into the playback model consumed by the frontend."""
    root = _parse_root(data)
    parts = root.findall("part")
    if not parts:
        raise MusicXMLError("Partitura sem partes")

    part_names = {
        sp.get("id"): (sp.findtext("part-name") or "").strip() for sp in root.iterfind("part-list/score-part")
    }

    parsed = [_parse_part(p) for p in parts]
    n_measures = max(len(lengths) for _, lengths, _ in parsed)

    # Measure lengths are aligned across parts (max content length), so a recognition error
    # in one part does not shift the others.
    first_time = root.find(".//attributes/time")
    beats = int(first_time.findtext("beats", "4").split("+")[0]) if first_time is not None else 4
    beat_type = int(first_time.findtext("beat-type", "4")) if first_time is not None else 4
    nominal = Fraction(beats * 4, beat_type)

    metas = parsed[0][2] + [MeasureInfo(number=str(i + 1)) for i in range(len(parsed[0][2]), n_measures)]
    start = Fraction(0)
    for i in range(n_measures):
        length = max((lengths[i] for _, lengths, _ in parsed if i < len(lengths)), default=Fraction(0))
        metas[i].start = start
        metas[i].length = length if length > 0 else nominal
        start += metas[i].length

    events: list[NoteEvent] = []
    for evs, _, _ in parsed:
        for e in evs:
            e.onset += metas[e.measure].start
            events.append(e)
    rests = [e for e in events if e.is_rest]
    events = _merge_ties([e for e in events if not e.is_rest])

    # Group by (part, staff) in score order.
    part_order = {p.get("id"): i for i, p in enumerate(parts)}
    keys = sorted({(e.part, e.staff) for e in events}, key=lambda k: (part_order.get(k[0], 99), int(k[1]) if k[1].isdigit() else 99))
    groups = []
    for part_id, staff in keys:
        evs = [e for e in events if e.part == part_id and e.staff == staff]
        staff_rests = [r for r in rests if r.part == part_id and r.staff == staff]
        k = estimate_voice_count(evs)
        groups.append(
            {
                "part": part_id,
                "part_name": part_names.get(part_id, part_id),
                "staff": staff,
                "count": k,
                "voices": separate_voices(evs, k, staff_rests),
            }
        )
    total = sum(g["count"] for g in groups)
    names = _voice_names(groups, total)

    # Expand repeats into a performance timeline.
    order = expand_repeats(metas)
    timeline = []
    perf = Fraction(0)
    for idx in order:
        timeline.append(
            {"measure_index": idx, "number": metas[idx].number, "start": float(perf), "length": float(metas[idx].length)}
        )
        perf += metas[idx].length

    voices_out = []
    vi = 0
    for g in groups:
        for line in g["voices"]:
            by_measure: dict[int, list[NoteEvent]] = {}
            for e in line:
                by_measure.setdefault(e.measure, []).append(e)
            notes = []
            for seg in timeline:
                idx = seg["measure_index"]
                offset = Fraction(seg["start"]) - metas[idx].start
                for e in by_measure.get(idx, []):
                    notes.append(
                        {
                            "t": round(float(e.onset + offset), 6),
                            "d": round(float(e.duration), 6),
                            "midi": e.midi,
                            "m": idx,
                        }
                    )
            pitches = [n["midi"] for n in notes] or [60]
            voices_out.append(
                {
                    "id": f"v{vi + 1}",
                    "name": names[vi],
                    "part": g["part"],
                    "part_name": g["part_name"],
                    "staff": g["staff"],
                    "low": note_name(min(pitches)),
                    "high": note_name(max(pitches)),
                    "mean_midi": round(mean(pitches), 2),
                    "notes": notes,
                }
            )
            vi += 1

    tempo = None
    for sound in root.iter("sound"):
        if sound.get("tempo"):
            tempo = float(sound.get("tempo"))
            break
    if tempo is None:
        pm = root.find(".//metronome/per-minute")
        if pm is not None and pm.text:
            try:
                tempo = float(pm.text)
            except ValueError:
                pass

    fifths = root.findtext(".//attributes/key/fifths")
    title = _first_text(root, ["work/work-title", "movement-title"]) or title_fallback

    return {
        "title": title,
        "tempo": tempo or DEFAULT_TEMPO,
        "time_signature": f"{beats}/{beat_type}",
        "key_fifths": int(fifths) if fifths and fifths.lstrip("-").isdigit() else 0,
        "measures": n_measures,
        "duration": float(perf),
        "staves": [
            {"part": g["part"], "part_name": g["part_name"], "staff": g["staff"], "voices": g["count"]} for g in groups
        ],
        "timeline": timeline,
        "voices": voices_out,
    }
