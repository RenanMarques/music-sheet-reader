from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app.lyrics import LyricsError, _group, _host, apply_lyric_edits, normalize_lyrics

FIXTURES = Path(__file__).parent / "fixtures"


def note(step, octave=4, dur=1, voice=1, chord=False, lyrics=(), staff=None):
    ly = "".join(
        f'<lyric number="{v}"><syllabic>{s}</syllabic><text>{t}</text></lyric>' for v, s, t in lyrics
    )
    return (
        "<note>" + ("<chord/>" if chord else "")
        + f"<pitch><step>{step}</step><octave>{octave}</octave></pitch><duration>{dur}</duration>"
        + f"<voice>{voice}</voice>" + (f"<staff>{staff}</staff>" if staff else "") + ly + "</note>"
    )


def doc(*measures, parts=1):
    body = ""
    for p in range(parts):
        ms = measures if p == 0 else measures[len(measures) // 2:]
        body += f'<part id="P{p + 1}">' + "".join(
            f'<measure number="{i + 1}">' + ("<attributes><divisions>1</divisions></attributes>" if i == 0 else "") + m + "</measure>"
            for i, m in enumerate(ms)
        ) + "</part>"
    return f'<score-partwise version="4.0"><part-list/>{body}</score-partwise>'.encode()


def syllables(xml: bytes, verse="1"):
    """[(measure, voice, syllabic, text)] in document order."""
    out = []
    for m in ET.fromstring(xml).iter("measure"):
        for n in m.iter("note"):
            for ly in n.findall("lyric"):
                if ly.get("number") == verse:
                    out.append((m.get("number"), n.findtext("voice"), ly.findtext("syllabic"), ly.findtext("text")))
    return out


def test_syllables_on_lower_voice_move_to_top_voice():
    # "Can" on soprano, "tam" stuck on the alto note at beat 2 (as Audiveris does).
    m = (
        note("C", 5, lyrics=[("1", "begin", "Can")]) + note("D", 5) + note("E", 5, 2)
        + "<backup><duration>4</duration></backup>"
        + note("A", 4, voice=2) + note("B", 4, voice=2, lyrics=[("1", "end", "tam")]) + note("C", 5, 2, voice=2)
    )
    xml, stats = normalize_lyrics(doc(m))
    assert stats == {"moved": 1, "dropped": 0}
    assert syllables(xml) == [("1", "1", "begin", "Can"), ("1", "1", "end", "tam")]


def test_lyric_goes_on_chord_head_and_keeps_verse_order():
    m = note("C", 5, lyrics=[("1", "single", "a")]) + note("A", 4, chord=True, lyrics=[("2", "single", "b")]) + note("C", 5, 3)
    xml, _ = normalize_lyrics(doc(m))
    first = next(ET.fromstring(xml).iter("note"))
    assert [ly.get("number") for ly in first.findall("lyric")] == ["1", "2"]


def test_footer_credits_read_as_lyrics_are_dropped():
    words = [("1", "single", w) for w in "um dois tres quatro cinco seis sete oito nove dez".split()]
    m1 = "".join(note("C", 5, lyrics=[w]) for w in words[:4])
    m2 = "".join(note("C", 5, lyrics=[w]) for w in words[4:8])
    m3 = "".join(note("C", 5, lyrics=[w]) for w in words[8:]) + note("C", 5, 2)
    # Part 2 (bass) only has two stray "syllables": the composer credit.
    bass = note("C", 3, lyrics=[("1", "single", "Mendelssohn")]) + note("C", 3, lyrics=[("4", "single", "com")]) + note("C", 3, 2)
    xml = f'''<score-partwise version="4.0"><part-list/>
      <part id="P1"><measure number="1"><attributes><divisions>1</divisions></attributes>{m1}</measure>
        <measure number="2">{m2}</measure><measure number="3">{m3}</measure></part>
      <part id="P2"><measure number="1"><attributes><divisions>1</divisions></attributes>{bass}</measure></part>
    </score-partwise>'''.encode()
    out, stats = normalize_lyrics(xml)
    assert stats["dropped"] == 2
    texts = [ly.findtext("text") for ly in ET.fromstring(out).iter("lyric")]
    assert texts == [w[2] for w in words]


def test_audiveris_fixture_hyphens_are_continuous_on_one_voice():
    # Audiveris output with OCR enabled (lyrics present), for examples/Cantam Anjos Harmonia.pdf.
    xml, stats = normalize_lyrics((FIXTURES / "cantam_anjos_audiveris_ocr.xml").read_bytes())
    assert stats["moved"] > 0
    root = ET.fromstring(xml)
    # Every syllable sits on the top voice sounding at its onset (Audiveris sometimes puts a
    # whole half-measure in voice 2, and then voice 2 *is* the top voice there).
    for notes in _group(root).values():
        with_lyrics = [x for x in notes if x.el.find("lyric") is not None]
        if with_lyrics:
            assert with_lyrics == [_host(notes)]
    p2 = next(p for p in root.iter("part") if p.get("id") == "P2")
    assert not list(p2.iter("lyric"))


EDIT_DOC = doc(
    note("C", 5, lyrics=[("1", "begin", "Can")]) + note("D", 5, lyrics=[("1", "end", "tern")]) + note("E", 5) + note("F", 5, lyrics=[("1", "single", "po")]),
    note("G", 5, lyrics=[("1", "single", "vos")]) + note("A", 5, 3),
)


def edit(**kw):
    return {"part": "P1", "staff": "1", "verse": "1", **kw}


def test_edit_fixes_text_and_keeps_hyphenation():
    out = apply_lyric_edits(EDIT_DOC, [edit(measure=0, offset=1, text="tem")])
    assert syllables(out)[:2] == [("1", "1", "begin", "Can"), ("1", "1", "end", "tem")]


def test_trailing_hyphen_joins_next_syllable_into_a_word():
    out = apply_lyric_edits(EDIT_DOC, [edit(measure=0, offset=3, text="po-")])
    assert [(s, t) for _, _, s, t in syllables(out)] == [("begin", "Can"), ("end", "tern"), ("begin", "po"), ("end", "vos")]


def test_add_syllable_on_note_without_lyric_and_remove_one():
    out = apply_lyric_edits(EDIT_DOC, [edit(measure=0, offset=2, text="to"), edit(measure=1, offset=0, text="")])
    assert [t for *_, t in syllables(out)] == ["Can", "tern", "to", "po"]
    # Removing "Can" turns "tern" into the start of the line: it no longer continues a word.
    out = apply_lyric_edits(EDIT_DOC, [edit(measure=0, offset=0, text="")])
    assert syllables(out)[0][2:] == ("single", "tern")


def test_edit_errors():
    with pytest.raises(LyricsError):
        apply_lyric_edits(EDIT_DOC, [edit(measure=5, offset=0, text="x")])
    with pytest.raises(LyricsError):
        apply_lyric_edits(EDIT_DOC, [{"measure": 0}])
