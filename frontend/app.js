/* Leitor de Partituras: upload, score rendering (OSMD) and per-voice playback (Tone.js).
 *
 * The backend returns an "analysis" with already-separated monophonic voices
 * (times in quarter notes, repeats expanded). Each voice gets its own synth and
 * Tone.Channel, so solo/mute/volume work independently.
 */
(() => {
  "use strict";

  const COLORS = ["#c026d3", "#ea580c", "#059669", "#2563eb", "#dc2626", "#0891b2", "#7c3aed", "#475569"];
  const NOTE_NAMES = ["Dó", "Dó♯", "Ré", "Mi♭", "Mi", "Fá", "Fá♯", "Sol", "Lá♭", "Lá", "Si♭", "Si"];
  const POLL_MS = 1500;

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
    const staves = a.staves.map((s) => s.voices).join(" + ");
    $("score-info").textContent =
      `${a.voices.length} vozes (${staves} por pauta), ${a.measures} compassos, ${a.time_signature}, ${keyName(a.key_fifths)}`;
    $("link-source").href = `/api/scores/${meta.id}/source`;
    $("link-xml").href = `/api/scores/${meta.id}/musicxml`;
    $("tempo").value = Math.round(a.tempo);
    $("tempo-value").textContent = Math.round(a.tempo);
    $("seek").max = a.duration;
    $("seek").value = 0;
    updatePosition(0);
  }

  function keyName(fifths) {
    const major = ["Dó♭", "Sol♭", "Ré♭", "Lá♭", "Mi♭", "Si♭", "Fá", "Dó", "Sol", "Ré", "Lá", "Mi", "Si", "Fá♯", "Dó♯"];
    return `${major[fifths + 7] || "?"} maior`;
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
      updatePlayButton(false);
    } else {
      if (transport.ticks / PPQ >= state.analysis.duration) transport.ticks = 0;
      transport.start("+0.05");
      // The start is scheduled 50 ms ahead, so transport.state still reads "stopped" here.
      updatePlayButton(true);
    }
  }

  function stop() {
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
      el.querySelector(".range").textContent = `${v.low}–${v.high}`;
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
    v.el.querySelector(".now").textContent =
      midi == null ? "—" : `${NOTE_NAMES[midi % 12]}${Math.floor(midi / 12) - 1}`;
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

  function updatePosition(q) {
    const a = state.analysis;
    if (!a || !a.timeline.length) return;
    const seg = segmentAt(q);
    $("position").textContent = `Compasso ${seg.number} · ${fmtTime(q)} / ${fmtTime(a.duration)}`;
    if (!state.seeking) $("seek").value = q;
    moveCursor(seg.measure_index, q - seg.start);
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
    });
    $("seek").addEventListener("change", () => { state.seeking = false; });
    $("tempo").oninput = (e) => {
      transport.bpm.value = Number(e.target.value);
      $("tempo-value").textContent = e.target.value;
      updatePosition(transport.ticks / PPQ);
    };
    $("timbre").onchange = changeTimbre;
    $("loop").onchange = applyLoop;
    $("btn-all").onclick = resetMixer;
    $("btn-library").onclick = () => setLibrary(!$("app").classList.contains("lib-open"));
    document.addEventListener("keydown", (e) => {
      if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName) && e.target.type !== "range") return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.code === "Space" && e.target.tagName !== "BUTTON") {
        e.preventDefault();
        togglePlay();
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
  openScore(location.hash.slice(1));
  requestAnimationFrame(tick);
})();
