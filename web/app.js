"use strict";
const $ = (id) => document.getElementById(id);
const api = (path, body) =>
  fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
               body: JSON.stringify(body || {})}).then(r => r.json());
const get = (path) => fetch(path).then(r => r.json());

let STATE = {};

/* ----------------------------------------------------------- tabs */
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  $(t.dataset.tab).classList.add("active");
});

/* ----------------------------------------------------------- audio + visualizer */
const player = $("player");
let audioCtx, analyser, srcNode, vizData, vizRunning = false;

function ensureAudioGraph() {
  if (audioCtx) return;
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  srcNode = audioCtx.createMediaElementSource(player);
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 512;
  analyser.smoothingTimeConstant = 0.7;
  srcNode.connect(analyser);
  analyser.connect(audioCtx.destination);
  vizData = new Uint8Array(analyser.frequencyBinCount);
}

function startAudio() {
  ensureAudioGraph();
  if (audioCtx.state === "suspended") audioCtx.resume();
  player.src = "/audio?t=" + Date.now();
  player.play().catch(() => {});
  if (!vizRunning) { vizRunning = true; drawViz(); }
}
function stopAudio() {
  player.pause();
  player.removeAttribute("src");
  player.load();
}

const canvas = $("viz"), cx = canvas.getContext("2d");
function drawViz() {
  requestAnimationFrame(drawViz);
  const W = canvas.width = canvas.clientWidth, H = canvas.height;
  cx.clearRect(0, 0, W, H);
  if (!analyser || player.paused) return;
  analyser.getByteFrequencyData(vizData);
  const bars = 40, step = Math.floor(vizData.length * 0.7 / bars);
  const bw = W / bars;
  cx.fillStyle = "#27d0a8";
  for (let i = 0; i < bars; i++) {
    let sum = 0;
    for (let j = 0; j < step; j++) sum += vizData[i * step + j];
    const v = (sum / step) / 255;
    const h = v * H;
    cx.fillRect(i * bw + 1, H - h, bw - 2, h);
  }
}

/* ----------------------------------------------------------- state render */
function render(s) {
  STATE = s;
  if (document.activeElement !== $("freq"))
    $("freq").value = s.freq;
  $("am").checked = s.am;
  $("unit").textContent = s.am ? "kHz" : "MHz";
  $("status").textContent = s.status || "";
  $("nowplaying").textContent = s.now_playing || "—";

  toggleBtn($("listen"), s.listen_on, "■ Stop", "▶ Listen");
  $("listen").classList.toggle("on", s.listen_on);
  toggleBtn($("record"), s.recording, "■ Stop Rec", "● Record");
  $("record").classList.toggle("rec-on", s.recording);
  toggleBtn($("transcribe"), s.tx_on, "Stop", "Transcribe");
  $("transcribe").classList.toggle("on", s.tx_on);

  $("eqOn").checked = s.eq_on;
  if (document.activeElement !== $("eqProfile") && $("eqProfile").options.length) $("eqProfile").value = s.eq;
  $("eqB").value = s.eq_b; $("eqM").value = s.eq_m; $("eqT").value = s.eq_t;

  toggleBtn($("timeshift"), s.timeshift_on, "Stop TS", "Timeshift");
  toggleBtn($("stream"), s.stream_on, "Stop", "Stream");
  toggleBtn($("log"), s.log_on, "Stop", "24/7 Log");
  $("streamInfo").textContent = s.stream_on ? (s.stream_info || "") : "";

  // keep the browser <audio> element in sync with the engine
  const wantAudio = s.listen_on;
  if (wantAudio && player.paused) startAudio();
  if (!wantAudio && !player.paused) stopAudio();
}
function toggleBtn(btn, on, onText, offText) { btn.textContent = on ? onText : offText; }

/* populate EQ profile select once */
function fillEq(profiles) {
  const sel = $("eqProfile");
  if (sel.options.length) return;
  profiles.forEach(p => { const o = document.createElement("option"); o.value = o.textContent = p; sel.appendChild(o); });
}

/* ----------------------------------------------------------- SSE */
function connectSSE() {
  const es = new EventSource("/events");
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "state") { fillEq(ev.state.eq_profiles || []); render(ev.state); }
    else if (ev.type === "transcript") appendTranscript(ev.text);
    else if (ev.type === "scan_start") { $("scanList").innerHTML = ""; $("scanStatus").textContent = "scanning…"; }
    else if (ev.type === "scan_progress") {
      $("scanBar").style.width = (100 * ev.i / ev.total) + "%";
      $("scanStatus").textContent = `scanning ${ev.freq} ${ev.unit} (${ev.i}/${ev.total})`;
    }
    else if (ev.type === "scan_done") renderScan(ev);
  };
  es.onerror = () => { $("status").textContent = "reconnecting…"; };
}

/* ----------------------------------------------------------- tuner */
$("tune").onclick = () => api("/api/tune", {freq: parseFloat($("freq").value), am: $("am").checked});
$("freq").onkeydown = (e) => { if (e.key === "Enter") $("tune").click(); };
$("stepUp").onclick = () => api("/api/step", {dir: 1});
$("stepDown").onclick = () => api("/api/step", {dir: -1});
$("seekUp").onclick = () => api("/api/seek", {up: true});
$("seekDown").onclick = () => api("/api/seek", {up: false});
$("am").onchange = () => api("/api/band", {am: $("am").checked});

/* ----------------------------------------------------------- controls */
$("listen").onclick = () => { ensureAudioGraph(); api("/api/listen", {on: !STATE.listen_on}); };
$("record").onclick = () => api("/api/record", {on: !STATE.recording});
$("transcribe").onclick = () => api("/api/transcribe", {on: !STATE.tx_on});
$("songid").onclick = () => api("/api/songid", {});

/* ----------------------------------------------------------- EQ */
const pushEq = () => api("/api/eq", {
  on: $("eqOn").checked, profile: $("eqProfile").value,
  bass: +$("eqB").value, mid: +$("eqM").value, treble: +$("eqT").value });
["eqOn", "eqProfile", "eqB", "eqM", "eqT"].forEach(id => $(id).onchange = pushEq);

/* ----------------------------------------------------------- presets */
function loadPresets() {
  get("/api/presets").then(pr => {
    const ul = $("presets"); ul.innerHTML = "";
    Object.entries(pr).forEach(([name, v]) => {
      const li = document.createElement("li");
      const span = document.createElement("span");
      span.className = "grow";
      span.textContent = `${name} — ${v.freq} ${v.am ? "AM" : "FM"}`;
      span.onclick = () => api("/api/preset", {action: "tune", name});
      const x = document.createElement("span");
      x.className = "x"; x.textContent = "✕";
      x.onclick = (e) => { e.stopPropagation(); api("/api/preset", {action: "remove", name}).then(loadPresets); };
      li.append(span, x); ul.appendChild(li);
    });
  });
}
$("savePreset").onclick = () => {
  const name = $("presetName").value.trim();
  if (!name) return;
  api("/api/preset", {action: "add", name, freq: STATE.freq, am: STATE.am})
    .then(() => { $("presetName").value = ""; loadPresets(); });
};

/* ----------------------------------------------------------- scan */
$("scanFM").onclick = () => api("/api/scan", {am: false});
$("scanAM").onclick = () => api("/api/scan", {am: true});
function renderScan(ev) {
  $("scanBar").style.width = "100%";
  $("scanStatus").textContent = `done — ${ev.stations.length} stations (floor ${ev.floor.toFixed(0)} dB)`;
  const ul = $("scanList"); ul.innerHTML = "";
  if (!ev.stations.length) { ul.innerHTML = "<li>no clear stations — check the antenna</li>"; return; }
  ev.stations.forEach(st => {
    const li = document.createElement("li");
    const bars = "█".repeat(Math.min(12, Math.max(1, Math.round(st.lift))));
    li.textContent = `${st.freq} ${ev.unit}  ${bars}`;
    li.onclick = () => api("/api/tune", {freq: st.freq, am: ev.am});
    ul.appendChild(li);
  });
}

/* ----------------------------------------------------------- LED */
document.querySelectorAll(".led").forEach(b => b.onclick = () => {
  const k = b.dataset.led, br = +$("ledBright").value;
  const map = {
    red: {red: true}, blue: {red: false, blue: 110}, purple: {red: true, blue: 90},
    pulse: {pulse: 96}, off: {red: false, blue: 0} };
  api("/api/led", map[k]);
});
$("ledBright").oninput = () => api("/api/led", {blue: +$("ledBright").value});

/* ----------------------------------------------------------- tools */
$("recTimed").onclick = () => api("/api/record-timed", {seconds: +$("recSecs").value, fmt: $("recFmt").value}).then(loadRecordings);
$("stream").onclick = () => api("/api/stream", {on: !STATE.stream_on, port: +$("streamPort").value});
$("log").onclick = () => api("/api/log", {on: !STATE.log_on, segment: +$("logSeg").value});
$("timeshift").onclick = () => {
  const on = !STATE.timeshift_on;
  api("/api/timeshift", {on, buffer_min: +$("tsMin").value}).then(() => {
    if (on) startTimeshift(); else $("tsPlayerCard").hidden = true;
  });
};
let hls;
function startTimeshift() {
  $("tsPlayerCard").hidden = false;
  const audio = $("tsPlayer"), url = "/ts/ts.m3u8";
  setTimeout(() => {
    if (audio.canPlayType("application/vnd.apple.mpegurl")) { audio.src = url; }
    else if (window.Hls && Hls.isSupported()) {
      if (hls) hls.destroy();
      hls = new Hls(); hls.loadSource(url); hls.attachMedia(audio);
    }
  }, 2500);  // let the buffer fill
}

/* ----------------------------------------------------------- transcript */
function appendTranscript(t) {
  const el = $("transcript");
  el.textContent += t;
  el.scrollTop = el.scrollHeight;
}
function applyTxStyle() {
  const el = $("transcript");
  el.style.fontFamily = $("txFont").value;
  el.style.fontSize = $("txSize").value + "px";
  el.style.color = $("txFg").value;
  el.style.background = $("txBg").value;
  localStorage.setItem("txStyle", JSON.stringify({
    font: $("txFont").value, size: $("txSize").value, fg: $("txFg").value, bg: $("txBg").value}));
}
["txFont", "txSize", "txFg", "txBg"].forEach(id => { $(id).oninput = applyTxStyle; });
(function restoreTx() {
  const s = JSON.parse(localStorage.getItem("txStyle") || "null");
  if (s) { $("txFont").value = s.font; $("txSize").value = s.size; $("txFg").value = s.fg; $("txBg").value = s.bg; }
  applyTxStyle();
})();

/* ----------------------------------------------------------- schedule */
$("schAdd").onclick = () => api("/api/schedule", {
  action: "add", name: $("schName").value, at: $("schAt").value,
  dur: +$("schDur").value, repeat: $("schRepeat").value }).then(r => $("schList").textContent = r.text || "(none)");
function loadSchedule() { get("/api/schedule").then(r => $("schList").textContent = r.text || "(none)"); }

/* ----------------------------------------------------------- recordings */
function loadRecordings() {
  get("/api/recordings").then(list => {
    const ul = $("recList"); ul.innerHTML = "";
    if (!list.length) { ul.innerHTML = "<li class='muted'>none yet</li>"; return; }
    list.forEach(r => {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.className = "grow"; a.href = "/rec/" + r.name; a.textContent = r.name;
      a.style.color = "var(--accent)"; a.download = "";
      const sz = document.createElement("span");
      sz.className = "muted"; sz.textContent = (r.size / 1048576).toFixed(1) + " MB";
      li.append(a, sz); ul.appendChild(li);
    });
  });
}
$("refreshRec").onclick = loadRecordings;

/* ----------------------------------------------------------- boot */
get("/api/state").then(s => { fillEq(s.eq_profiles || []); render(s); });
connectSSE();
loadPresets();
loadSchedule();
loadRecordings();
