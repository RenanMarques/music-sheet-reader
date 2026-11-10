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
      sharpNotes: ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"],
      majorKeys: ["C♭", "G♭", "D♭", "A♭", "E♭", "B♭", "F", "C", "G", "D", "A", "E", "B", "F♯", "C♯"],
    },
    solfege: {
      notes: ["Dó", "Dó♯", "Ré", "Mi♭", "Mi", "Fá", "Fá♯", "Sol", "Lá♭", "Lá", "Si♭", "Si"],
      sharpNotes: ["Dó", "Dó♯", "Ré", "Ré♯", "Mi", "Fá", "Fá♯", "Sol", "Sol♯", "Lá", "Lá♯", "Si"],
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

  /** Note name for the UI. In sharp keys (as drawn, i.e. after transposition) black keys are
   * spelled with sharps, matching the score; otherwise the mixed default (C♯ E♭ F♯ A♭ B♭). */
  function noteName(midi) {
    const n = NOTATIONS[settings.noteNames];
    const a = state.analysis;
    const sharpKey = a && transposeFifths(a.key_fifths, state.transpose) > 0;
    return `${(sharpKey ? n.sharpNotes : n.notes)[midi % 12]}${Math.floor(midi / 12) - 1}`;
  }

  function keyName(fifths) {
    return `${NOTATIONS[settings.noteNames].majorKeys[fifths + 7] || "?"} maior`;
  }

  // Key signature (fifths) for each tonic pitch class, C=0 … B=11. Same table as OSMD's
  // TransposeCalculator, so the key we name always matches the key it draws.
  const KEY_FOR_PITCH_CLASS = [0, -5, 2, -3, 4, -1, 6, 1, -4, 3, -2, 5];

  /** Fifths of a major key transposed by `semitones`. */
  function transposeFifths(fifths, semitones) {
    const tonic = (((fifths * 7) % 12) + 12) % 12;
    return KEY_FOR_PITCH_CLASS[(((tonic + semitones) % 12) + 12) % 12];
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
    transpose: 0, // semitones applied to playback and to the drawn score (per score, saved locally)
    layout: "parts", // "parts" | "combined" | "separate" staves (per score, saved locally)
    hidden: new Set(), // ids of voices left out of the drawing (per score, saved locally)
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
    for (const v of ["empty", "compose", "status", "error", "player"]) $(v).hidden = v !== view;
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

  // ------------------------------------------------------------------ new score (compose)
  // Files are collected here (in the browser) and only sent when the user clicks "Processar":
  // one file without a voice = a whole score; otherwise one file per voice, combined into one
  // score. A voice left on "Automático" is identified by the server (printed text, pitches).

  const COMPOSE_ROUTE = "nova";
  const ACCEPTED = /\.(pdf|png|jpe?g|tiff?|bmp|gif|xml|musicxml|mxl)$/i;
  const MAX_FILES = 12;
  const VOICE_CHOICES = ["Soprano", "Mezzo-soprano", "Contralto", "Tenor", "Barítono", "Baixo"];
  const OTHER = "__other";
  const compose = { files: [] }; // {file, voice: "" | one of VOICE_CHOICES | OTHER, other: ""}

  function openCompose(files) {
    if (files && files.length) addComposeFiles(files);
    if (location.hash.slice(1) === COMPOSE_ROUTE) showCompose();
    else location.hash = COMPOSE_ROUTE;
    if (NARROW.matches) setLibrary(false);
  }

  function showCompose() {
    show("compose");
    renderCompose();
  }

  function addComposeFiles(files) {
    const rejected = [];
    for (const file of files) {
      if (!ACCEPTED.test(file.name)) { rejected.push(file.name); continue; }
      if (compose.files.some((f) => f.file.name === file.name && f.file.size === file.size)) continue;
      if (compose.files.length >= MAX_FILES) { rejected.push(`${file.name} (máximo de ${MAX_FILES} arquivos)`); continue; }
      compose.files.push({ file, voice: "", other: "" });
    }
    composeError(rejected.length ? `Não adicionado: ${rejected.join(", ")}. Use PDF, imagem ou MusicXML.` : "");
    if (!$("compose").hidden) renderCompose();
  }

  function composeError(msg) {
    $("compose-error").textContent = msg;
    $("compose-error").hidden = !msg;
  }

  function formatSize(bytes) {
    return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function renderCompose() {
    const list = $("compose-list");
    list.replaceChildren(...compose.files.map((entry, i) => {
      const li = document.createElement("li");
      li.className = "compose-file";
      li.innerHTML = `
        <div class="file"><strong></strong><small></small></div>
        <div class="voice"><select></select><input class="other" type="text" maxlength="40" placeholder="Nome da voz" hidden></div>
        <button type="button" class="remove">✕</button>`;
      li.querySelector("strong").textContent = entry.file.name;
      li.querySelector("strong").title = entry.file.name;
      li.querySelector("small").textContent = formatSize(entry.file.size);
      const select = li.querySelector("select");
      select.setAttribute("aria-label", `Voz de ${entry.file.name}`);
      for (const [value, label] of [["", "Automático"], ...VOICE_CHOICES.map((v) => [v, v]), [OTHER, "Outra…"]]) {
        select.add(new Option(label, value, false, value === entry.voice));
      }
      const other = li.querySelector("input");
      other.value = entry.other;
      other.setAttribute("aria-label", `Nome da voz de ${entry.file.name}`);
      const sync = () => {
        select.classList.toggle("auto", !entry.voice);
        other.hidden = entry.voice !== OTHER;
      };
      sync();
      select.onchange = () => {
        entry.voice = select.value;
        sync();
        if (entry.voice === OTHER) other.focus();
        renderComposeNote();
      };
      other.oninput = () => { entry.other = other.value; };
      const remove = li.querySelector(".remove");
      remove.title = `Remover ${entry.file.name}`;
      remove.setAttribute("aria-label", remove.title);
      remove.onclick = () => {
        compose.files.splice(i, 1);
        renderCompose();
        (list.querySelectorAll(".remove")[Math.min(i, compose.files.length - 1)] || $("compose-drop")).focus();
      };
      return li;
    }));
    renderComposeNote();
  }

  function renderComposeNote() {
    const n = compose.files.length;
    const note = $("compose-note");
    if (n === 0) note.textContent = "";
    else if (n === 1 && !compose.files[0].voice) {
      note.textContent = "Um arquivo: as vozes são separadas a partir das pautas. Escolha uma voz só se o arquivo tiver uma voz apenas.";
    } else {
      note.textContent = `${n === 1 ? "Um arquivo com uma voz" : `${n} arquivos, um por voz`}: viram uma só partitura, com as vozes ` +
        "da mais aguda para a mais grave. As que ficarem em Automático são identificadas pelo nome impresso na " +
        "partitura ou comparando as alturas; dá para renomear e reordenar depois, no painel de vozes.";
    }
    $("compose-submit").disabled = n === 0;
  }

  async function submitCompose() {
    const entries = compose.files;
    if (!entries.length) return;
    const missing = entries.find((e) => e.voice === OTHER && !e.other.trim());
    if (missing) { composeError(`Dê um nome à voz de ${missing.file.name} ou escolha outra opção.`); return; }
    const fd = new FormData();
    const single = entries.length === 1 && !entries[0].voice;
    for (const e of entries) {
      fd.append(single ? "file" : "files", e.file);
      if (!single) fd.append("voices", e.voice === OTHER ? e.other.trim() : e.voice);
    }
    const title = $("compose-title").value.trim();
    if (title) fd.append("title", title);
    const btn = $("compose-submit");
    btn.disabled = true;
    btn.textContent = "Enviando…";
    composeError("");
    try {
      const meta = await api("/api/scores", { method: "POST", body: fd });
      compose.files = [];
      $("compose-title").value = "";
      location.hash = meta.id;
      refreshList();
    } catch (e) {
      composeError(`Falha no envio: ${e.message}`);
    } finally {
      btn.textContent = "Processar";
      renderComposeNote();
    }
  }

  function cancelCompose() {
    compose.files = [];
    $("compose-title").value = "";
    composeError("");
    location.hash = "";
  }

  function setupDropzone() {
    const dz = $("dropzone");
    dz.onclick = () => openCompose();
    $("btn-empty-new").onclick = () => openCompose();
    const drop = $("compose-drop");
    const input = $("compose-input");
    input.onchange = () => { addComposeFiles(input.files); input.value = ""; };
    for (const [zone, onFiles] of [[dz, openCompose], [drop, addComposeFiles]]) {
      zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
      zone.addEventListener("dragleave", () => zone.classList.remove("over"));
      zone.addEventListener("drop", (e) => {
        e.preventDefault();
        zone.classList.remove("over");
        onFiles(e.dataTransfer.files);
      });
    }
    $("compose-submit").onclick = submitCompose;
    $("compose-cancel").onclick = cancelCompose;
  }

  // ------------------------------------------------------------------ opening a score

  async function openScore(id) {
    const token = ++state.loadToken;
    clearTimeout(state.pollTimer);
    if (state.editing) setEditing(false);
    stop();
    disposeAudio();
    state.id = id && id !== COMPOSE_ROUTE ? id : null;
    refreshList();
    if (id === COMPOSE_ROUTE) { showCompose(); return; }
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
      const layout = loadLayout(id, meta);
      const hidden = loadHidden(id, meta.voices);
      const [analysis, xml] = await Promise.all([
        api(`/api/scores/${id}/analysis`),
        fetch(scoreXmlUrl(id, layout, hidden), { cache: "no-cache" }).then((r) => r.text()),
      ]);
      if (token !== state.loadToken) return;
      state.analysis = analysis;
      state.meta = meta;
      state.layout = layout;
      state.hidden = hidden;
      renderLayoutButton();
      state.transpose = loadTranspose(id);
      setEditing(false);
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
    renderKeyControl();
    renderSources(meta);
    $("link-xml").href = `/api/scores/${meta.id}/musicxml`;
    transport.bpm.value = a.tempo; // before updatePosition(): the total time depends on it
    $("tempo").value = Math.round(a.tempo);
    $("tempo-value").textContent = Math.round(a.tempo);
    $("seek").max = a.duration;
    $("seek").value = 0;
    requestAnimationFrame(() => renderSeekTicks(a));
    updatePosition(0);
  }

  const HOW = {
    "escolhida": "escolhida no envio",
    "texto da partitura": "lida no texto da partitura",
    "alturas": "identificada comparando as alturas",
  };

  /** "Ver original", or one link per uploaded file for a score combined from one file per voice,
   *  plus how its voices were identified and what was patched (octaves, missing measures). */
  function renderSources(meta) {
    const parts = meta.sources || [];
    $("link-source").hidden = parts.length > 1;
    $("link-source").href = `/api/scores/${meta.id}/source`;
    const box = $("source-parts");
    box.hidden = parts.length <= 1;
    box.replaceChildren("Originais:");
    const voicesOf = (p) => p.voices || (p.name ? [{ name: p.name, abbr: p.abbr }] : []);
    const notes = [];
    parts.forEach((p, i) => {
      const a = document.createElement("a");
      a.href = `/api/scores/${meta.id}/source?part=${i}`;
      a.target = "_blank";
      a.rel = "noopener";
      const vs = voicesOf(p);
      a.textContent = vs.map((v) => v.abbr || v.name).join("/") || String(i + 1);
      a.title = p.filename;
      box.append(" ", a);
      for (const v of vs) if (v.how) notes.push(`${v.name}: ${HOW[v.how] || v.how} (${p.filename})`);
    });
    const warnings = meta.warnings || [];
    const box2 = $("merge-notes");
    box2.hidden = !notes.length && !warnings.length;
    box2.querySelector("summary").textContent = warnings.length
      ? `Como as vozes foram identificadas (e ${warnings.length} ${warnings.length === 1 ? "ajuste" : "ajustes"} na leitura)`
      : "Como as vozes foram identificadas";
    box2.querySelector("ul").replaceChildren(...[...notes, ...warnings].map((w) => {
      const li = document.createElement("li");
      li.textContent = w;
      return li;
    }));
  }

  function renderScoreInfo(a) {
    const staves = a.staves.map((s) => s.voices).join(" + ");
    const key = state.transpose
      ? `${keyName(transposeFifths(a.key_fifths, state.transpose))} (original: ${keyName(a.key_fifths)})`
      : keyName(a.key_fifths);
    $("score-info").textContent =
      `${a.voices.length} vozes (${staves} por pauta), ${a.measures} compassos, ${a.time_signature}, ${key}`;
  }

  /** Re-labels everything that shows note or key names (notation setting or transposition changed). */
  function refreshNoteNames() {
    if (!state.analysis) return;
    renderScoreInfo(state.analysis);
    renderKeyControl();
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
    return `${noteName(lo + state.transpose)}–${noteName(hi + state.transpose)}`;
  }

  // ------------------------------------------------------------------ transposition

  const TRANSPOSE_LIMIT = 12; // semitones, each way
  const SELECT_RANGE = 6; // the key list offers −6…+6

  // ------------------------------------------------------------------ staff layout
  // "parts": the staves as stored. "combined": same-clef neighbouring voices (S+C, T+B) share a
  // staff. "separate": each voice of a shared staff gets its own staff (the voices of the mixer).
  // Both drawn by the backend (app/layout.py). Drawing only: playback, the mixer and the stored
  // MusicXML don't change. Saved per score.

  const LAYOUTS = { combined: "combinable", separate: "separable" }; // layout -> meta flag

  function loadLayout(id, meta) {
    let saved = null;
    try { saved = localStorage.getItem(`layout:${id}`); } catch (_) { /* private mode */ }
    return LAYOUTS[saved] && meta[LAYOUTS[saved]] ? saved : "parts";
  }

  function scoreXmlUrl(id, layout, hidden) {
    const q = new URLSearchParams();
    if (LAYOUTS[layout]) q.set("layout", layout);
    if (hidden && hidden.size) q.set("hide", [...hidden].sort().join(","));
    const qs = q.toString();
    return `/api/scores/${id}/musicxml` + (qs ? `?${qs.replace(/%2C/g, ",")}` : "");
  }

  // ------------------------------------------------------------------ hidden voices
  // The eye button of a mixer row leaves that voice out of the drawing (`hide=` on the MusicXML
  // request, see app/layout.py draw()); it still plays. A voice sharing a staff with others is
  // drawn alone on its staff when another one is hidden. Saved per score; at least one voice
  // always stays visible.

  function loadHidden(id, count) {
    try {
      const ids = JSON.parse(localStorage.getItem(`hidden:${id}`) || "[]");
      const ok = ids.filter((v) => typeof v === "string" && /^v\d+$/.test(v) && Number(v.slice(1)) <= count);
      return ok.length < count ? new Set(ok) : new Set();
    } catch (_) { return new Set(); }
  }

  async function toggleHidden(voiceId) {
    if (!state.osmd || !state.id) return;
    if (state.hidden.has(voiceId)) state.hidden.delete(voiceId);
    else if (state.hidden.size < state.analysis.voices.length - 1) state.hidden.add(voiceId);
    else return;
    try { localStorage.setItem(`hidden:${state.id}`, JSON.stringify([...state.hidden])); } catch (_) { /* private mode */ }
    renderHiddenButtons();
    closeLyricEditor();
    await reloadScoreXml();
  }

  function renderHiddenButtons() {
    const last = state.hidden.size >= state.analysis.voices.length - 1;
    state.analysis.voices.forEach((info, i) => {
      const el = state.voices[i] && state.voices[i].el;
      if (!el) return;
      const hidden = state.hidden.has(info.id);
      const btn = el.querySelector(".v");
      btn.innerHTML = hidden ? ICON_EYE_OFF : ICON_EYE;
      btn.classList.toggle("on", hidden);
      btn.setAttribute("aria-pressed", String(hidden));
      btn.disabled = !hidden && last;
      btn.title = hidden ? `Mostrar ${info.name} na partitura`
        : last ? "Pelo menos uma voz fica na partitura" : `Ocultar ${info.name} da partitura (continua tocando)`;
      btn.setAttribute("aria-label", hidden ? `Mostrar ${info.name} na partitura` : `Ocultar ${info.name} da partitura`);
      el.classList.toggle("offscore", hidden);
    });
  }

  /** A part of the separated layout is one voice of an original (part, staff): `P1__s1v2`. */
  function sourcePart(part) {
    const m = /^(.*)__s([^v]+)v(\d+)$/.exec(part);
    return m ? { part: m[1], staff: m[2], voice: Number(m[3]) } : null;
  }

  function renderLayoutButton() {
    const buttons = { combined: ["btn-layout", "Pautas juntas", "Juntar pautas"], separate: ["btn-separate", "Pautas separadas", "Separar pautas"] };
    for (const [layout, [id, on, off]] of Object.entries(buttons)) {
      const btn = $(id);
      btn.hidden = !(state.meta && state.meta[LAYOUTS[layout]]);
      const active = state.layout === layout;
      btn.setAttribute("aria-pressed", String(active));
      btn.textContent = active ? on : off;
    }
  }

  async function toggleLayout(layout) {
    if (!state.osmd || !state.id) return;
    state.layout = state.layout === layout ? "parts" : layout;
    try { localStorage.setItem(`layout:${state.id}`, state.layout); } catch (_) { /* private mode */ }
    renderLayoutButton();
    closeLyricEditor();
    await reloadScoreXml();
  }

  function loadTranspose(id) {
    try { return Math.max(-TRANSPOSE_LIMIT, Math.min(TRANSPOSE_LIMIT, Number(localStorage.getItem(`transpose:${id}`)) || 0)); }
    catch (_) { return 0; }
  }

  function signed(n) { return n > 0 ? `+${n}` : n < 0 ? `−${-n}` : "0"; }

  function renderKeyControl() {
    const a = state.analysis;
    if (!a) return;
    const select = $("key-select");
    const t = state.transpose;
    const offsets = [];
    for (let o = SELECT_RANGE; o >= -SELECT_RANGE; o--) offsets.push(o);
    if (!offsets.includes(t)) offsets.unshift(t);
    offsets.sort((x, y) => y - x);
    select.innerHTML = "";
    for (const o of offsets) {
      const opt = document.createElement("option");
      opt.value = String(o);
      const name = keyName(transposeFifths(a.key_fifths, o)).replace(" maior", "");
      opt.textContent = o === 0 ? `${name} (original)` : `${name} ${signed(o)}`;
      opt.selected = o === t;
      select.appendChild(opt);
    }
    select.title = t ? `Tonalidade: ${signed(t)} semitons em relação à original` : "Tonalidade original";
    // Compact label for the closed select: key name + small "original" or offset.
    const label = $("key-label");
    label.innerHTML = "";
    const name = document.createElement("span");
    name.textContent = keyName(transposeFifths(a.key_fifths, t)).replace(" maior", "");
    const small = document.createElement("small");
    small.textContent = t ? signed(t) : "original";
    label.append(name, small);
    // Size the select to the label (+ room for the arrow), not to its longest option.
    select.style.width = "";
    requestAnimationFrame(() => { select.style.width = `${Math.ceil(label.scrollWidth) + 26}px`; });
    $("key-control").classList.toggle("transposed", t !== 0);
    $("btn-key-down").disabled = t <= -TRANSPOSE_LIMIT;
    $("btn-key-up").disabled = t >= TRANSPOSE_LIMIT;
  }

  let transposeRenderTimer = 0;

  /** Changes the transposition: audio immediately, the drawn score shortly after (debounced,
   * so several quick clicks only re-render once). */
  function setTranspose(semitones) {
    if (!state.analysis) return;
    const t = Math.max(-TRANSPOSE_LIMIT, Math.min(TRANSPOSE_LIMIT, semitones));
    if (t === state.transpose) return;
    const delta = t - state.transpose;
    state.transpose = t;
    // The "current note" labels hold sounding (transposed) pitches: shift them too.
    for (const v of state.voices) {
      const now = v.el && v.el.querySelector(".now");
      if (now && now.dataset.midi) now.dataset.midi = String(Number(now.dataset.midi) + delta);
    }
    try {
      if (t) localStorage.setItem(`transpose:${state.id}`, String(t));
      else localStorage.removeItem(`transpose:${state.id}`);
    } catch (_) { /* storage blocked */ }
    refreshNoteNames();
    if (transport.state === "started") {
      // Re-sound what is playing now at the new pitch.
      releaseAll();
      chase(transport.ticks / PPQ, Tone.now() + 0.02);
    }
    clearTimeout(transposeRenderTimer);
    transposeRenderTimer = setTimeout(rerenderTransposed, 120);
  }

  function rerenderTransposed() {
    const osmd = state.osmd;
    if (!osmd || !osmd.Sheet) return;
    const stage = $("stage");
    const top = stage.scrollTop;
    osmd.Sheet.Transpose = state.transpose;
    osmd.updateGraphic();
    osmd.render();
    buildCursorSteps();
    updatePosition(transport.ticks / PPQ, { scroll: false });
    stage.scrollTop = top;
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
    state.osmd.TransposeCalculator = new opensheetmusicdisplay.TransposeCalculator();
    await state.osmd.load(xml);
    state.osmd.Sheet.Transpose = state.transpose;
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
    a.voices.forEach((v) => {
      const channel = new Tone.Channel({ volume: 0 }).toDestination();
      const voice = { channel, synth: makeSynth(timbre).connect(channel), mute: false, solo: false, volume: 0, el: null };
      voice.part = new Tone.Part((time, n) => {
        const dur = Math.max(0.05, Tone.Ticks(n.dt).toSeconds() * 0.94);
        const midi = n.midi + state.transpose;
        voice.synth.triggerAttackRelease(Tone.Frequency(midi, "midi").toFrequency(), dur, time);
        Tone.getDraw().schedule(() => showNote(voice, midi), time);
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
    for (const v of state.voices) showNote(v, null);
  }

  const ICON_EYE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/></svg>';
  const ICON_EYE_OFF = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/><path d="M4 20 20 4"/></svg>';
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
    endVoiceEdit();
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
        <button class="v" aria-pressed="false"></button>
        <button class="m" aria-pressed="false">M</button>
        <button class="s" aria-pressed="false">S</button>
        <input type="range" min="-30" max="6" step="1" value="0">`;
      el.querySelector("strong").textContent = v.name;
      el.querySelector(".range").textContent = voiceRange(v);
      el.querySelector(".m").title = `Silenciar ${v.name}`;
      el.querySelector(".s").title = `Tocar só ${v.name} (combine várias com S)${i < 9 ? ` · tecla ${i + 1}` : ""}`;
      el.querySelector("input").value = String(state.voices[i].volume);
      el.querySelector("input").title = `Volume de ${v.name}`;
      el.querySelector("input").setAttribute("aria-label", `Volume de ${v.name}`);
      el.querySelector(".s").onclick = () => toggleSolo(i);
      el.querySelector(".m").onclick = () => { state.voices[i].mute = !state.voices[i].mute; applyMixer(); };
      el.querySelector(".v").onclick = () => toggleHidden(v.id);
      el.querySelector("input").oninput = (e) => { state.voices[i].volume = Number(e.target.value); applyMixer(); };
      state.voices[i].el = el;
      box.appendChild(el);
    });
    applyMixer();
    renderHiddenButtons();
  }

  // ------------------------------------------------------------------ renaming / reordering voices
  // Metadata only (PUT /voices): the score and the audio stay as they are. Reordering moves the
  // voice objects (channel, synth, mixer state) together with their analysis entries.

  let voiceEdit = null; // [{voice, info, name}] while editing

  function startVoiceEdit() {
    if (!state.analysis) return;
    voiceEdit = state.analysis.voices.map((info, i) => ({ voice: state.voices[i], info, name: info.name }));
    for (const id of ["btn-edit-voices", "btn-all"]) $(id).hidden = true;
    for (const id of ["btn-voices-cancel", "btn-voices-save"]) $(id).hidden = false;
    renderVoiceEditor();
    const first = $("voices").querySelector("input");
    if (first) first.focus();
  }

  function endVoiceEdit() {
    voiceEdit = null;
    for (const id of ["btn-edit-voices", "btn-all"]) $(id).hidden = false;
    for (const id of ["btn-voices-cancel", "btn-voices-save"]) $(id).hidden = true;
  }

  function renderVoiceEditor(focus) {
    const box = $("voices");
    box.replaceChildren(...voiceEdit.map((e, i) => {
      const el = document.createElement("div");
      el.className = "track edit";
      el.style.setProperty("--c", COLORS[i % COLORS.length]);
      el.innerHTML = `
        <input class="vname" type="text" maxlength="40">
        <button class="up">▲</button>
        <button class="down">▼</button>
        <small class="auto" hidden></small>`;
      const input = el.querySelector("input");
      input.value = e.name;
      input.setAttribute("aria-label", `Nome da voz ${i + 1}`);
      input.placeholder = e.info.auto_name || "";
      const auto = el.querySelector(".auto");
      const syncAuto = () => {
        const changed = e.info.auto_name && e.name.trim() && e.name.trim() !== e.info.auto_name;
        auto.hidden = !changed;
        auto.textContent = changed ? `Identificada como ${e.info.auto_name}` : "";
      };
      syncAuto();
      input.oninput = () => { e.name = input.value; syncAuto(); };
      input.onkeydown = (ev) => {
        if (ev.key === "Enter") { ev.preventDefault(); saveVoiceEdit(); }
        if (ev.key === "Escape") { ev.preventDefault(); renderVoices(state.analysis); }
      };
      const move = (delta, cls) => {
        const j = i + delta;
        [voiceEdit[i], voiceEdit[j]] = [voiceEdit[j], voiceEdit[i]];
        renderVoiceEditor({ index: j, cls });
      };
      const up = el.querySelector(".up");
      const down = el.querySelector(".down");
      up.disabled = i === 0;
      down.disabled = i === voiceEdit.length - 1;
      up.title = `Subir ${e.name || e.info.auto_name}`;
      down.title = `Descer ${e.name || e.info.auto_name}`;
      up.setAttribute("aria-label", up.title);
      down.setAttribute("aria-label", down.title);
      up.onclick = () => move(-1, ".up");
      down.onclick = () => move(1, ".down");
      return el;
    }));
    if (focus) {
      const row = box.children[focus.index];
      const btn = row && row.querySelector(focus.cls);
      (btn && !btn.disabled ? btn : row && row.querySelector("input")).focus();
    }
  }

  async function saveVoiceEdit() {
    if (!voiceEdit) return;
    const edited = voiceEdit.map((e) => ({ ...e, name: e.name.trim() || e.info.auto_name || e.info.name }));
    const body = { voices: edited.map((e) => ({ id: e.info.id, name: e.name === e.info.auto_name ? "" : e.name })) };
    const btn = $("btn-voices-save");
    btn.disabled = true;
    try {
      await api(`/api/scores/${state.id}/voices`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
    } catch (err) {
      $("voices-title").textContent = `Não foi possível salvar: ${err.message}`;
      return;
    } finally {
      btn.disabled = false;
    }
    state.analysis.voices = edited.map((e) => ({ ...e.info, name: e.name }));
    state.voices = edited.map((e) => e.voice);
    renderVoices(state.analysis);
    // Separated staves (separate layout, or next to a hidden voice) carry the voice names.
    if (state.layout === "separate" || state.hidden.size) await reloadScoreXml();
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

  /** Shows the note a voice is playing in its mixer row (`v` is the voice object, so the
   *  indicator follows the voice when the mixer is reordered). */
  function showNote(v, midi) {
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
        const midi = n.midi + state.transpose;
        voice.synth.triggerAttackRelease(Tone.Frequency(midi, "midi").toFrequency(), Math.max(0.05, secs), when);
        Tone.getDraw().schedule(() => showNote(voice, midi), when);
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

  // ------------------------------------------------------------------ lyric editing

  /** Editable positions ("slots") of every lyric line, from OSMD's graphical model.
   * A line is (part, staff, verse); it has one slot per sounding note onset of that staff,
   * with or without a syllable, so Tab can also reach notes whose syllable the OCR missed.
   * Slot boxes are in OSMD units; the backend addresses notes by the same
   * (part, staff, measure index, offset in quarters) key. */
  function lyricLines() {
    const osmd = state.osmd;
    if (!osmd || !osmd.GraphicSheet) return new Map();
    const lines = new Map();
    const rowY = new Map(); // `${system}|${part}|${staff}|${verse}` -> label top (units)
    const entries = [];
    const topSplit = new Map(); // original `${part}|${staff}` -> top drawn voice number
    for (const inst of osmd.Sheet.Instruments) {
      const sp = sourcePart(inst.IdString);
      const k = sp && `${sp.part}|${sp.staff}`;
      if (sp && !(topSplit.get(k) <= sp.voice)) topSplit.set(k, sp.voice);
    }
    osmd.GraphicSheet.MeasureList.forEach((staves, mi) => {
      staves.forEach((gm) => {
        if (!gm) return;
        const staffObj = gm.ParentStaff;
        let part = staffObj.ParentInstrument.IdString;
        let staff = String(staffObj.ParentInstrument.Staves.indexOf(staffObj) + 1);
        // Separated staves (separate layout, or a staff with hidden voices): a lyric edit
        // addresses the original (part, staff); its syllables are drawn on the top drawn voice's
        // staff (app/layout.py), so only that one offers slots.
        const split = sourcePart(part);
        if (split) {
          if (split.voice !== topSplit.get(`${split.part}|${split.staff}`)) return;
          ({ part, staff } = split);
        }
        const system = gm.ParentMusicSystem ? gm.ParentMusicSystem.Id : 0;
        for (const se of gm.staffEntries || []) {
          // On a combined staff only the top voice (voice 1) carries lyrics: its notes are the
          // ones a lyric edit addresses in the stored score (same part id, see app/layout.py).
          const sounding = se.sourceStaffEntry.VoiceEntries.some((ve) =>
            (state.layout !== "combined" || ve.ParentVoice.VoiceId === 1) && ve.Notes.some((n) => !n.isRest()));
          const lyrics = {};
          for (const gle of se.LyricsEntries || []) {
            const le = gle.LyricsEntry;
            const ps = gle.GraphicalLabel.PositionAndShape;
            const box = {
              x: ps.AbsolutePosition.x + ps.BorderLeft, y: ps.AbsolutePosition.y + ps.BorderTop,
              w: ps.BorderRight - ps.BorderLeft, h: ps.BorderBottom - ps.BorderTop,
            };
            const syl = le.Word ? le.Word.Syllables : [le];
            lyrics[le.VerseNumber] = { text: le.Text, continues: syl.indexOf(le) < syl.length - 1, box };
            rowY.set(`${system}|${part}|${staff}|${le.VerseNumber}`, box);
          }
          const sp = se.PositionAndShape;
          entries.push({
            part, staff, system, measure: mi, offset: se.relInMeasureTimestamp.RealValue * 4,
            x: sp.AbsolutePosition.x + (sp.BorderLeft + sp.BorderRight) / 2, sounding, lyrics,
            staffBottom: gm.PositionAndShape.AbsolutePosition.y + 4,
          });
        }
      });
    });
    // Verses per staff, then one ordered slot list per line.
    const verses = new Map();
    for (const e of entries) for (const v of Object.keys(e.lyrics)) {
      const k = `${e.part}|${e.staff}`;
      if (!verses.has(k)) verses.set(k, new Set());
      verses.get(k).add(v);
    }
    for (const e of entries) {
      for (const verse of verses.get(`${e.part}|${e.staff}`) || []) {
        if (!e.sounding && !e.lyrics[verse]) continue;
        const key = `${e.part}|${e.staff}|${verse}`;
        if (!lines.has(key)) lines.set(key, []);
        const ly = e.lyrics[verse];
        const row = rowY.get(`${e.system}|${e.part}|${e.staff}|${verse}`);
        lines.get(key).push({
          key, part: e.part, staff: e.staff, verse, measure: e.measure, offset: e.offset,
          text: ly ? ly.text : "", continues: ly ? ly.continues : false,
          box: ly ? ly.box : { x: e.x - 1.5, y: row ? row.y : e.staffBottom + 2 + 2.6 * (Number(verse) - 1), w: 3, h: row ? row.h : 2 },
        });
      }
    }
    for (const slots of lines.values()) slots.sort((a, b) => a.measure - b.measure || a.offset - b.offset);
    return lines;
  }

  function slotBox(slot) {
    const pad = 0.4;
    return { x: slot.box.x - pad, y: slot.box.y - pad, w: slot.box.w + 2 * pad, h: slot.box.h + 2 * pad };
  }

  /** The slot under the pointer: a syllable's own box, or an empty note position in a lyric row. */
  function lyricSlotAt(clientX, clientY) {
    const svg = state.osmd && $("osmd").querySelector("svg");
    if (!svg) return null;
    const r = svg.getBoundingClientRect(), unit = 10 * state.osmd.zoom;
    const ux = (clientX - r.left) / unit, uy = (clientY - r.top) / unit;
    let best = null, bestD = Infinity;
    for (const slots of lyricLines().values()) {
      for (const s of slots) {
        const b = slotBox(s);
        if (uy < b.y || uy > b.y + b.h) continue;
        const inside = ux >= b.x && ux <= b.x + b.w;
        const d = inside ? 0 : Math.min(Math.abs(ux - b.x), Math.abs(ux - b.x - b.w));
        if (d < bestD && d < 2) { bestD = d; best = s; }
      }
    }
    return best;
  }

  /** Positions an overlay over a box given in OSMD units. */
  function placeBox(el, box) {
    const svg = $("osmd").querySelector("svg");
    const sheet = $("sheet").getBoundingClientRect();
    const r = svg.getBoundingClientRect(), unit = 10 * state.osmd.zoom;
    el.style.left = `${r.left - sheet.left + box.x * unit}px`;
    el.style.top = `${r.top - sheet.top + box.y * unit}px`;
    el.style.width = `${box.w * unit}px`;
    el.style.height = `${box.h * unit}px`;
    el.hidden = false;
  }

  const lyricEdit = { slot: null, saving: false };

  function openLyricEditor(slot) {
    const input = $("lyric-input");
    const svg = $("osmd").querySelector("svg");
    const sheet = $("sheet").getBoundingClientRect();
    const r = svg.getBoundingClientRect(), unit = 10 * state.osmd.zoom;
    lyricEdit.slot = slot;
    input.value = slot.text + (slot.continues ? "-" : "");
    input.style.left = `${r.left - sheet.left + (slot.box.x + slot.box.w / 2) * unit}px`;
    input.style.top = `${r.top - sheet.top + (slot.box.y - 0.35) * unit}px`;
    input.style.height = `${(slot.box.h + 0.7) * unit}px`;
    input.style.fontSize = `${slot.box.h * unit * 0.9}px`;
    input.style.width = `${Math.max(3.5, input.value.length + 2)}ch`;
    input.disabled = false;
    input.hidden = false;
    $("measure-hover").hidden = true;
    input.focus();
    input.select();
  }

  function closeLyricEditor() {
    lyricEdit.slot = null;
    $("lyric-input").hidden = true;
  }

  function editStatus(text, error = false) {
    const el = $("edit-status");
    el.textContent = text;
    el.classList.toggle("error", error);
  }

  /** Saves the open syllable (if changed); then optionally opens the next/previous slot. */
  async function commitLyric(step = 0) {
    const slot = lyricEdit.slot;
    if (!slot || lyricEdit.saving) return;
    const input = $("lyric-input");
    const value = input.value.trim();
    const changed = value !== (slot.text + (slot.continues ? "-" : ""));
    const go = () => {
      if (!step) { closeLyricEditor(); return; }
      const slots = lyricLines().get(slot.key) || [];
      const i = slots.findIndex((s) => s.measure === slot.measure && Math.abs(s.offset - slot.offset) < 1e-6);
      const next = slots[i + step];
      if (next) openLyricEditor(next); else closeLyricEditor();
    };
    if (!changed) { go(); return; }
    lyricEdit.saving = true;
    input.disabled = true;
    editStatus("Salvando…");
    try {
      const meta = await api(`/api/scores/${state.id}/lyrics`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ edits: [{ part: slot.part, staff: slot.staff, measure: slot.measure, offset: slot.offset, verse: slot.verse, text: value }] }),
      });
      state.meta = meta;
      await reloadScoreXml();
      editStatus("Salvo");
      updateRestoreButton();
      lyricEdit.saving = false;
      go();
    } catch (e) {
      lyricEdit.saving = false;
      input.disabled = false;
      input.focus();
      editStatus(`Não foi possível salvar: ${e.message}`, true);
    }
  }

  /** Re-renders the score after a lyric change, keeping the scroll and playback cursor. */
  let reloadSeq = 0;

  async function reloadScoreXml() {
    const stage = $("stage");
    const top = stage.scrollTop;
    const seq = ++reloadSeq;
    const xml = await fetch(scoreXmlUrl(state.id, state.layout, state.hidden), { cache: "no-store" }).then((r) => r.text());
    if (seq !== reloadSeq) return; // a newer reload (e.g. another voice hidden meanwhile) wins
    await state.osmd.load(xml);
    state.osmd.Sheet.Transpose = state.transpose;
    state.osmd.render();
    buildCursorSteps();
    updatePosition(transport.ticks / PPQ, { scroll: false });
    stage.scrollTop = top;
  }

  function updateRestoreButton() {
    const btn = $("btn-restore-lyrics");
    btn.hidden = !(state.meta && state.meta.lyrics_edited);
    btn.classList.remove("confirm");
    btn.textContent = "Restaurar letra original";
  }

  function setEditing(on) {
    state.editing = on;
    $("app").classList.toggle("editing", on);
    $("edit-bar").hidden = !on;
    $("btn-edit-lyrics").setAttribute("aria-pressed", String(on));
    $("measure-hover").hidden = true;
    hidePick();
    closeLyricEditor();
    editStatus("");
    updateRestoreButton();
  }

  function setupLyricEditing() {
    $("btn-edit-lyrics").onclick = () => setEditing(!state.editing);
    $("btn-layout").onclick = () => toggleLayout("combined");
    $("btn-separate").onclick = () => toggleLayout("separate");
    $("btn-done-lyrics").onclick = () => setEditing(false);
    const input = $("lyric-input");
    input.addEventListener("keydown", (e) => {
      e.stopPropagation(); // keep play/solo shortcuts out of the text field
      if (e.key === "Enter") { e.preventDefault(); commitLyric(0); }
      else if (e.key === "Tab") { e.preventDefault(); commitLyric(e.shiftKey ? -1 : 1); }
      else if (e.key === "Escape") { e.preventDefault(); closeLyricEditor(); }
    });
    input.addEventListener("input", () => { input.style.width = `${Math.max(3.5, input.value.length + 2)}ch`; });
    input.addEventListener("blur", () => {
      // Clicking elsewhere saves, like most inline editors (unless a save is already running).
      setTimeout(() => { if (lyricEdit.slot && document.activeElement !== input) commitLyric(0); }, 0);
    });
    let confirmTimer = 0;
    $("btn-restore-lyrics").onclick = async () => {
      const btn = $("btn-restore-lyrics");
      if (!btn.classList.contains("confirm")) {
        btn.classList.add("confirm");
        btn.textContent = "Desfazer todas as correções?";
        clearTimeout(confirmTimer);
        confirmTimer = setTimeout(updateRestoreButton, 4000);
        return;
      }
      clearTimeout(confirmTimer);
      closeLyricEditor();
      try {
        state.meta = await api(`/api/scores/${state.id}/lyrics/restore`, { method: "POST" });
        await reloadScoreXml();
        editStatus("Letra original restaurada");
      } catch (e) {
        editStatus(`Não foi possível restaurar: ${e.message}`, true);
      }
      updateRestoreButton();
    };
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
  window.leitor.lyricSlotAt = (x, y) => { const s = lyricSlotAt(x, y); return s && { ...s, box: { ...s.box } }; };

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
      if (!HOVER.matches || e.target.closest(".jump-pop, .lyric-input")) return;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        if (state.editing) {
          const slot = lyricSlotAt(e.clientX, e.clientY);
          if (slot) placeBox(hover, slotBox(slot)); else hover.hidden = true;
          return;
        }
        const hit = hitTest(e.clientX, e.clientY, false);
        sheet.classList.toggle("can-pick", Boolean(hit));
        if (hit) placeOver(hover, hit); else hover.hidden = true;
      });
    });
    sheet.addEventListener("pointerleave", () => { hover.hidden = true; sheet.classList.remove("can-pick"); });

    sheet.addEventListener("pointerdown", (e) => { pick.down = { x: e.clientX, y: e.clientY }; });
    sheet.addEventListener("click", (e) => {
      if (e.target.closest(".jump-pop, .lyric-input")) return;
      const d = pick.down;
      pick.down = null;
      // Ignore drags, text selections and non-primary buttons.
      if (e.button !== 0 || !d || Math.hypot(e.clientX - d.x, e.clientY - d.y) > 6) return;
      if (String(window.getSelection() || "").trim()) return;
      if (state.editing) {
        const slot = lyricSlotAt(e.clientX, e.clientY);
        if (slot) openLyricEditor(slot);
        return;
      }
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
    $("btn-edit-voices").onclick = startVoiceEdit;
    $("btn-voices-save").onclick = saveVoiceEdit;
    $("btn-voices-cancel").onclick = () => renderVoices(state.analysis);
    $("btn-follow").onclick = () => setFollow(!settings.follow);
    $("btn-key-down").onclick = () => setTranspose(state.transpose - 1);
    $("btn-key-up").onclick = () => setTranspose(state.transpose + 1);
    $("key-select").onchange = (e) => { setTranspose(Number(e.target.value)); e.target.blur(); };
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
      } else if (e.key === "+" || e.key === "=" || e.code === "NumpadAdd") {
        setTranspose(state.transpose + 1);
      } else if (e.key === "-" || e.code === "NumpadSubtract") {
        setTranspose(state.transpose - 1);
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
  setupLyricEditing();
  openScore(location.hash.slice(1));
  requestAnimationFrame(tick);
})();
