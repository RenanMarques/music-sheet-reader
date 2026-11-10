from app.sheettext import common_title
from app.voices import Line, assign, find_type, voice_from_lines

OH_VINDE = ["OH, VINDE FIÉIS", ". CONTRALTO .", "C", ". H.C.C. 090 .", "1. Oh, vin2. O- lhai,"]


def test_find_type():
    assert find_type("Soprano 2").name == "Soprano"
    assert find_type("Alto").name == "Contralto"
    assert find_type("Bass").name == "Baixo"
    assert find_type("mezzo soprano").name == "Mezzo-soprano"
    assert find_type("Solista") is None
    assert find_type("C") is None  # single letters are too ambiguous (lyrics, staff labels)


def test_voice_printed_in_the_header():
    assert voice_from_lines(OH_VINDE).name == "Contralto"
    assert voice_from_lines(["CANTAM ANJOS HARMONIAS", ". BAIXO .", ". ."]).name == "Baixo"
    assert voice_from_lines(["Hino", "Do alto céu nos vem"]) is None  # a lyric line is not a voice label


def test_common_title_of_the_sheets():
    other = ["OH, VINDE FIÉIS", ". TENOR .", "T", ". H.C.C. 090 ."]
    assert common_title([OH_VINDE, other]) == "Oh, Vinde Fiéis"
    assert common_title([["Direitos autorais", "Hino"], ["Direitos autorais", "Hino"]]) == "Hino"
    assert common_title([OH_VINDE, []]) is None
    assert common_title([OH_VINDE]) is None


def test_assign_keeps_known_voices_in_pitch_order():
    soprano = find_type("Soprano")
    # A line above a known Soprano is a Soprano as well; below it, the comparison goes on.
    lines = [Line(70), Line(66, fixed=soprano), Line(57, low_clef=True)]
    assert [t.name for t in assign(lines)] == ["Soprano", "Soprano", "Tenor"]
    # Known voices against their pitches: they are kept, the rest is assigned around them.
    lines = [Line(50, low_clef=True, fixed=soprano), Line(70, fixed=find_type("Baixo")), Line(64)]
    assert [t.name for t in assign(lines)] == ["Soprano", "Baixo", "Contralto"]
