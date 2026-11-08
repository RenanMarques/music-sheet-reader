import io
import zipfile
from pathlib import Path

import pytest

from app.musicxml import MusicXMLError, analyze, read_musicxml_bytes

FIXTURES = Path(__file__).parent / "fixtures"


def score(*parts: str, divisions: int = 1, time: str = "4/4") -> bytes:
    beats, beat_type = time.split("/")
    part_list = "".join(f'<score-part id="P{i + 1}"><part-name>Voice</part-name></score-part>' for i in range(len(parts)))
    body = ""
    for i, measures in enumerate(parts):
        attrs = (
            f"<attributes><divisions>{divisions}</divisions><key><fifths>0</fifths></key>"
            f"<time><beats>{beats}</beats><beat-type>{beat_type}</beat-type></time></attributes>"
        )
        body += f'<part id="P{i + 1}">' + measures.replace("<!--attrs-->", attrs, 1) + "</part>"
    return f'<?xml version="1.0"?><score-partwise version="4.0"><part-list>{part_list}</part-list>{body}</score-partwise>'.encode()


def n(step: str, octave: int, dur: int = 1, chord: bool = False, voice: int = 1, staff: int | None = None,
      alter: int = 0, tie: str | None = None) -> str:
    return (
        "<note>" + ("<chord/>" if chord else "")
        + f"<pitch><step>{step}</step>" + (f"<alter>{alter}</alter>" if alter else "") + f"<octave>{octave}</octave></pitch>"
        + f"<duration>{dur}</duration>" + (f'<tie type="{tie}"/>' if tie else "")
        + f"<voice>{voice}</voice>" + (f"<staff>{staff}</staff>" if staff else "") + "</note>"
    )


def rest(dur: int = 1, voice: int = 1) -> str:
    return f"<note><rest/><duration>{dur}</duration><voice>{voice}</voice></note>"


def m(num: int, content: str, barline: str = "") -> str:
    attrs = "<!--attrs-->" if num == 1 else ""
    return f'<measure number="{num}">{attrs}{content}{barline}</measure>'


def line(analysis: dict, name: str) -> list[tuple[float, float, int]]:
    voice = next(v for v in analysis["voices"] if v["name"] == name)
    return [(x["t"], x["d"], x["midi"]) for x in voice["notes"]]


def test_satb_chords_are_split_by_pitch():
    treble = m(1, n("E", 5) + n("C", 5, chord=True) + n("D", 5) + n("B", 4, chord=True) + n("C", 5, 2) + n("A", 4, 2, chord=True))
    bass = m(1, n("G", 3) + n("C", 3, chord=True) + n("G", 3) + n("G", 2, chord=True) + n("E", 3, 2) + n("A", 2, 2, chord=True))
    a = analyze(score(treble, bass))
    assert [v["name"] for v in a["voices"]] == ["Soprano", "Contralto", "Tenor", "Baixo"]
    assert line(a, "Soprano") == [(0, 1, 76), (1, 1, 74), (2, 2, 72)]
    assert line(a, "Contralto") == [(0, 1, 72), (1, 1, 71), (2, 2, 69)]
    assert line(a, "Baixo") == [(0, 1, 48), (1, 1, 43), (2, 2, 45)]


def test_shared_notehead_is_doubled_and_sustained_note_keeps_its_voice():
    # Beat 1: unison C5 (one head for both voices). Beats 2-3: alto holds A4 (half) under soprano C5, D5.
    treble = m(
        1,
        n("C", 5) + n("C", 5) + n("D", 5) + n("C", 5)
        + "<backup><duration>4</duration></backup>"
        + n("E", 4, voice=2) + n("A", 4, 2, voice=2) + n("G", 4, voice=2),
    )
    a = analyze(score(treble))
    assert len(a["voices"]) == 2
    top, low = a["voices"]
    assert [(x["t"], x["midi"]) for x in top["notes"]] == [(0, 72), (1, 72), (2, 74), (3, 72)]
    assert [(x["t"], x["d"], x["midi"]) for x in low["notes"]] == [(0, 1, 64), (1, 2, 69), (3, 1, 67)]

    unison = m(1, n("C", 5) + n("A", 4, chord=True) + n("F", 4, 2) + n("B", 4) + n("G", 4, chord=True))
    top, low = analyze(score(unison))["voices"]
    assert [x["midi"] for x in top["notes"]] == [69 + 3, 65, 71]
    assert [x["midi"] for x in low["notes"]] == [69, 65, 67]


def test_single_line_is_one_voice_named_melody():
    a = analyze(score(m(1, n("C", 4) + n("D", 4) + n("E", 4, alter=-1) + rest())))
    assert [v["name"] for v in a["voices"]] == ["Melodia"]
    assert line(a, "Melodia") == [(0, 1, 60), (1, 1, 62), (2, 1, 63)]
    assert a["duration"] == 4


def test_ties_are_merged_across_barlines():
    a = analyze(score(m(1, n("C", 4, 2) + n("G", 4, 2, tie="start")) + m(2, n("G", 4, 2, tie="stop") + n("E", 4, 2))))
    assert line(a, "Melodia") == [(0, 2, 60), (2, 4, 67), (6, 2, 64)]


def test_divisions_backup_and_explicit_rest():
    # Soprano C5 (dotted half) D5; alto rests two beats then sings G4. The explicit rest
    # means the alto is silent, not sharing the soprano's C5.
    content = n("C", 5, 6) + n("D", 5, 2) + "<backup><duration>8</duration></backup>" + rest(4, voice=2) + n("G", 4, 4, voice=2)
    a = analyze(score(m(1, content), divisions=2))
    top, low = a["voices"]
    assert [(x["t"], x["d"]) for x in top["notes"]] == [(0, 3), (3, 1)]
    assert [(x["t"], x["d"], x["midi"]) for x in low["notes"]] == [(2, 2, 67)]


def test_forward_without_rest_keeps_unison_until_voices_split():
    content = n("C", 5, 6) + n("D", 5, 2) + "<backup><duration>8</duration></backup>" + "<forward><duration>4</duration></forward>" + n("G", 4, 4, voice=2)
    top, low = analyze(score(m(1, content), divisions=2))["voices"]
    assert [(x["t"], x["d"]) for x in top["notes"]] == [(0, 3), (3, 1)]
    assert [(x["t"], x["d"], x["midi"]) for x in low["notes"]] == [(0, 2, 72), (2, 2, 67)]


def test_grand_staff_in_one_part_is_split_per_staff():
    content = (
        n("E", 5, 4, staff=1) + n("C", 5, 4, chord=True, staff=1)
        + "<backup><duration>4</duration></backup>"
        + n("G", 3, 4, voice=5, staff=2) + n("C", 3, 4, chord=True, voice=5, staff=2)
    )
    a = analyze(score(m(1, content)))
    assert [(s["staff"], s["voices"]) for s in a["staves"]] == [("1", 2), ("2", 2)]
    assert [v["notes"][0]["midi"] for v in a["voices"]] == [76, 72, 55, 48]


def test_repeats_and_endings_are_expanded():
    fwd = '<barline location="left"><repeat direction="forward"/></barline>'
    e1 = '<barline location="left"><ending number="1" type="start"/></barline>'
    e1_end = '<barline location="right"><ending number="1" type="stop"/><repeat direction="backward"/></barline>'
    e2 = '<barline location="left"><ending number="2" type="start"/></barline>'
    e2_end = '<barline location="right"><ending number="2" type="discontinue"/></barline>'
    p = (
        m(1, n("C", 4, 4))
        + m(2, fwd + n("D", 4, 4))
        + m(3, e1 + n("E", 4, 4) + e1_end)
        + m(4, e2 + n("F", 4, 4) + e2_end)
        + m(5, n("G", 4, 4))
    )
    a = analyze(score(p))
    assert [s["number"] for s in a["timeline"]] == ["1", "2", "3", "2", "4", "5"]
    assert [x["midi"] for x in a["voices"][0]["notes"]] == [60, 62, 64, 62, 65, 67]
    assert a["duration"] == 24


def test_measures_are_aligned_across_parts_when_one_is_short():
    # OMR dropped a beat in part 1, measure 1: part 2 must still start measure 2 at beat 4.
    p1 = m(1, n("C", 5, 3)) + m(2, n("D", 5, 4))
    p2 = m(1, n("C", 3, 4)) + m(2, n("D", 3, 4))
    a = analyze(score(p1, p2))
    assert [x["t"] for x in a["voices"][0]["notes"]] == [0, 4]
    assert [x["t"] for x in a["voices"][1]["notes"]] == [0, 4]


def test_mxl_is_unpacked():
    raw = score(m(1, n("C", 4, 4)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="s.xml"/></rootfiles></container>')
        z.writestr("s.xml", raw)
    assert read_musicxml_bytes(buf.getvalue()) == raw


def test_invalid_input():
    with pytest.raises(MusicXMLError):
        analyze(b"not xml")
    with pytest.raises(MusicXMLError):
        analyze(b"<score-timewise/>")


def test_audiveris_hymn_has_satb_and_correct_bass_opening():
    a = analyze((FIXTURES / "cantam_anjos_audiveris.xml").read_bytes())
    assert [v["name"] for v in a["voices"]] == ["Soprano", "Contralto", "Tenor", "Baixo"]
    assert a["measures"] == 20 and a["key_fifths"] == -1
    bass = [x for x in line(a, "Baixo") if x[0] < 8]
    # F3 F3 F3 C3 | A2 F2 C3 C3
    assert [x[2] for x in bass] == [53, 53, 53, 48, 45, 41, 48, 48]
    means = [v["mean_midi"] for v in a["voices"]]
    assert means == sorted(means, reverse=True)
