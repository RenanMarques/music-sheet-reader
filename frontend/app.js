/* Leitor de Partituras: upload, score rendering (OSMD) and per-voice playback (Tone.js).
 *
 * The backend returns an "analysis" with already-separated monophonic voices
 * (times in quarter notes, repeats expanded). Each voice gets its own synth and
 * Tone.Channel, so solo/mute/volume work independently.
 */
(() => {
  "use strict";

  const COLORS = ["#c026d3", "#ea580c", "#059669", "#2563eb", "#dc2626", "#0891b2", "#7c3aed", "#475569"];
  const POLL_MS = 1500;

  // ------------------------------------------------------------------ settings
  // Persisted per browser under localStorage "settings". There is no settings screen yet;
  // change them from the browser console, e.g. leitor.set("noteNames", "solfege").

  const DEFAULT_SETTINGS = {
    noteNames: "letters", // "letters" → A3, G2 · "solfege" → Lá3, Sol2
    follow: true, // the score view follows playback
  };

  const NOTATIONS = {
    letters: {
      notes: ["C", "C♯", "D", "E♭", "E", "F", "F♯", "G", "A♭", "A", "B♭", "B"],
      majorKeys: ["C♭", "G♭", "D♭", "A♭", "E♭", "B♭", "F", "C", "G", "D", "A", "E", "B", "F♯", "C♯"],
    },
    solfege: {
      notes: ["Dó", "Dó♯", "Ré", "Mi♭", "Mi", "Fá", "Fá♯", "Sol", "Lá♭", "Lá", "Si♭", "Si"],
      majorKeys: ["Dó♭", "Sol♭", "Ré♭", "Lá♭", "Mi♭", "Si♭", "Fá", "Dó", "Sol", "Ré", "Lá", "Mi", "Si", "Fá♯", "Dó♯"],
    },
  };

  const settings = loadSettings();

  function loadSettings() {
    const out = { ...DEFAULT_SETTINGS };
    try {
      const saved = JSON.parse(localStorage.getItem("settings") || "{}");
      for (const k of Object.keys(DEFAULT_SETTINGS)) if (k in saved) out[k] = saved[k];
    } catch (_) { /* storage blocked or corrupt: use defaults */ }
    if (!(out.noteNames in NOTATIONS)) out.noteNames = DEFAULT_SETTINGS.noteNames;
    return out;
  }

  function saveSettings() {
    try { localStorage.setItem("settings", JSON.stringify(settings)); } catch (_) { /* storage blocked */ }
  }

  /** Changes a setting, saves it and refreshes what depends on it. */
  function setSetting(key, value) {
    if (!(key in DEFAULT_SETTINGS)) throw new Error(`Configuração desconhecida: ${key}. Opções: ${Object.keys(DEFAULT_SETTINGS).join(", ")}`);
    if (key === "noteNames" && !(value in NOTATIONS)) throw new Error(`Use um destes: ${Object.keys(NOTATIONS).join(", ")}`);
    if (key === "follow") { setFollow(Boolean(value)); return; }
    settings[key] = value;
    saveSettings();
    if (key === "noteNames") refreshNoteNames();
  }

  function noteName(midi) {
    return `${NOTATIONS[settings.noteNames].notes[midi % 12]}${Math.floor(midi / 12) - 1}`;
  }

  function keyName(fifths) {
    return `${NOTATIONS[settings.noteNames].majorKeys[fifths + 7] || "?"} maior`;
  }

  window.leitor = { settings, set: setSetting, defaults: DEFAULT_SETTINGS };

  const $ = (id) => document.getElementById(id);
  const transport = Tone.getTransport();
  const PPQ = transport.PPQ;

  const state = {
    id: null,
    analysis: null,
    osmd: null,
    voices: [], // {channel, synth, part, mute, solo, volume, el}
    steps: [],
    stepIdx: 0,
    seeking: false,
    pollTimer: null,
    loadToken: 0,
  };

  // ------------------------------------------------------------------ API

  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try { msg = (await res.json()).detail || msg; } catch (_) { /* not json */ }
      throw new Error(msg);
    }
    return res.status === 204 ? null : res.json();
  }

  // ------------------------------------------------------------------ views

  function show(view) {
    for (const v of ["empty", "status", "error", "player"]) $(v).hidden = v !== view;
    const loaded = view === "player";
    for (const id of ["mixer", "transport", "score-meta"]) $(id).hidden = !loaded;
  }

  // ------------------------------------------------------------------ library drawer

  const NARROW = window.matchMedia("(max-width: 1100px)");

  function setLibrary(open) {
    $("app").classList.toggle("lib-open", open);
    $("btn-library").setAttribute("aria-expanded", String(open));
  }

  function showError(msg) {
    $("error").textContent = msg;
    show("error");
  }

  // ------------------------------------------------------------------ score list / upload

  async function refreshList() {
    let scores = [];
    try { scores = await api("/api/scores"); } catch (_) { return; }
    const ul = $("score-list");
    ul.innerHTML = "";
    if (!scores.length) {
      ul.innerHTML = '<li class="muted">Nenhuma ainda.</li>';
      return;
    }
    for (const s of scores) {
      const li = document.createElement("li");
      li.className = "item" + (s.id === state.id ? " active" : "");
      const badge = { queued: "na fila", processing: "processando", error: "erro", done: `${s.voices} vozes` }[s.status] || s.status;
      li.innerHTML = `<span class="name"></span><span class="badge ${s.status === "error" ? "error" : ""}">${badge}</span>`;
      li.querySelector(".name").textContent = s.title || s.filename;
      li.title = s.filename;
      li.onclick = () => {
        location.hash = s.id;
        if (NARROW.matches) setLibrary(false);
      };
      if (s.status === "done" || s.status === "error") {
        const del = document.createElement("button");
        del.className = "del";
        del.textContent = "✕";
        del.title = "Remover";
        del.onclick = async (ev) => {
          ev.stopPropagation();
          await api(`/api/scores/${s.id}`, { method: "DELETE" });
          if (state.id === s.id) location.hash = "";
          refreshList();
        };
        li.appendChild(del);
      }
      ul.appendChild(li);
    }
  }

  async function upload(file) {
    const fd = new FormData();
    fd.append("file", file);
    show("status");
    $("status-title").textContent = `Enviando ${file.name}…`;
    $("status-text").textContent = "";
    try {
      const meta = await api("/api/scores", { method: "POST", body: fd });
      location.hash = meta.id;
      refreshList();
    } catch (e) {
      showError(`Falha no envio: ${e.message}`);
    }
  }

  function setupDropzone() {
    const dz = $("dropzone");
    const input = $("file-input");
    input.onchange = () => { if (input.files[0]) upload(input.files[0]); input.value = ""; };
    dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
    dz.addEventListener("dragleave", () => dz.classList.remove("over"));
    dz.addEventListener("drop", (e) => {
      e.preventDefault();
      dz.classList.remove("over");
      if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
    });
  }

  // ------------------------------------------------------------------ opening a score

  async function openScore(id) {
    const token = ++state.loadToken;
    clearTimeout(state.pollTimer);
    stop();
    disposeAudio();
    state.id = id || null;
    refreshList();
    if (!id) { show("empty"); return; }

    let meta;
    try { meta = await api(`/api/scores/${id}`); } catch (e) { showError(e.message); return; }
    if (token !== state.loadToken) return;

    if (meta.status === "queued" || meta.status === "processing") {
      show("status");
      $("status-title").textContent = meta.filename;
      $("status-text").textContent = meta.stage || "Processando…";
      state.pollTimer = setTimeout(() => { if (token === state.loadToken) openScore(id); }, POLL_MS);
      return;
    }
    if (meta.status === "error") {
      showError(`Não foi possível ler "${meta.filename}":\n${meta.error}`);
      refreshList();
      return;
    }

    show("status");
    $("status-title").textContent = meta.filename;
    $("status-text").textContent = "Carregando…";
    try {
      const [analysis, xml] = await Promise.all([
        api(`/api/scores/${id}/analysis`),
        fetch(`/api/scores/${id}/musicxml`).then((r) => r.text()),
      ]);
      if (token !== state.loadToken) return;
      state.analysis = analysis;
      renderHeader(meta, analysis);
      setupAudio(analysis);
      renderVoices(analysis);
      show("player");
      await renderScore(xml);
    } catch (e) {
      if (token === state.loadToken) showError(`Erro ao carregar: ${e.message}`);
    }
    refreshList();
  }

  function renderHeader(meta, a) {
    $("score-title").textContent = a.title;
    renderScoreInfo(a);
    $("link-source").href = `/api/scores/${meta.id}/source`;
    $("link-xml").href = `/api/scores/${meta.id}/musicxml`;
    $("tempo").value = Math.round(a.tempo);
    $("tempo-value").textContent = Math.round(a.tempo);
    $("seek").max = a.duration;
    $("seek").value = 0;
    requestAnimationFrame(() => renderSeekTicks(a));
    updatePosition(0);
  }

  function renderScoreInfo(a) {
    const staves = a.staves.map((s) => s.voices).join(" + ");
    $("score-info").textContent =
      `${a.voices.length} vozes (${staves} por pauta), ${a.measures} compassos, ${a.time_signature}, ${keyName(a.key_fifths)}`;
  }

  /** Re-labels everything that shows note names (after the notation setting changes). */
  function refreshNoteNames() {
    if (!state.analysis) return;
    renderScoreInfo(state.analysis);
    state.analysis.voices.forEach((v, i) => {
      const el = state.voices[i] && state.voices[i].el;
      if (!el) return;
      el.querySelector(".range").textContent = voiceRange(v);
      const now = el.querySelector(".now");
      if (now.dataset.midi) now.textContent = noteName(Number(now.dataset.midi));
    });
  }

  function voiceRange(v) {
    if (!v.notes.length) return "";
    let lo = Infinity, hi = -Infinity;
    for (const n of v.notes) { lo = Math.min(lo, n.midi); hi = Math.max(hi, n.midi); }
    return `${noteName(lo)}–${noteName(hi)}`;
  }

  // ------------------------------------------------------------------ score rendering + cursor

  async function renderScore(xml) {
    $("osmd").innerHTML = "";
    state.osmd = new opensheetmusicdisplay.OpenSheetMusicDisplay("osmd", {
      backend: "svg",
      autoResize: true,
      // OMR text (titles, credits, footers) is noisy; draw only notes and lyrics.
      drawTitle: false,
      drawSubtitle: false,
      drawComposer: false,
      drawLyricist: false,
      drawCredits: false,
      drawPartNames: false,
      followCursor: true,
    });
    state.osmd.FollowCursor = settings.follow;
    await state.osmd.load(xml);
    state.osmd.render();
    buildCursorSteps();
  }

  /** Records every cursor position as (measure index, offset in quarters inside the measure)
   * so the cursor can follow playback even when repeats reorder measures. */
  function buildCursorSteps() {
    const cursor = state.osmd.cursor;
    cursor.show();
    cursor.reset();
    const steps = [];
    const it = cursor.iterator;
    let guard = 0;
    while (!it.EndReached && guard++ < 20000) {
      const abs = it.currentTimeStamp.RealValue * 4;
      const mStart = it.CurrentMeasure ? it.CurrentMeasure.AbsoluteTimestamp.RealValue * 4 : abs;
      steps.push({ m: it.CurrentMeasureIndex, off: abs - mStart });
      cursor.next();
    }
    cursor.reset();
    state.steps = steps;
    state.stepIdx = 0;
  }

  // ------------------------------------------------------------------ follow playback

  /** Turns "follow the cursor" on/off. Turning it on scrolls straight to the current position. */
  function setFollow(on) {
    settings.follow = on;
    saveSettings();
    if (state.osmd) state.osmd.FollowCursor = on;
    const btn = $("btn-follow");
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-pressed", String(on));
    btn.title = on
      ? "Acompanhando a execução. Clique para soltar a tela (F)"
      : "Tela solta. Clique para voltar a acompanhar a execução (F)";
    const el = state.osmd && state.osmd.cursor && state.osmd.cursor.cursorElement;
    if (on && el && el.isConnected) el.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  /** Scrolling the score by hand while playing detaches the view, so it stops pulling back
   * to the cursor. Only user gestures count: OSMD's own scrolling fires plain "scroll". */
  function setupAutoDetach() {
    const stage = $("stage");
    const detach = () => { if (settings.follow && transport.state === "started") setFollow(false); };
    stage.addEventListener("wheel", detach, { passive: true });
    stage.addEventListener("touchmove", detach, { passive: true });
    stage.addEventListener("pointerdown", (e) => {
      // Grabbing the scrollbar (the area right of / below the content box).
      if (e.offsetX > stage.clientWidth || e.offsetY > stage.clientHeight) detach();
    });
    document.addEventListener("keydown", (e) => {
      if (["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown"].includes(e.key) &&
          !["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) detach();
    });
  }

  function moveCursor(measureIndex, offset) {
    const { steps, osmd } = state;
    if (!osmd || !steps.length) return;
    let target = -1;
    for (let i = 0; i < steps.length; i++) {
      const s = steps[i];
      if (s.m === measureIndex && s.off <= offset + 1e-6) target = i;
      else if (s.m === measureIndex && target >= 0) break;
    }
    if (target < 0 || target === state.stepIdx) return;
    const cursor = osmd.cursor;
    if (target < state.stepIdx) { cursor.reset(); state.stepIdx = 0; }
    while (state.stepIdx < target) { cursor.next(); state.stepIdx++; }
  }

  // ------------------------------------------------------------------ audio

  function makeSynth(type) {
    const oscillator = type === "fatsawtooth" ? { type, count: 3, spread: 18 } : { type };
    const synth = new Tone.PolySynth(Tone.Synth, {
      oscillator,
      envelope: { attack: 0.03, decay: 0.15, sustain: 0.75, release: 0.25 },
    });
    synth.volume.value = { triangle: -8, sine: -6, fatsawtooth: -20, square: -18, sawtooth: -18 }[type] ?? -10;
    return synth;
  }

  function setupAudio(a) {
    disposeAudio();
    transport.bpm.value = a.tempo;
    const timbre = $("timbre").value;
    a.voices.forEach((v, i) => {
      const channel = new Tone.Channel({ volume: 0 }).toDestination();
      const voice = { channel, synth: makeSynth(timbre).connect(channel), mute: false, solo: false, volume: 0, el: null };
      voice.part = new Tone.Part((time, n) => {
        const dur = Math.max(0.05, Tone.Ticks(n.dt).toSeconds() * 0.94);
        voice.synth.triggerAttackRelease(Tone.Frequency(n.midi, "midi").toFrequency(), dur, time);
        Tone.getDraw().schedule(() => showNote(i, n.midi), time);
      }, v.notes.map((n) => ({ time: `${Math.round(n.t * PPQ)}i`, dt: Math.round(n.d * PPQ), midi: n.midi })));
      voice.part.start(0);
      state.voices.push(voice);
    });
    applyLoop();
  }

  function disposeAudio() {
    transport.stop();
    transport.cancel();
    for (const v of state.voices) {
      v.part.dispose();
      v.synth.dispose();
      v.channel.dispose();
    }
    state.voices = [];
  }

  function changeTimbre() {
    const type = $("timbre").value;
    for (const v of state.voices) {
      const old = v.synth;
      v.synth = makeSynth(type).connect(v.channel);
      old.releaseAll();
      setTimeout(() => old.dispose(), 500);
    }
  }

  function applyLoop() {
    if (!state.analysis) return;
    transport.loop = $("loop").checked;
    transport.loopStart = 0;
    transport.loopEnd = `${Math.round(state.analysis.duration * PPQ)}i`;
  }

  function releaseAll() { for (const v of state.voices) v.synth.releaseAll(); }

  async function togglePlay() {
    if (!state.analysis) return;
    await Tone.start();
    if (transport.state === "started") {
      transport.pause();
      releaseAll();
      hidePick();
      updatePlayButton(false);
    } else {
      if (transport.ticks / PPQ >= state.analysis.duration) transport.ticks = 0;
      const when = Tone.now() + 0.05;
      transport.start(when);
      chase(transport.ticks / PPQ, when);
      // The start is scheduled 50 ms ahead, so transport.state still reads "stopped" here.
      updatePlayButton(true);
    }
  }

  function stop() {
    hidePick();
    transport.stop();
    transport.ticks = 0;
    releaseAll();
    updatePlayButton(false);
    if (state.analysis) updatePosition(0);
    for (let i = 0; i < state.voices.length; i++) showNote(i, null);
  }

  const ICON_PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l10.5-6.5z"/></svg>';
  const ICON_PAUSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6.5" y="5.5" width="4" height="13" rx="1"/><rect x="13.5" y="5.5" width="4" height="13" rx="1"/></svg>';

  function updatePlayButton(playing) {
    const btn = $("btn-play");
    btn.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
    btn.setAttribute("aria-label", playing ? "Pausar" : "Tocar");
    btn.title = playing ? "Pausar (espaço)" : "Tocar (espaço)";
    btn.classList.toggle("playing", playing);
  }

  // ------------------------------------------------------------------ mixer

  function renderVoices(a) {
    const box = $("voices");
    box.innerHTML = "";
    $("voices-title").textContent = `Vozes (${a.voices.length})`;
    a.voices.forEach((v, i) => {
      const el = document.createElement("div");
      el.className = "track";
      el.style.setProperty("--c", COLORS[i % COLORS.length]);
      el.innerHTML = `
        <div class="name"><span class="led"></span><strong></strong><span class="range"></span></div>
        <span class="now">—</span>
        <button class="m" aria-pressed="false">M</button>
        <button class="s" aria-pressed="false">S</button>
        <input type="range" min="-30" max="6" step="1" value="0">`;
      el.querySelector("strong").textContent = v.name;
      el.querySelector(".range").textContent = voiceRange(v);
      el.querySelector(".m").title = `Silenciar ${v.name}`;
      el.querySelector(".s").title = `Tocar só ${v.name} (combine várias com S)${i < 9 ? ` · tecla ${i + 1}` : ""}`;
      el.querySelector("input").title = `Volume de ${v.name}`;
      el.querySelector("input").setAttribute("aria-label", `Volume de ${v.name}`);
      el.querySelector(".s").onclick = () => toggleSolo(i);
      el.querySelector(".m").onclick = () => { state.voices[i].mute = !state.voices[i].mute; applyMixer(); };
      el.querySelector("input").oninput = (e) => { state.voices[i].volume = Number(e.target.value); applyMixer(); };
      state.voices[i].el = el;
      box.appendChild(el);
    });
    applyMixer();
  }

  function toggleSolo(i) {
    const v = state.voices[i];
    if (!v) return;
    v.solo = !v.solo;
    applyMixer();
  }

  function resetMixer() {
    state.voices.forEach((v) => { v.solo = false; v.mute = false; });
    applyMixer();
  }

  function applyMixer() {
    const anySolo = state.voices.some((v) => v.solo);
    for (const v of state.voices) {
      v.channel.solo = v.solo;
      v.channel.mute = v.mute;
      v.channel.volume.value = v.volume <= -30 ? -Infinity : v.volume;
      if (!v.el) continue;
      for (const [cls, on] of [[".s", v.solo], [".m", v.mute]]) {
        const b = v.el.querySelector(cls);
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", String(on));
      }
      v.el.classList.toggle("silent", v.mute || (anySolo && !v.solo));
    }
  }

  function showNote(i, midi) {
    const v = state.voices[i];
    if (!v || !v.el) return;
    const now = v.el.querySelector(".now");
    now.textContent = midi == null ? "—" : noteName(midi);
    now.dataset.midi = midi == null ? "" : String(midi);
    if (midi == null) return;
    const led = v.el.querySelector(".led");
    led.classList.remove("hit");
    void led.offsetWidth; // restart the animation
    led.classList.add("hit");
  }

  // ------------------------------------------------------------------ position / animation

  function segmentAt(q) {
    const tl = state.analysis.timeline;
    let lo = 0, hi = tl.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (tl[mid].start <= q + 1e-9) lo = mid; else hi = mid - 1;
    }
    return tl[lo];
  }

  function fmtTime(q) {
    const s = Math.max(0, q * 60 / transport.bpm.value);
    return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  }

  function updatePosition(q, { scroll = true } = {}) {
    const a = state.analysis;
    if (!a || !a.timeline.length) return;
    const seg = segmentAt(q);
    $("position").textContent = `Compasso ${seg.number} · ${fmtTime(q)} / ${fmtTime(a.duration)}`;
    if (!state.seeking) $("seek").value = q;
    const osmd = state.osmd;
    const follow = osmd && osmd.FollowCursor;
    if (!scroll && osmd) osmd.FollowCursor = false;
    try { moveCursor(seg.measure_index, q - seg.start); } finally { if (osmd) osmd.FollowCursor = follow; }
  }

  // ------------------------------------------------------------------ seeking + note chasing

  /** Moves playback to q (quarters). While playing, notes that would already be sounding
   * at q are re-triggered ("note chasing") so no voice goes silent until its next onset. */
  function seekTo(q, { scroll = false } = {}) {
    const a = state.analysis;
    if (!a) return;
    q = Math.max(0, Math.min(q, a.duration - 1e-3));
    releaseAll();
    transport.ticks = Math.round(q * PPQ);
    updatePosition(q, { scroll });
    if (transport.state === "started") chase(q, Tone.now() + 0.02);
  }

  function chase(q, when) {
    const a = state.analysis;
    if (!a) return;
    a.voices.forEach((v, i) => {
      const voice = state.voices[i];
      if (!voice) return;
      for (const n of v.notes) {
        if (n.t >= q - 1e-6) break; // notes are sorted by time; onsets at q are played by the Part
        const left = n.t + n.d - q;
        if (left <= 0.05) continue;
        const secs = Tone.Ticks(Math.round(left * PPQ)).toSeconds() * 0.94;
        voice.synth.triggerAttackRelease(Tone.Frequency(n.midi, "midi").toFrequency(), Math.max(0.05, secs), when);
        Tone.getDraw().schedule(() => showNote(i, n.midi), when);
      }
    });
  }

  /** Performance time of (measure, offset): with repeats a measure plays several times, so pick
   * the occurrence closest to the current position. */
  function timeOf(measureIndex, offset) {
    const now = transport.ticks / PPQ;
    let best = null;
    for (const seg of state.analysis.timeline) {
      if (seg.measure_index !== measureIndex) continue;
      const q = seg.start + Math.min(offset, Math.max(0, seg.length - 1e-3));
      if (best === null || Math.abs(q - now) < Math.abs(best.q - now)) best = { q, seg };
    }
    return best;
  }

  // ------------------------------------------------------------------ seek bar ruler

  function renderSeekTicks(a) {
    const box = $("seek-ticks");
    box.innerHTML = "";
    if (!a.duration) return;
    const width = box.getBoundingClientRect().width || 400;
    // Keep ticks at least ~6px apart; when thinned, mark every Nth measure.
    const every = Math.max(1, Math.ceil(a.timeline.length / Math.max(1, width / 6)));
    a.timeline.forEach((seg, i) => {
      if (i === 0 || i % every) return;
      const t = document.createElement("span");
      t.style.left = `${(seg.start / a.duration) * 100}%`;
      if (seg.measure_index < (a.timeline[i - 1] || seg).measure_index) t.className = "major"; // repeat jump
      box.appendChild(t);
    });
  }

  function showSeekBubble(q) {
    const a = state.analysis;
    const bubble = $("seek-bubble");
    if (!a || q == null) { bubble.hidden = true; return; }
    const ticks = $("seek-ticks");
    const x = ticks.offsetLeft + ticks.clientWidth * (q / a.duration);
    bubble.style.left = `${x}px`;
    bubble.textContent = `c. ${segmentAt(q).number}`;
    bubble.hidden = false;
  }

  // ------------------------------------------------------------------ click on the score to seek

  const HOVER = window.matchMedia("(hover: hover) and (pointer: fine)");
  const POP_TTL_MS = 3000;
  const pick = { down: null, timer: null, target: null };

  /** Bounding box (in OSMD units) of measure i across all staves of its system. */
  function measureBox(i) {
    const ml = state.osmd.GraphicSheet.MeasureList;
    const gms = (ml[i] || []).filter(Boolean);
    if (!gms.length) return null;
    const first = gms[0].PositionAndShape, last = gms[gms.length - 1].PositionAndShape;
    let x = first.AbsolutePosition.x;
    const right = x + first.Size.width;
    // The first measure of a system also owns the clef/key area to its left.
    const sys = gms[0].ParentMusicSystem;
    const prev = (ml[i - 1] || []).find(Boolean);
    if (sys && (!prev || prev.ParentMusicSystem !== sys)) x = sys.PositionAndShape.AbsolutePosition.x;
    const top = first.AbsolutePosition.y - 3;
    const bottom = last.AbsolutePosition.y + 4 + 3; // staff height is 4 units; 3 units for ledger notes
    return { x, y: top, w: right - x, h: bottom - top };
  }

  /** Maps a pointer position to {measure, offset, box}; exact=true snaps to the nearest note column. */
  function hitTest(clientX, clientY, exact) {
    const osmd = state.osmd;
    const svg = osmd && $("osmd").querySelector("svg");
    if (!svg || !state.analysis) return null;
    const r = svg.getBoundingClientRect();
    const unit = 10 * osmd.zoom;
    const ux = (clientX - r.left) / unit, uy = (clientY - r.top) / unit;
    const n = osmd.GraphicSheet.MeasureList.length;
    for (let i = 0; i < n; i++) {
      const box = measureBox(i);
      if (!box || ux < box.x || ux >= box.x + box.w || uy < box.y || uy >= box.y + box.h) continue;
      let offset = 0;
      if (exact) {
        let bestDx = Infinity;
        for (const gm of osmd.GraphicSheet.MeasureList[i].filter(Boolean)) {
          for (const se of gm.staffEntries || []) {
            const dx = Math.abs(se.PositionAndShape.AbsolutePosition.x - ux);
            if (dx < bestDx) { bestDx = dx; offset = se.relInMeasureTimestamp.RealValue * 4; }
          }
        }
      }
      return { measure: i, offset, box, unit, svgRect: r };
    }
    return null;
  }

  /** Places an overlay element over a measure box (overlays live in .sheet, positioned relative to it). */
  function placeOver(el, hit) {
    const sheet = $("sheet").getBoundingClientRect();
    const { box, unit, svgRect } = hit;
    el.style.left = `${svgRect.left - sheet.left + box.x * unit}px`;
    el.style.top = `${svgRect.top - sheet.top + box.y * unit}px`;
    el.style.width = `${box.w * unit}px`;
    el.style.height = `${box.h * unit}px`;
    el.hidden = false;
  }

  /** Screen rectangle of measure i (used by browser tests). */
  function measureRect(i) {
    const svg = state.osmd && $("osmd").querySelector("svg");
    const box = svg && measureBox(i);
    if (!box) return null;
    const r = svg.getBoundingClientRect(), unit = 10 * state.osmd.zoom;
    return { left: r.left + box.x * unit, top: r.top + box.y * unit, width: box.w * unit, height: box.h * unit };
  }
  window.leitor.measureRect = measureRect;

  function hidePick() {
    clearTimeout(pick.timer);
    pick.target = null;
    $("measure-pick").hidden = true;
    $("jump-pop").hidden = true;
  }

  /** While playing, a click only proposes the jump; this confirms it. */
  function confirmJump() {
    if (!pick.target) return;
    const { q } = pick.target;
    hidePick();
    seekTo(q);
  }

  function setupScorePicking() {
    const sheet = $("sheet");
    const hover = $("measure-hover");
    let raf = 0;

    sheet.addEventListener("pointermove", (e) => {
      if (!HOVER.matches || e.target.closest(".jump-pop")) return;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const hit = hitTest(e.clientX, e.clientY, false);
        sheet.classList.toggle("can-pick", Boolean(hit));
        if (hit) placeOver(hover, hit); else hover.hidden = true;
      });
    });
    sheet.addEventListener("pointerleave", () => { hover.hidden = true; sheet.classList.remove("can-pick"); });

    sheet.addEventListener("pointerdown", (e) => { pick.down = { x: e.clientX, y: e.clientY }; });
    sheet.addEventListener("click", (e) => {
      if (e.target.closest(".jump-pop")) return;
      const d = pick.down;
      pick.down = null;
      // Ignore drags, text selections and non-primary buttons.
      if (e.button !== 0 || !d || Math.hypot(e.clientX - d.x, e.clientY - d.y) > 6) return;
      if (String(window.getSelection() || "").trim()) return;
      const hit = hitTest(e.clientX, e.clientY, e.altKey);
      if (!hit) { hidePick(); return; }
      const target = timeOf(hit.measure, hit.offset);
      if (!target) return;

      if (transport.state !== "started") {
        // Paused/stopped: just move the position, nothing starts playing.
        hidePick();
        seekTo(target.q);
        return;
      }
      // Playing: propose the jump instead of interrupting the rehearsal.
      pick.target = target;
      placeOver($("measure-pick"), hit);
      const pop = $("jump-pop");
      const where = hit.offset > 0 ? `compasso ${target.seg.number}, tempo ${Math.floor(hit.offset) + 1}` : `compasso ${target.seg.number}`;
      pop.innerHTML = "";
      pop.append(`▶ Ir para o ${where}`);
      const bar = document.createElement("span");
      bar.className = "time-left";
      bar.style.setProperty("--ttl", `${POP_TTL_MS}ms`);
      pop.appendChild(bar);
      const sheetRect = sheet.getBoundingClientRect();
      pop.style.left = `${e.clientX - sheetRect.left}px`;
      pop.style.top = `${hit.svgRect.top - sheetRect.top + hit.box.y * hit.unit}px`;
      pop.hidden = false;
      clearTimeout(pick.timer);
      pick.timer = setTimeout(hidePick, POP_TTL_MS);
    });

    $("jump-pop").addEventListener("click", confirmJump);
    // Hovering the proposal keeps it alive; leaving restarts a short timeout.
    $("jump-pop").addEventListener("pointerenter", () => clearTimeout(pick.timer));
    $("jump-pop").addEventListener("pointerleave", () => { pick.timer = setTimeout(hidePick, 1500); });
  }

  function tick() {
    if (state.analysis && transport.state === "started") {
      const q = transport.ticks / PPQ;
      if (!transport.loop && q >= state.analysis.duration) {
        stop();
      } else {
        updatePosition(q);
      }
    }
    requestAnimationFrame(tick);
  }

  // ------------------------------------------------------------------ wiring

  function setupControls() {
    $("btn-play").onclick = togglePlay;
    $("btn-stop").onclick = stop;
    $("seek").addEventListener("input", (e) => {
      state.seeking = true;
      const q = Number(e.target.value);
      releaseAll();
      transport.ticks = Math.round(q * PPQ);
      updatePosition(q);
      showSeekBubble(q);
    });
    $("seek").addEventListener("change", (e) => {
      state.seeking = false;
      showSeekBubble(null);
      if (transport.state === "started") chase(Number(e.target.value), Tone.now() + 0.02);
    });
    window.addEventListener("resize", () => { if (state.analysis) renderSeekTicks(state.analysis); });
    $("tempo").oninput = (e) => {
      transport.bpm.value = Number(e.target.value);
      $("tempo-value").textContent = e.target.value;
      updatePosition(transport.ticks / PPQ);
    };
    $("timbre").onchange = changeTimbre;
    $("loop").onchange = applyLoop;
    $("btn-all").onclick = resetMixer;
    $("btn-follow").onclick = () => setFollow(!settings.follow);
    $("btn-library").onclick = () => setLibrary(!$("app").classList.contains("lib-open"));
    document.addEventListener("keydown", (e) => {
      if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName) && e.target.type !== "range") return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === "Escape" && pick.target) {
        hidePick();
      } else if (e.key === "Enter" && pick.target && e.target.tagName !== "BUTTON") {
        e.preventDefault();
        confirmJump();
      } else if (e.code === "Space" && e.target.tagName !== "BUTTON") {
        e.preventDefault();
        togglePlay();
      } else if (e.code === "KeyF") {
        setFollow(!settings.follow);
      } else if (/^Digit[1-9]$/.test(e.code)) {
        toggleSolo(Number(e.code.slice(5)) - 1);
      }
    });
    window.addEventListener("hashchange", () => openScore(location.hash.slice(1)));
  }

  setupDropzone();
  setupControls();
  setLibrary(!NARROW.matches || !location.hash.slice(1));
  updatePlayButton(false);
  setFollow(settings.follow);
  setupAutoDetach();
  setupScorePicking();
  openScore(location.hash.slice(1));
  requestAnimationFrame(tick);
})();
