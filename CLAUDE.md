# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Web tool that reads sheet music (PDF/image via OMR, or MusicXML), splits it into voices (e.g. SATB) and plays it with synthesized sounds, with per-voice solo/mute/volume. UI text is in Portuguese (pt-BR).

## Commands

Everything runs in Docker — the host has no pip/venv/node. `docker compose` passes `UID`/`GID` (defaults to 1000) so files in `./data` belong to the user.

```bash
docker compose up -d --build          # build + run at http://localhost:8000 (backend changes need --build)
docker compose logs -f app

# Tests (mount the source so no rebuild is needed for test runs)
docker run --rm -v "$PWD/backend:/app/backend" -w /app/backend music-sheet-reader python -m pytest -q -p no:cacheprovider
#   single test: append  tests/test_musicxml.py::test_satb_chords_are_split_by_pitch

# Run Audiveris by hand on a file (outputs MusicXML + .omr book with page images)
docker run --rm -u $(id -u):$(id -g) -v "$PWD/examples:/in:ro" -v "$PWD/data/out:/out" music-sheet-reader \
  /opt/audiveris/bin/Audiveris -batch -export -output /out \
  -constant org.audiveris.omr.sheet.BookManager.useCompression=false -- "/in/Cantam Anjos Harmonia.pdf"
```

`frontend/` is bind-mounted read-only by compose, so frontend edits only need a browser reload.

## Architecture

Pipeline: upload → (OMR if PDF/image) → MusicXML → `analyze()` → JSON playback model → browser.

- `backend/app/main.py` — FastAPI. Jobs run on a single-worker `ThreadPoolExecutor` (Audiveris is memory hungry). State lives on disk only, in `DATA_DIR/scores/<id>/` (`meta.json` status, `source.<ext>`, `omr/`, `score.xml`, `analysis.json`); queued/processing jobs are resumed at startup. Also serves `frontend/` at `/` and vendored JS at `/vendor`.
- `backend/app/omr.py` — wraps the Audiveris CLI (batch + export, uncompressed XML).
- `backend/app/musicxml.py` — pure-stdlib MusicXML parser + voice separation. This is the core logic:
  - **MusicXML `<voice>` numbers are ignored**: Audiveris encodes them unreliably (soprano+alto usually come out as chords in one voice). Notes are grouped by (part, staff); `estimate_voice_count` picks the polyphony present at ≥20% of onsets (max 4/staff); `separate_voices` assigns notes per onset with a monotone pitch-order mapping that minimises distance to each voice's previous pitch.
  - Fewer notes than voices ⇒ the note is **doubled** (a shared notehead, typical in hymns) unless an explicit `<rest>` on that staff covers the onset. Doubled copies are released when a new note needs a voice.
  - Measure lengths are aligned across parts (max content length per measure), so a rhythm error in one OMR'd part does not shift the others.
  - Repeats/voltas are expanded into a `timeline` (D.C./D.S. are not). Note times are in quarter notes, already in performance order; each note keeps `m` = original measure index.
  - Voice names: 4 voices total ⇒ Soprano/Contralto/Tenor/Baixo; 1 ⇒ Melodia; else "Voz N".
- `frontend/app.js` — vanilla JS, no build step. Tone.js: one `PolySynth` → `Tone.Channel` → `Tone.Part` per voice (solo/mute use `Channel.solo`/`mute`); times scheduled in ticks so tempo changes apply live. OpenSheetMusicDisplay renders `score.xml`; the cursor is synced by mapping (original measure index, offset in measure) to precomputed cursor steps, which keeps it correct across repeats.

## Gotchas

- Audiveris 5.11 is installed by `dpkg-deb -x` (its postinst fails headless) and needs `libgtk-3` even in batch mode.
- Audiveris OCR uses Tesseract's legacy engine: needs the full `tessdata` models (in `/opt/tessdata`), not Ubuntu's LSTM-only packages. Without OCR, lyrics get misread as music symbols and note recognition gets worse.
- OCR'd titles/credits are garbage-prone: for OMR uploads the title is the filename, and OSMD is told not to draw titles/credits.
- `backend/tests/fixtures/cantam_anjos_audiveris.xml` is real Audiveris output for `examples/Cantam Anjos Harmonia.pdf` (generated before OCR was enabled, so it includes a recognition error in m.2); the test checks SATB detection and the opening bass line.
- Frontend libs (Tone.js 15.0.4, OSMD 1.9.0) are downloaded into `/app/vendor` at image build time; versions are Dockerfile `ARG`s.
