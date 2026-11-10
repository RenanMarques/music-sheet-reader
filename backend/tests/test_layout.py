from xml.etree import ElementTree as ET

from app.layout import can_combine, combine_staves
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
