import time

import pytest
from fastapi.testclient import TestClient

from tests.test_musicxml import m, n, score


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "SCORES_DIR", tmp_path / "scores")
    (tmp_path / "scores").mkdir()
    return TestClient(main.app)


def wait_done(client, score_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = client.get(f"/api/scores/{score_id}").json()
        if meta["status"] in ("done", "error"):
            return meta
        time.sleep(0.05)
    raise AssertionError("timeout")


def test_musicxml_upload_roundtrip(client):
    xml = score(m(1, n("C", 4, 4)))
    r = client.post("/api/scores", files={"file": ("hino.musicxml", xml)})
    assert r.status_code == 202
    sid = r.json()["id"]
    meta = wait_done(client, sid)
    assert meta["status"] == "done" and meta["title"] == "hino" and meta["voices"] == 1
    a = client.get(f"/api/scores/{sid}/analysis").json()
    assert a["voices"][0]["notes"] == [{"t": 0.0, "d": 4.0, "midi": 60, "m": 0}]
    assert client.get(f"/api/scores/{sid}/musicxml").content == xml
    assert [s["id"] for s in client.get("/api/scores").json()] == [sid]
    assert client.delete(f"/api/scores/{sid}").status_code == 204
    assert client.get(f"/api/scores/{sid}").status_code == 404


def test_bad_xml_reports_error(client):
    sid = client.post("/api/scores", files={"file": ("x.xml", b"<nope")}).json()["id"]
    meta = wait_done(client, sid)
    assert meta["status"] == "error" and "inválido" in meta["error"]


def test_rejects_unknown_extension(client):
    assert client.post("/api/scores", files={"file": ("a.docx", b"x")}).status_code == 400


def test_path_traversal_ids_are_rejected(client):
    assert client.get("/api/scores/..%2F..%2Fetc").status_code == 404


def test_omr_upload_uses_filename_as_title(client, monkeypatch, tmp_path):
    from app import main

    xml = score(m(1, n("C", 4, 4))).replace(b"<part-list>", b"<movement-title>ocr garbage</movement-title><part-list>")

    def fake_omr(source, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "source.xml").write_bytes(xml)
        return out_dir / "source.xml"

    monkeypatch.setattr(main, "run_audiveris", fake_omr)
    sid = client.post("/api/scores", files={"file": ("Meu Hino.pdf", b"%PDF-1.5")}).json()["id"]
    meta = wait_done(client, sid)
    assert meta["status"] == "done" and meta["title"] == "Meu Hino"


def test_lyric_edit_and_restore(client):
    xml = score(m(1, n("C", 4, 4))).replace(b"</note>", b'<lyric number="1"><syllabic>single</syllabic><text>Ola</text></lyric></note>', 1)
    sid = client.post("/api/scores", files={"file": ("h.musicxml", xml)}).json()["id"]
    wait_done(client, sid)
    r = client.put(f"/api/scores/{sid}/lyrics", json={"edits": [{"part": "P1", "measure": 0, "offset": 0, "text": "Olá"}]})
    assert r.status_code == 200 and r.json()["lyrics_edited"] is True
    assert "Olá" in client.get(f"/api/scores/{sid}/musicxml").text
    bad = client.put(f"/api/scores/{sid}/lyrics", json={"edits": [{"part": "P1", "measure": 9, "offset": 0, "text": "x"}]})
    assert bad.status_code == 400
    r = client.post(f"/api/scores/{sid}/lyrics/restore")
    assert r.json()["lyrics_edited"] is False
    assert "Ola<" in client.get(f"/api/scores/{sid}/musicxml").text


def test_responses_ask_browsers_to_revalidate(client):
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/api/scores").headers["cache-control"] == "no-cache"


def _fake_omr_by_content(monkeypatch, pitches):
    """Audiveris stand-in: the uploaded bytes name the voice whose MusicXML it returns."""
    from app import main

    def fake_omr(source, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        step, octave = pitches[source.read_bytes().decode()]
        (out_dir / "x.xml").write_bytes(score(m(1, n(step, octave, 4))))
        return out_dir / "x.xml"

    monkeypatch.setattr(main, "run_audiveris", fake_omr)


def test_parts_upload_is_combined_into_one_score(client, monkeypatch):
    _fake_omr_by_content(monkeypatch, {"low": ("C", 3), "high": ("E", 5), "mid-high": ("C", 5), "mid-low": ("G", 3)})
    uploads = [("files", (f"parte {i}.pdf", v.encode())) for i, v in enumerate(["low", "high", "mid-low", "mid-high"])]
    r = client.post("/api/scores", files=uploads, data={"voices": ["", "", "", ""]})
    assert r.status_code == 202
    sid = r.json()["id"]
    meta = wait_done(client, sid)
    assert meta["status"] == "done", meta.get("error")
    # Title: no text on these fake PDFs, so the first file's name.
    assert meta["title"] == "parte 0" and meta["voices"] == 4 and meta["warnings"] == []
    # Sources in score order, with the voice found in each and how.
    assert [(s["filename"], s["voices"][0]["name"], s["voices"][0]["how"]) for s in meta["sources"]] == [
        ("parte 1.pdf", "Soprano", "alturas"), ("parte 3.pdf", "Contralto", "alturas"),
        ("parte 2.pdf", "Tenor", "alturas"), ("parte 0.pdf", "Baixo", "alturas"),
    ]
    a = client.get(f"/api/scores/{sid}/analysis").json()
    assert [(v["name"], v["notes"][0]["midi"]) for v in a["voices"]] == [
        ("Soprano", 76), ("Contralto", 72), ("Tenor", 55), ("Baixo", 48)
    ]
    assert client.get(f"/api/scores/{sid}/source?part=0").content == b"high"
    assert client.get(f"/api/scores/{sid}/source?part=4").status_code == 404


def test_voice_chosen_by_the_user_and_title(client, monkeypatch):
    _fake_omr_by_content(monkeypatch, {"a": ("E", 5), "b": ("C", 5)})
    files = [("files", ("x.pdf", b"a")), ("files", ("y.pdf", b"b"))]
    sid = client.post("/api/scores", files=files, data={"voices": ["Solista", ""], "title": "Meu Hino"}).json()["id"]
    meta = wait_done(client, sid)
    assert meta["title"] == "Meu Hino"
    assert [(s["voices"][0]["name"], s["voices"][0]["how"]) for s in meta["sources"]] == [("Solista", "escolhida"), ("Soprano", "alturas")]
    assert "Meu%20Hino.musicxml" in client.get(f"/api/scores/{sid}/musicxml").headers["content-disposition"]


def test_voices_must_match_files(client):
    files = [("files", ("x.musicxml", score(m(1, n("C", 4, 4))))), ("files", ("y.musicxml", score(m(1, n("C", 4, 4)))))]
    assert client.post("/api/scores", files=files, data={"voices": ["Soprano"]}).status_code == 400


def test_rename_and_reorder_voices(client):
    xml = score(m(1, n("E", 5, 4)), m(1, n("C", 3, 4)))
    sid = client.post("/api/scores", files={"file": ("h.musicxml", xml)}).json()["id"]
    wait_done(client, sid)
    a = client.get(f"/api/scores/{sid}/analysis").json()
    assert [(v["id"], v["name"]) for v in a["voices"]] == [("v1", "Soprano"), ("v2", "Baixo")]
    r = client.put(f"/api/scores/{sid}/voices", json={"voices": [{"id": "v2", "name": "Barítono"}, {"id": "v1", "name": ""}]})
    assert r.status_code == 200
    a = client.get(f"/api/scores/{sid}/analysis").json()
    assert [(v["id"], v["name"], v["auto_name"]) for v in a["voices"]] == [("v2", "Barítono", "Baixo"), ("v1", "Soprano", "Soprano")]
    assert client.put(f"/api/scores/{sid}/voices", json={"voices": [{"id": "v9"}]}).status_code == 400
    assert client.put(f"/api/scores/{sid}/voices", json={"voices": []}).status_code == 200
    assert [v["name"] for v in client.get(f"/api/scores/{sid}/analysis").json()["voices"]] == ["Soprano", "Baixo"]


def test_parts_upload_rejects_a_bad_file(client):
    files = [("files", ("[S] h.musicxml", b"<x/>")), ("files", ("[C] h.docx", b"x"))]
    r = client.post("/api/scores", files=files)
    assert r.status_code == 400 and "[C] h.docx" in r.json()["detail"]


def test_separate_staves_layout(client):
    treble = m(1, n("E", 5, 2) + n("C", 5, 2, chord=True) + n("D", 5, 2) + n("B", 4, 2, chord=True))
    sid = client.post("/api/scores", files={"file": ("h.musicxml", score(treble))}).json()["id"]
    meta = wait_done(client, sid)
    assert meta["separable"] and not meta["combinable"]
    r = client.get(f"/api/scores/{sid}/musicxml?layout=separate")
    assert r.status_code == 200 and b'<part id="P1__s1v2">' in r.content
    client.put(f"/api/scores/{sid}/voices", json={"voices": [{"id": "v1", "name": ""}, {"id": "v2", "name": "Alto"}]})
    assert b"<part-name>Alto</part-name>" in client.get(f"/api/scores/{sid}/musicxml?layout=separate").content


def test_hidden_voices_are_left_out_of_the_drawing(client):
    treble = m(1, n("E", 5, 2) + n("C", 5, 2, chord=True) + n("D", 5, 2) + n("B", 4, 2, chord=True))
    sid = client.post("/api/scores", files={"file": ("h.musicxml", score(treble))}).json()["id"]
    wait_done(client, sid)
    r = client.get(f"/api/scores/{sid}/musicxml?hide=v1")
    assert r.status_code == 200 and b'<part id="P1__s1v2">' in r.content and b"P1__s1v1" not in r.content
    assert b"P1__s1v" not in client.get(f"/api/scores/{sid}/musicxml?hide=bogus").content
