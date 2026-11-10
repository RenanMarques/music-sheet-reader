from xml.etree import ElementTree as ET

from app.merge import PartFile, merge_scores
from app.musicxml import analyze
from app.voices import find_type
from tests.test_musicxml import m, n, rest, score

DOUBLE_BAR = '<barline location="right"><bar-style>light-light</bar-style></barline>'
FINAL = '<barline location="right"><bar-style>light-heavy</bar-style></barline>'


def clef(sign: str = "G", line: int = 2, octave_change: int = 0) -> str:
    oc = f"<clef-octave-change>{octave_change}</clef-octave-change>" if octave_change else ""
    return f"<clef><sign>{sign}</sign><line>{line}</line>{oc}</clef>"


def single(*measures: str, clef: str = "") -> bytes:
    """One-part score; `clef` goes into the first measure's attributes."""
    xml = score("".join(measures))
    return xml.replace(b"</time></attributes>", f"</time>{clef}</attributes>".encode(), 1)


def part(*notes: tuple[str, int], c: str = "") -> bytes:
    """One measure of quarter notes (step, octave)."""
    return single(m(1, "".join(n(s, o) for s, o in notes)), clef=c)


SOPRANO = part(("E", 5), ("D", 5), ("C", 5), ("D", 5), c=clef())
ALTO = part(("C", 5), ("B", 4), ("A", 4), ("B", 4), c=clef())
TENOR = part(("G", 3), ("G", 3), ("A", 3), ("G", 3), c=clef("F", 4))
BASS = part(("C", 3), ("G", 2), ("F", 2), ("G", 2), c=clef("F", 4))


def merge(*xmls: bytes, voices=None, printed=None, title="Hino"):
    voices = voices or [None] * len(xmls)
    printed = printed or [None] * len(xmls)
    files = [
        PartFile(xml=x, label=f"f{i}.pdf", voice=v, printed_voice=find_type(p))
        for i, (x, v, p) in enumerate(zip(xmls, voices, printed))
    ]
    return merge_scores(files, title)


def part_names(xml: bytes) -> list[str]:
    return [sp.findtext("part-name") for sp in ET.fromstring(xml).iterfind("part-list/score-part")]


def test_voices_are_identified_by_comparing_pitches_and_ordered_high_to_low():
    r = merge(BASS, ALTO, TENOR, SOPRANO)  # any upload order
    assert part_names(r.xml) == ["Soprano", "Contralto", "Tenor", "Baixo"]
    assert [(v.file, v.how) for v in r.voices] == [(3, "alturas"), (1, "alturas"), (2, "alturas"), (0, "alturas")]
    assert r.warnings == []
    assert [v["name"] for v in analyze(r.xml)["voices"]] == ["Soprano", "Contralto", "Tenor", "Baixo"]


def test_unusual_arrangements():
    # Three treble voices, two of them close together (SSA), and a male duo (TB).
    s1 = part(("G", 5), ("F", 5), ("E", 5), ("F", 5), c=clef())
    s2 = part(("E", 5), ("D", 5), ("C", 5), ("D", 5), c=clef())
    a = part(("A", 4), ("G", 4), ("F", 4), ("G", 4), c=clef())
    assert part_names(merge(a, s2, s1).xml) == ["Soprano 1", "Soprano 2", "Contralto"]
    assert part_names(merge(BASS, TENOR).xml) == ["Tenor", "Baixo"]


def test_user_choice_and_printed_voice_win_over_pitches():
    r = merge(SOPRANO, ALTO, TENOR, BASS, voices=[None, "Mezzo-soprano", "Solista", None], printed=["SOPRANO", None, None, ". BAIXO ."])
    assert [(v.name, v.how) for v in r.voices] == [
        ("Soprano", "texto da partitura"), ("Mezzo-soprano", "escolhida"), ("Solista", "escolhida"), ("Baixo", "texto da partitura")
    ]


def test_conflicting_choice_is_warned():
    r = merge(SOPRANO, BASS, voices=["Baixo", "Soprano"])
    assert part_names(r.xml) == ["Soprano", "Baixo"]  # the user's names, highest voice type first
    assert r.warnings == ["Soprano está mais grave que Baixo: confira as vozes escolhidas."]


def test_octave_error_of_a_known_voice_is_fixed_without_changing_the_drawing():
    # Printed "SOPRANO", but the clef was read as treble 8va: every note an octave too high.
    s = part(("G", 5), ("A", 5), ("B", 5), ("C", 6), c=clef(octave_change=1))
    r = merge(s, BASS, printed=["SOPRANO", None])
    assert r.warnings == ["Soprano: notas lidas 1 oitava(s) acima; corrigido."]
    assert [x["midi"] for x in analyze(r.xml)["voices"][0]["notes"]] == [67, 69, 71, 72]
    assert ET.fromstring(r.xml).find("part/measure/attributes/clef/clef-octave-change") is None


def test_treble_8va_of_an_unknown_voice_is_fixed_before_comparing():
    s = part(("G", 5), ("A", 5), ("B", 5), ("C", 6), c=clef(octave_change=1))
    a = part(("D", 5), ("E", 5), ("D", 5), ("D", 5), c=clef(octave_change=1))
    r = merge(a, s, TENOR, BASS)
    assert part_names(r.xml) == ["Soprano", "Contralto", "Tenor", "Baixo"]
    assert r.warnings == ["Contralto: notas lidas 1 oitava(s) acima; corrigido.", "Soprano: notas lidas 1 oitava(s) acima; corrigido."]


def test_tenor_in_plain_treble_clef_gets_an_8vb_clef():
    t = part(("A", 4), ("G", 4), ("A", 4), ("B", 4), c=clef())
    r = merge(t, voices=["Tenor"])
    assert [x["midi"] for x in analyze(r.xml)["voices"][0]["notes"]] == [57, 55, 57, 59]
    assert ET.fromstring(r.xml).findtext("part/measure/attributes/clef/clef-octave-change") == "-1"


def test_missing_measure_is_filled_inside_its_section():
    # Verse: 3 measures + double bar; refrain: 2 measures + final bar. The alto lost a verse measure.
    full = single(m(1, n("C", 5, 4)), m(2, n("D", 5, 4)), m(3, n("E", 5, 4), DOUBLE_BAR), m(4, n("F", 5, 4)), m(5, n("G", 5, 4), FINAL))
    short = single(m(1, n("A", 4, 4)), m(2, n("B", 4, 4), DOUBLE_BAR), m(3, n("C", 5, 4)), m(4, n("D", 5, 4), FINAL))
    r = merge(full, short, voices=["Soprano", "Contralto"])
    assert r.warnings == ["Contralto: 1 compasso(s) não reconhecido(s) entre os compassos 1 e 3; completado(s) com pausa."]
    alto = ET.fromstring(r.xml).findall("part")[1].findall("measure")
    assert [mm.get("number") for mm in alto] == ["1", "2", "3", "4", "5"]
    assert alto[1].find("note/rest").get("measure") == "yes"  # inserted before the section's last measure
    assert [x["t"] for x in analyze(r.xml)["voices"][1]["notes"]] == [0, 8, 12, 16]  # the refrain stays in place


def test_parts_without_common_sections_are_padded_at_the_end():
    full = single(m(1, n("C", 5, 4)), m(2, n("D", 5, 4)))
    short = single(m(1, n("A", 4, 4)))
    r = merge(full, short, voices=["Soprano", "Contralto"])
    assert len(r.warnings) == 1 and "Contralto" in r.warnings[0]
    assert len(ET.fromstring(r.xml).findall("part")[1].findall("measure")) == 2


def test_voice_split_into_parts_by_omr_is_folded_back():
    # Audiveris read the "C" label as "0" on line 1 and "U" on line 2: two parts, each with
    # notes only in its own system (and different divisions).
    xml = score(
        m(1, n("C", 4, 4)) + m(2, n("D", 4, 4)) + m(3, rest(4)),
        m(1, rest(8)) + m(2, rest(8)) + m(3, n("E", 4, 4) + n("F", 4, 4)),
    )
    xml = xml.replace(b'<part id="P2"><measure number="1"><attributes><divisions>1', b'<part id="P2"><measure number="1"><attributes><divisions>2')
    r = merge(xml, voices=["Contralto"])
    assert part_names(r.xml) == ["Contralto"]
    a = analyze(r.xml)
    assert [(x["t"], x["d"], x["midi"]) for x in a["voices"][0]["notes"]] == [(0, 4, 60), (4, 4, 62), (8, 2, 64), (10, 2, 65)]


def test_real_parts_in_one_file_are_kept():
    xml = score(m(1, n("C", 5, 4)) + m(2, n("D", 5, 4)), m(1, n("C", 4, 4)) + m(2, n("B", 3, 4)))
    assert part_names(merge(xml, voices=["Soprano"]).xml) == ["Soprano 1", "Soprano 2"]
