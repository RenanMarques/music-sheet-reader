# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Web tool that reads sheet music (PDF/image via OMR, or MusicXML), splits it into voices (e.g. SATB) and plays it with synthesized sounds, with per-voice solo/mute/volume. UI text is in Portuguese (pt-BR).

## Commands

Everything runs in Docker — the host has no pip/venv/node. `./start.sh` exports `HOST_UID`/`HOST_GID` (compose runs the container as that user, so `./data` stays user-owned; bash's `UID` isn't exported, hence the separate names), creates `./data`, runs compose and waits for `/api/scores`.

```bash
./start.sh                            # build + run at http://localhost:8000 and wait until it answers (--open, --no-build, PORT=…)
docker compose logs -f app

# Tests (mount the source so no rebuild is needed for test runs)
docker run --rm -v "$PWD/backend:/app/backend" -w /app/backend music-sheet-reader python -m pytest -q -p no:cacheprovider
#   single test: append  tests/test_musicxml.py::test_satb_chords_are_split_by_pitch

# Run Audiveris by hand on a file (outputs MusicXML + .omr book with page images)
docker run --rm -u $(id -u):$(id -g) -v "$PWD/examples:/in:ro" -v "$PWD/data/out:/out" music-sheet-reader \
  /opt/audiveris/bin/Audiveris -batch -export -output /out \
  -constant org.audiveris.omr.sheet.BookManager.useCompression=false \
  -constant org.audiveris.omr.image.ImageLoading.pdfResolution=400 -- "/in/Cantam Anjos/Cantam Anjos Harmonia.pdf"
```

`frontend/` is bind-mounted read-only by compose, so frontend edits only need a browser reload.

## Architecture

Pipeline: upload → (OMR if PDF/image) → MusicXML → `analyze()` → JSON playback model → browser.

- `backend/app/main.py` — FastAPI. Jobs run on a single-worker `ThreadPoolExecutor` (Audiveris is memory hungry). State lives on disk only, in `DATA_DIR/scores/<id>/` (`meta.json` status, `source.<ext>`, `omr/`, `score.xml`, `analysis.json`); queued/processing jobs are resumed at startup. Also serves `frontend/` at `/` and vendored JS at `/vendor`.
- `backend/app/omr.py` — wraps the Audiveris CLI (batch + export, uncompressed XML). PDFs are rasterised at 400 dpi (`OMR_PDF_DPI`; Audiveris' 300 dpi default leaves hymn-sheet staves ~18 px apart and misses hollow noteheads/whole rests); pages over Audiveris' pixel limit (`LoadStep.maxPixelCount`, ~A3 at 400 dpi) are retried at the default.
- `backend/app/lyrics.py` — lyrics clean-up and editing on the MusicXML. Audiveris gets hyphenation (`<syllabic>`) mostly right but hangs syllables on whichever voice owns the note, and OSMD only draws hyphens between syllables of the *same* voice; `normalize_lyrics` (OMR uploads only) moves each staff's syllables to the top sounding voice and drops short lyric lines (footer credits read as lyrics). `apply_lyric_edits` addresses notes by (part, staff, measure index, offset in quarters) — the key the frontend reads from OSMD — and recomputes begin/middle/end along the verse; a trailing "-" in the edited text means the word continues. `score.orig.xml` is the pre-edit version (restore copies it back); scores processed before this existed are migrated at startup.
- `backend/app/musicxml.py` — pure-stdlib MusicXML parser + voice separation. This is the core logic:
  - **MusicXML `<voice>` numbers are ignored**: Audiveris encodes them unreliably (soprano+alto usually come out as chords in one voice). Notes are grouped by (part, staff); `estimate_voice_count` picks the polyphony present at ≥20% of onsets (max 4/staff); `separate_voices` assigns notes per onset with a monotone pitch-order mapping that minimises distance to each voice's previous pitch.
  - Fewer notes than voices ⇒ the note is **doubled** (a shared notehead, typical in hymns) unless an explicit `<rest>` on that staff covers the onset. Doubled copies are released when a new note needs a voice.
  - Measure lengths are aligned across parts (max content length per measure), so a rhythm error in one OMR'd part does not shift the others.
  - Repeats/voltas are expanded into a `timeline` (D.C./D.S. are not). Note times are in quarter notes, already in performance order; each note keeps `m` = original measure index.
  - Voice names: 1 voice ⇒ "Melodia"; otherwise identified comparatively by `app.voices` (below).
- `backend/app/voices.py` — voice identification, by content (never by file name). Catalog of voice types with their typical median pitch (Soprano, Mezzo-soprano, Contralto, Tenor, Barítono, Baixo). Signals, strongest first: the user's choice, the voice printed in the sheet's header (`voice_from_lines`, a line that is just the voice word, e.g. ". SOPRANO ."), then `assign()`: lines sorted by median pitch get types by a monotone DP (a higher line never gets a lower type) minimising distance to each type's center, with costs for rare types, repeated types (→ "Soprano 1/2" via `numbered()`) and clef mismatch; known voices are positions in that order (fallback when they contradict the pitches). Used both for combined per-voice sheets and for naming the voices separated from a single score (`musicxml._voice_names`; part names win when every part is one voice with a distinct, meaningful name). No fixed "4 voices ⇒ SATB" rule.
- `backend/app/sheettext.py` — text printed on a sheet: `pdftotext` (poppler-utils, in the image) for digital PDFs, else MusicXML credits. `common_title()`: the first header line printed on *all* the per-voice sheets is the title.
- `backend/app/merge.py` — scores from **one file per voice** (`files` + parallel `voices` form fields; "" = automatic; a name not in the catalog, e.g. "Solista", is kept as is and ordered by pitch). Each file is OMR'd (`omr/<n>/`) and lyric-normalised on its own, then `merge_scores()` builds one part per voice, highest first. Fixes for typical OMR damage: parts of one file that are a single voice split per system (Audiveris names parts after the label left of each system, e.g. "C" read as "0"/"U"/"O") are folded back; a known voice in the right clef but an octave off its range is shifted (±1 octave only; a clef that contradicts the voice means the *voice* is wrong → warning instead), and an unknown voice in treble "8va" (never used for singers) is shifted down; `clef-octave-change` is adjusted so the drawing is unchanged; parts with missing measures get whole-measure rests *inside the section* where they are short (section barlines — double bars, repeats, voltas — are the anchors). `meta["sources"]` is in score order, each with `voices: [{name, abbr, how}]` (`how`: escolhida / texto da partitura / alturas); `meta["warnings"]` lists fixes; `/source?part=i` serves each file.
- Voice names/order set by the user (`PUT /api/scores/{id}/voices`, `meta["voice_layout"]`) are applied when serving `/analysis` (each voice also gets `auto_name`); analysis.json itself is never rewritten.
- `backend/app/layout.py` — drawing-only alternative layouts, generated on the fly from score.xml by `GET /musicxml?layout=…`; playback/analysis always use the stored parts.
  - `combined`: neighbouring single-staff parts with the same clef and measure count (S+C, T+B) on one staff (voices 1/2, stems up/down, lyrics of the top voice only). The combined staff keeps the top part's id, so lyric edits made on it address the top voice in score.xml unchanged (the frontend only offers voice-1 notes as lyric slots in this layout). `meta["combinable"]`.
  - `separate`: the other way round, each voice of a shared staff (e.g. S+A as chords) on its own staff. It reuses `musicxml.separate()` (the same voice separation as the analysis; each `NoteEvent.src` keeps its source `<note>` elements), so each new staff is exactly one mixer voice, named as in the mixer (user names from `voice_layout`). Gaps become rests, notes the separation shortened are cut (type/ties fixed), broken beam groups are dropped, all lyrics of an onset are drawn on the highest voice singing there. New part ids are `<part>__s<staff>v<voice>`; the frontend maps them back (`sourcePart()`) so lyric edits address the original (part, staff), and only offers slots on `v1` staves. `meta["separable"]`.
  - Both flags are computed at processing and migrated at startup.
- `frontend/` layout is a fixed app shell (CSS grid, `100dvh`): only `.stage` (the score) scrolls, so OSMD's `followCursor` never moves the transport bar (bottom) or the voice mixer (right). Mixer/transport/header meta are shown only while a score is loaded (`show()` in `app.js`); the library is a collapsible column that becomes a drawer below 1100px.
- New score screen (`#compose`, route `#nova`): files are staged in the browser (`compose.files`, each with a voice: "" = automático, a catalog voice, or free text) and only sent on "Processar". One file without a voice → the single-score path (`file` field); otherwise `files` + `voices`.
- Mixer "Editar": rename/reorder voices (`startVoiceEdit`/`saveVoiceEdit`). Reordering permutes `state.voices` (audio objects) and `state.analysis.voices` together; `showNote()` takes the voice object, so playback callbacks don't depend on positions.
- User preferences live in the `settings` object at the top of `app.js` (`DEFAULT_SETTINGS`, persisted as JSON in `localStorage["settings"]`): `noteNames` (`"letters"` → A3, default; `"solfege"` → Lá3) and `follow`. There is no settings screen yet; `leitor.set(key, value)` in the browser console changes them. All note/key names in the UI go through `noteName()`/`keyName()` (the backend's `low`/`high` strings are not displayed).
- All position changes go through `seekTo(q)`, which also does **note chasing** (`chase()`): notes already sounding at `q` are re-triggered for their remaining length, so jumping into a held note isn't silent. Playback start and slider release chase too.
- Click-to-seek on the score: `measureBox()`/`hitTest()` map pointer coordinates to OSMD geometry (`GraphicSheet.MeasureList[measure][staff].PositionAndShape`, units × `10 * osmd.zoom` px; first measure of a system extends left over the clef). Paused → the click moves the position; playing → it only proposes the jump (`#jump-pop`, 3 s, Enter/Esc) so a stray click can't interrupt; Alt+click snaps to the nearest `staffEntries` column instead of the measure start. With repeats, `timeOf()` picks the occurrence closest to the current position. `leitor.measureRect(i)` exposes measure rectangles for browser tests.
- Lyric editing mode ("Editar letra"): clicks edit syllables instead of seeking. `lyricLines()` builds, from OSMD's model (`staffEntries[].LyricsEntries`, label `PositionAndShape`), one slot per sounding note per (part, staff, verse), so Tab also reaches notes whose syllable the OCR missed. Each commit PUTs the edit and re-renders (`reloadScoreXml()`, ~200 ms) keeping scroll and cursor. `leitor.lyricSlotAt(x, y)` is a test hook. In browser tests use `scrollIntoView({behavior: "instant"})`: `.stage` has smooth scrolling.
- Staff layout (`state.layout`, `"parts"` | `"combined"` | `"separate"`, saved per score in `localStorage["layout:<id>"]`): the "Juntar pautas" / "Separar pautas" header toggles, shown when `meta.combinable` / `meta.separable`; switching re-fetches the XML via `scoreXmlUrl()` and re-renders with `reloadScoreXml()` (also after renaming voices in the separate layout).
- Transposition (`state.transpose`, semitones, saved per score in `localStorage["transpose:<id>"]`) is applied at playback time (`n.midi + state.transpose` in the Part callback and `chase()`) and to the drawing via OSMD's `TransposeCalculator` + `osmd.Sheet.Transpose` (re-applied after every `load()`; re-render debounced). The analysis and the stored/downloaded MusicXML stay in the original key. Key names use `KEY_FOR_PITCH_CLASS`, the same table as OSMD's calculator, so the named key always matches the drawn one; in sharp keys `noteName()` spells black keys with sharps.
- `frontend/app.js` — vanilla JS, no build step. Tone.js: one `PolySynth` → `Tone.Channel` → `Tone.Part` per voice (solo/mute use `Channel.solo`/`mute`); times scheduled in ticks so tempo changes apply live. OpenSheetMusicDisplay renders `score.xml`; the cursor is synced by mapping (original measure index, offset in measure) to precomputed cursor steps, which keeps it correct across repeats.

## Claude Code setup

- `.claude/settings.json` is shared project config (committed): it enables the `frontend-design` plugin; use its skill for UI/visual work. Personal settings go in `.claude/settings.local.json` (git-ignored).

## Gotchas

- Audiveris 5.11 is installed by `dpkg-deb -x` (its postinst fails headless) and needs `libgtk-3` even in batch mode.
- Audiveris OCR uses Tesseract's legacy engine: needs the full `tessdata` models (in `/opt/tessdata`), not Ubuntu's LSTM-only packages. Without OCR, lyrics get misread as music symbols and note recognition gets worse.
- OCR'd titles/credits are garbage-prone: for OMR uploads the title is the filename, and OSMD is told not to draw titles/credits.
- Audiveris CLI can't open paths with non-ASCII characters (e.g. "Fiéis") in the container: copy to an ASCII name when running it by hand. The app is unaffected (it stores uploads as `source.<ext>`).
- `examples/Oh Vinde Fiéis/` and `examples/Cantam Anjos/096?…` are per-voice sheets (one PDF per voice) for the merge feature; `examples/Cantam Anjos/Cantam Anjos Harmonia.pdf` is the same hymn as a full score, handy to compare against the merged parts.
- `backend/tests/fixtures/cantam_anjos_audiveris.xml` is real Audiveris output for `examples/Cantam Anjos/Cantam Anjos Harmonia.pdf` (generated before OCR was enabled, so it includes a recognition error in m.2); the test checks SATB detection and the opening bass line.
- Frontend libs (Tone.js 15.0.4, OSMD 1.9.0) are downloaded into `/app/vendor` at image build time; versions are Dockerfile `ARG`s.
