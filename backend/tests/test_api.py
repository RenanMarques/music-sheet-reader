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
