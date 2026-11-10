from xml.etree import ElementTree as ET

from app.layout import can_combine, can_separate, combine_staves, draw, separate_staves
from app.lyrics import apply_lyric_edits
from app.merge import PartFile, merge_scores
from app.musicxml import analyze
from tests.test_musicxml import m, n, rest, score

LYRIC = '<lyric number="1"><syllabic>single</syllabic><text>{}</text></lyric></note>'


def with_clef(xml: bytes, sign: str, line: int) -> bytes:
    return xml.replace(b"</time></attributes>", f"</time><clef><sign>{sign}</sign><line>{line}</line></clef></attributes>".encode(), 1)


def satb() -> bytes:
    s = with_clef(score(m(1, n("E", 5, 2).replace("</note>", LYRIC.format("Can"), 1) + n("D", 5, 2))), "G", 2)
    # Different divisions in one part, as OMR output often has.
    c = with_clef(score(m(1, n("C", 5, 2) + n("B", 4, 2) + n("A", 4, 4).replace("</note>", LYRIC.format("xx"), 1)), divisions=2), "G", 2)
    t = with_clef(score(m(1, n("G", 3, 4))), "F", 4)
    b = with_clef(score(m(1, n("C", 3, 2) + rest(2))), "F", 4)
    return merge_scores([PartFile(xml=x, label=f"{i}") for i, x in enumerate([s, c, t, b])], "Hino").xml


def test_same_clef_voices_share_a_staff():
    xml = satb()
    assert can_combine(xml)
    out = combine_staves(xml)
    root = ET.fromstring(out)
    assert [sp.findtext("part-name") for sp in root.iterfind("part-list/score-part")] == ["Soprano / Contralto", "Tenor / Baixo"]
    assert [p.get("id") for p in root.findall("part")] == ["P1", "P3"]
    treble = root.find("part")
    notes = treble.findall("measure/note")
    assert [(x.findtext("voice"), x.findtext("stem")) for x in notes] == [("1", "up")] * 2 + [("2", "down")] * 3
    assert [x.findtext("lyric/text") for x in notes] == ["Can", None, None, None, None]  # top voice's lyrics only
    # Same music as the separate parts (the analysis separates the two voices again).
    a = analyze(out)
    assert [[(x["t"], x["d"], x["midi"]) for x in v["notes"]] for v in a["voices"]] == [
        [(0, 2, 76), (2, 2, 74)], [(0, 1, 72), (1, 1, 71), (2, 2, 69)], [(0, 4, 55)], [(0, 2, 48)]
    ]


def test_lyric_edit_key_from_the_combined_staff_targets_the_top_voice():
    xml = satb()
    edited = apply_lyric_edits(xml, [{"part": "P1", "staff": "1", "measure": 0, "offset": 2, "verse": "1", "text": "tam"}])
    assert [x.findtext("lyric/text") for x in ET.fromstring(combine_staves(edited)).find("part").iter("note")][:2] == ["Can", "tam"]


def test_nothing_to_combine():
    xml = with_clef(score(m(1, n("C", 5, 4)), m(1, n("C", 3, 4))), "G", 2)
    assert not can_combine(score(m(1, n("C", 5, 4))))
    assert combine_staves(score(m(1, n("C", 5, 4)))) == score(m(1, n("C", 5, 4)))
    assert not can_combine(xml)  # second part has no clef: different


# --------------------------------------------------------------------------- separate staves


def shared_satb() -> bytes:
    """Soprano+alto as chords on a treble staff, tenor+bass on a bass staff (hymnal style).
    The first syllable hangs on the alto's notehead, as OMR often leaves it."""
    treble = m(1, n("E", 5) + n("C", 5, chord=True).replace("</note>", LYRIC.format("Can"), 1)
               + n("D", 5) + n("B", 4, chord=True) + n("C", 5, 2))  # last: one notehead, both voices
    treble += m(2, n("D", 5, 2) + n("B", 4, 2, chord=True) + n("C", 5, 2) + n("A", 4, 2, chord=True), '<barline location="right"><bar-style>light-heavy</bar-style></barline>')
    bass = m(1, n("G", 3) + n("C", 3, chord=True) + n("G", 3) + n("G", 2, chord=True) + n("E", 3, 2) + n("A", 2, 2, chord=True))
    bass += m(2, n("G", 3, 4) + n("G", 2, 4, chord=True))
    xml = score(treble, bass)
    xml = xml.replace(b"</time></attributes>", b"</time><clef><sign>G</sign><line>2</line></clef></attributes>", 1)
    i = xml.index(b'<part id="P2">')
    return xml[:i] + xml[i:].replace(b"</time></attributes>", b"</time><clef><sign>F</sign><line>4</line></clef></attributes>", 1)


def voices_of(a: dict) -> list[list[tuple]]:
    return [[(x["t"], x["d"], x["midi"]) for x in v["notes"]] for v in a["voices"]]


def pitches(part: ET.Element, measure: int = 0) -> list[str]:
    out = []
    for x in part.findall("measure")[measure].findall("note"):
        out.append("rest" if x.find("rest") is not None else x.findtext("pitch/step") + x.findtext("pitch/octave"))
    return out


def test_voices_of_a_shared_staff_get_their_own_staves():
    xml = shared_satb()
    assert can_separate(xml) and not can_combine(xml)
    out = separate_staves(xml)
    root = ET.fromstring(out)
    ids = ["P1__s1v1", "P1__s1v2", "P2__s1v1", "P2__s1v2"]
    assert [p.get("id") for p in root.findall("part")] == ids
    sps = root.findall("part-list/score-part")
    assert [(sp.get("id"), sp.findtext("part-name"), sp.findtext("part-abbreviation")) for sp in sps] == [
        (ids[0], "Soprano", "S"), (ids[1], "Contralto", "C"), (ids[2], "Tenor", "T"), (ids[3], "Baixo", "B")
    ]
    s, c, t, b = root.findall("part")
    assert pitches(s) == ["E5", "D5", "C5"]
    assert pitches(c) == ["C5", "B4", "C5"]  # the shared notehead is drawn on both staves
    assert pitches(b, 1) == ["G2"]
    assert all(x.find("chord") is None and x.findtext("voice") == "1" for x in root.iter("note"))
    assert [p.findtext("measure/attributes/clef/sign") for p in (s, c, t, b)] == ["G", "G", "F", "F"]
    assert [p.findall("measure")[1].findtext("barline/bar-style") for p in (s, c)] == ["light-heavy"] * 2
    # The syllable goes to the top voice.
    assert [x.findtext("lyric/text") for x in s.iter("note")] == ["Can", None, None, None, None]
    assert not c.findall(".//lyric")
    # Exactly the voices the analysis (and the mixer) has.
    assert voices_of(analyze(out)) == voices_of(analyze(xml))
    assert [v["name"] for v in analyze(out)["voices"]] == ["Soprano", "Contralto", "Tenor", "Baixo"]


def test_separated_staves_use_the_given_names():
    root = ET.fromstring(separate_staves(shared_satb(), ["Soprano", "Alto", None, "Baixo 2"]))
    assert [(sp.findtext("part-name"), sp.findtext("part-abbreviation")) for sp in root.iterfind("part-list/score-part")] == [
        ("Soprano", "S"), ("Alto", "C"), ("Tenor", "T"), ("Baixo 2", "B2")
    ]


def test_silent_voice_gets_rests():
    # Voice 2 sings only the first half note (explicit rest after it), and nothing in measure 2.
    treble = m(1, n("E", 5, 2) + n("D", 5) + n("E", 5) + "<backup><duration>4</duration></backup>"
               + n("C", 5, 2, voice=2) + rest(2, voice=2))
    treble += m(2, n("E", 5, 4) + "<backup><duration>4</duration></backup>" + rest(4, voice=2))
    root = ET.fromstring(separate_staves(score(treble)))
    top, low = root.findall("part")
    assert pitches(top) == ["E5", "D5", "E5"]
    assert pitches(low) == ["C5", "rest"]
    r = low.find("measure").findall("note")[1]
    assert (r.findtext("duration"), r.findtext("type")) == ("2", "half")
    whole = low.findall("measure")[1].find("note/rest")
    assert whole.get("measure") == "yes"
    assert low.findall("measure")[1].findtext("note/duration") == "4"


def test_two_staff_part_is_split_per_staff_and_voice():
    xml = score(m(1, n("E", 5, 4, staff=1) + n("C", 5, 4, chord=True, staff=1) + "<backup><duration>4</duration></backup>"
                  + n("G", 3, 4, voice=2, staff=2) + n("C", 3, 4, chord=True, voice=2, staff=2)))
    xml = xml.replace(b"</time></attributes>", b'</time><staves>2</staves><clef number="1"><sign>G</sign><line>2</line></clef>'
                      b'<clef number="2"><sign>F</sign><line>4</line></clef></attributes>', 1)
    root = ET.fromstring(separate_staves(xml))
    parts = root.findall("part")
    assert [p.get("id") for p in parts] == ["P1__s1v1", "P1__s1v2", "P1__s2v1", "P1__s2v2"]
    assert [p.findtext("measure/attributes/clef/sign") for p in parts] == ["G", "G", "F", "F"]
    assert root.find(".//staves") is None and root.find(".//note/staff") is None
    assert all(c.get("number") is None for c in root.iter("clef"))
    assert [pitches(p) for p in parts] == [["E5"], ["C5"], ["G3"], ["C3"]]


def test_lyric_edit_key_from_a_separated_staff_targets_the_original_staff():
    # The frontend maps "P1__s1v1" back to (P1, staff 1); the syllable is drawn on the top voice.
    edited = apply_lyric_edits(shared_satb(), [{"part": "P1", "staff": "1", "measure": 0, "offset": 1, "verse": "1", "text": "tam"}])
    s = ET.fromstring(separate_staves(edited)).find("part")
    assert [x.findtext("lyric/text") for x in s.iter("note")][:2] == ["Can", "tam"]


def test_nothing_to_separate():
    xml = with_clef(score(m(1, n("C", 5, 4)), m(1, n("C", 3, 4))), "G", 2)
    assert not can_separate(xml)
    assert separate_staves(xml) == xml


# --------------------------------------------------------------------------- hidden voices


def part_ids(xml: bytes) -> list[str]:
    root = ET.fromstring(xml)
    ids = [p.get("id") for p in root.findall("part")]
    assert ids == [sp.get("id") for sp in root.iterfind("part-list/score-part")]
    return ids


def test_hiding_a_voice_of_a_shared_staff_draws_the_other_one_alone():
    xml = shared_satb()
    assert part_ids(draw(xml, "parts", hidden={1})) == ["P1__s1v1", "P2"]  # T+B stay as stored
    out = draw(xml, "parts", hidden={0})
    assert part_ids(out) == ["P1__s1v2", "P2"]
    alto = ET.fromstring(out).find("part")
    assert pitches(alto) == ["C5", "B4", "C5"]
    assert alto.find(".//lyric/text").text == "Can"  # the top shown voice gets the syllables
    assert ET.fromstring(out).find("part-list/score-part/part-name").text == "Contralto"


def test_hidden_voices_in_the_separate_layout():
    assert part_ids(draw(shared_satb(), "separate", hidden={2, 3})) == ["P1__s1v1", "P1__s1v2"]


def test_hidden_part_is_removed_and_the_rest_still_combines():
    xml = satb()  # one part per voice: P1..P4
    assert part_ids(draw(xml, "parts", hidden={0, 3})) == ["P2", "P3"]
    out = draw(xml, "combined", hidden={1})
    assert part_ids(out) == ["P1", "P3"]
    assert [sp.findtext("part-name") for sp in ET.fromstring(out).iterfind("part-list/score-part")] == ["Soprano", "Tenor / Baixo"]


def test_hiding_every_voice_is_ignored():
    xml = shared_satb()
    assert draw(xml, "parts", hidden={0, 1, 2, 3}) == xml
    assert draw(xml, "parts") == xml
