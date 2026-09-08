// Study Buddy — bank captures, compact them, then ask.
//
// The browser is the eye: getDisplayMedia gives us the window, and each press of Capture
// grabs one frame and posts it. Frames are 1400px wide because interfaces are dense
// 11-13px text and a menu label that survives at 800px is mush at 600.
//
// Captures are posted through a SERIAL queue, never in parallel. The server assigns the
// sequence number on arrival, and the order captures were taken in is load-bearing —
// it's what lets the reader recognise the fourth shot as the same page scrolled further
// rather than a new screen. Two requests racing would silently transpose them.

const $ = (id) => document.getElementById(id);
const state = { subject: null, shots: [], sealed: false, stale: false, sharing: false };

// ===== plumbing =====
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
  const d = await r.json().catch(() => ({ ok: false, error: "Bad response from server." }));
  if (!d.ok && !opts?.allowFail) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}
const post = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body || {}) });

let toastTimer = null;
function toast(msg, kind) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (kind ? " " + kind : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4200);
}

// ===== the eye =====
const FRAME_MAX_W = 1400;
let stream = null;
const preview = $("preview");

async function share() {
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: { ideal: 5, max: 10 } }, audio: false,
      selfBrowserSurface: "exclude",
    });
  } catch (e) {
    if (e && e.name === "NotAllowedError") return false;   // they cancelled the picker
    toast("Couldn't start sharing: " + (e.message || e), "bad");
    return false;
  }
  preview.srcObject = stream;
  preview.play().catch(() => {});
  stream.getVideoTracks()[0].addEventListener("ended", stopSharing);
  state.sharing = true;
  render();
  return true;
}

function stopSharing() {
  try { (stream ? stream.getTracks() : []).forEach((t) => t.stop()); } catch (e) {}
  stream = null;
  preview.srcObject = null;
  state.sharing = false;
  render();
}

function grabFrame() {
  if (!stream || !preview.videoWidth) return null;
  const w = Math.min(preview.videoWidth, FRAME_MAX_W), scale = w / preview.videoWidth;
  const c = document.createElement("canvas");
  c.width = w;
  c.height = Math.round(preview.videoHeight * scale);
  c.getContext("2d").drawImage(preview, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.82);   // text survives compression poorly
}

// ===== the capture queue =====
// One in flight at a time, in the order the button was pressed. A pending row appears
// the instant you press, so you can carry on clicking through pages while the reading
// catches up behind you.
const queue = [];
let draining = false;
let pendingId = 0;

function bank() {
  if (!state.sharing) { toast("Share a window first.", "bad"); return; }
  const image = grabFrame();
  if (!image) { toast("Nothing to capture yet — the shared window hasn't painted.", "bad"); return; }
  const label = $("labelInput").value.trim();
  $("labelInput").value = "";
  const row = { pending: ++pendingId, label, summary: label || "reading…" };
  state.shots.push(row);
  render();
  queue.push({ image, label, row });
  drain();
}

async function drain() {
  if (draining) return;
  draining = true;
  while (queue.length) {
    const job = queue.shift();
    try {
      const d = await post("/api/capture", {
        image: job.image, label: job.label, subject: state.subject,
      });
      state.subject = d.subject;
      Object.assign(job.row, { pending: 0, id: d.id, seq: d.seq, summary: d.summary,
                               note: d.note, kind: "screen" });
      state.stale = true;
      state.sealed = state.sealed;   // a new capture doesn't unseal, it staleness-marks
    } catch (e) {
      job.row.pending = 0;
      job.row.failed = true;
      job.row.summary = "couldn't read this one — " + e.message;
      toast(e.message, "bad");
    }
    render();
  }
  draining = false;
  refreshShots();
}

// ===== rendering =====
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function render() {
  $("subjectName").textContent = state.subject || "No subject yet";
  const n = state.shots.length;
  $("shotBadge").textContent = n;
  $("shotBadge").classList.toggle("hidden", !n);
  $("previewEmpty").classList.toggle("hidden", state.sharing);
  preview.classList.toggle("hidden", !state.sharing);
  $("captureBtn").disabled = !state.sharing;
  $("stopBtn").classList.toggle("hidden", !state.sharing);
  const ready = state.shots.some((s) => !s.pending && !s.failed);
  $("doneBtn").disabled = !ready;
  $("doneBtn").textContent = state.sealed && state.stale
    ? `Done — fold in ${n} capture${n === 1 ? "" : "s"}`
    : "Done — read it all";
  $("shotsTitle").textContent = n
    ? `${n} capture${n === 1 ? "" : "s"} banked` + (state.sealed ? " · brief written" : "")
    : "Nothing captured yet";

  const list = $("shots");
  list.innerHTML = "";
  state.shots.forEach((s, i) => {
    const li = el("li", "shot" + (s.pending ? " pending" : "") + (s.failed ? " failed" : ""));
    li.appendChild(el("span", "shot-n", s.seq ? "#" + s.seq : "·"));
    const mid = el("div", "shot-mid");
    mid.appendChild(el("div", "shot-sum", s.summary || "…"));
    if (s.label) mid.appendChild(el("div", "shot-label", "“" + s.label + "”"));
    if (s.kind === "url" && s.source) mid.appendChild(el("div", "shot-label", s.source));
    li.appendChild(mid);
    if (!s.pending) {
      const b = el("button", "ghost tiny-btn", "✕");
      b.title = "Bin this capture";
      b.addEventListener("click", () => dropShot(s, i));
      li.appendChild(b);
    }
    list.appendChild(li);
  });
}

async function refreshShots() {
  if (!state.subject) return;
  try {
    const d = await api("/api/shots?subject=" + encodeURIComponent(state.subject));
    state.shots = d.shots;
    const st = await api("/api/state");
    state.sealed = st.sealed;
    state.stale = st.stale;
    render();
  } catch (e) { /* the list is cosmetic; never break capture over it */ }
}

async function dropShot(shot, idx) {
  if (shot.id) {
    try { await api("/api/shots/" + shot.id, { method: "DELETE" }); } catch (e) {}
  }
  state.shots.splice(idx, 1);
  state.stale = true;
  render();
}

// ===== done: compact =====
async function seal() {
  const btn = $("doneBtn");
  const was = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Reading it all…";
  try {
    const d = await post("/api/seal", { subject: state.subject });
    state.sealed = true;
    state.stale = false;
    state.subject = d.subject;
    toast(`Brief written from ${d.n_shots} capture${d.n_shots === 1 ? "" : "s"}.`, "good");
    if (d.truncated) toast("The brief hit its length limit — ask for detail in sections.", "bad");
    showAsk();
    await loadTurns();
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    btn.textContent = was;
    btn.disabled = false;
    render();
  }
}

// ===== ask =====
function showAsk() {
  $("captureView").classList.add("hidden");
  $("askView").classList.remove("hidden");
  const w = $("staleWarn");
  if (state.stale && state.sealed) {
    w.textContent = "Captures banked since the brief was written aren't in it yet — press "
                  + "“Capture more”, then Done, to fold them in.";
    w.classList.remove("hidden");
  } else w.classList.add("hidden");
  $("question").focus();
}
function showCapture() {
  $("askView").classList.add("hidden");
  $("captureView").classList.remove("hidden");
}

const chat = $("chat");
// Answers lean on **bold** and `code` for the values that matter — a price, a setting,
// an exact phrase — so rendering them literally buries the very thing being emphasised.
// Escape first, then allow only these three: this text comes from a model reading a page
// that could itself contain markup.
function md(text) {
  const esc = (text || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\s)\*([^*\n]+)\*(?=\s|$|[.,;:!?])/g, "$1<em>$2</em>");
}

function addBubble(cls, text) {
  const empty = chat.querySelector(".empty");
  if (empty) empty.remove();
  const n = el("div", "bubble " + cls);
  setBubble(n, text);
  chat.appendChild(n);
  chat.scrollTop = chat.scrollHeight;
  return n;
}
function setBubble(node, text) { node.innerHTML = md(text); }

async function ask(q) {
  addBubble("me", q);
  const thinking = addBubble("buddy thinking", "reading the brief…");
  try {
    const d = await post("/api/ask", { question: q, subject: state.subject });
    thinking.classList.remove("thinking");
    setBubble(thinking, d.answer);
  } catch (e) {
    thinking.classList.remove("thinking");
    thinking.classList.add("err");
    thinking.textContent = e.message;
  }
}

async function loadTurns() {
  if (!state.subject) return;
  try {
    const d = await api("/api/turns?subject=" + encodeURIComponent(state.subject));
    if (!d.turns.length) return;
    chat.innerHTML = "";
    d.turns.forEach((t) => { addBubble("me", t.question); addBubble("buddy", t.answer || ""); });
  } catch (e) {}
}

// ===== overlays =====
function sheet(title, render) {
  $("sheetTitle").textContent = title;
  $("sheetBody").innerHTML = "";
  render($("sheetBody"));
  $("sheet").classList.remove("hidden");
}
$("sheetClose").addEventListener("click", () => $("sheet").classList.add("hidden"));
$("sheet").addEventListener("click", (e) => {
  if (e.target === $("sheet")) $("sheet").classList.add("hidden");
});

async function showBrief() {
  const d = await api("/api/brief?subject=" + encodeURIComponent(state.subject || ""));
  sheet("Brief — " + (state.subject || ""), (body) => {
    if (!d.brief) { body.appendChild(el("p", "hint", "Nothing compacted yet.")); return; }
    body.appendChild(el("p", "hint",
      `From ${d.brief.n_shots} capture(s) · ${new Date(d.brief.updated * 1000).toLocaleString()}`));
    const pre = el("div", "brief-doc");
    d.brief.text.split("\n").forEach((line) => {
      if (!line.trim()) return;
      const cls = line.startsWith("#") ? "brief-h" : "brief-l";
      pre.appendChild(el("div", cls, line.replace(/^#+\s*/, "").replace(/^[-*]\s*/, "• ")));
    });
    body.appendChild(pre);
  });
}

async function showLibrary() {
  const d = await api("/api/subjects");
  sheet("Library", (body) => {
    if (!d.subjects.length) { body.appendChild(el("p", "hint", "Nothing studied yet.")); return; }
    d.subjects.forEach((s) => {
      const row = el("div", "lib-row");
      const left = el("div");
      left.appendChild(el("div", "lib-name", s.name));
      left.appendChild(el("div", "hint",
        `${s.n_shots} capture(s) · ${s.sealed ? "brief written" : "not compacted"}`
        + (s.n_turns ? ` · ${s.n_turns} question(s)` : "")));
      row.appendChild(left);
      const open = el("button", "ghost", "Open");
      open.addEventListener("click", async () => {
        await post("/api/subject", { subject: s.name });
        $("sheet").classList.add("hidden");
        await boot();
        if (state.sealed) { showAsk(); await loadTurns(); }
      });
      row.appendChild(open);
      const del = el("button", "ghost tiny-btn", "✕");
      del.title = "Delete this subject and everything captured for it";
      del.addEventListener("click", async () => {
        if (!confirm(`Delete "${s.name}" and its ${s.n_shots} capture(s)? This can't be undone.`)) return;
        await api("/api/subject", { method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ subject: s.name }) });
        $("sheet").classList.add("hidden");
        await boot();
      });
      row.appendChild(del);
      body.appendChild(row);
    });
  });
}

// ===== wiring =====
$("shareBtn").addEventListener("click", share);
$("stopBtn").addEventListener("click", stopSharing);
$("captureBtn").addEventListener("click", bank);
$("doneBtn").addEventListener("click", seal);
$("backToCapture").addEventListener("click", showCapture);
$("briefBtn").addEventListener("click", () => showBrief().catch((e) => toast(e.message, "bad")));
$("libraryBtn").addEventListener("click", () => showLibrary().catch((e) => toast(e.message, "bad")));

$("newBtn").addEventListener("click", async () => {
  await post("/api/subject/new");
  state.subject = null; state.shots = []; state.sealed = false; state.stale = false;
  chat.innerHTML = "";
  showCapture();
  render();
  toast("New subject — the first capture will name it.", "good");
});

$("subjectBtn").addEventListener("click", async () => {
  const next = prompt("Name this subject", state.subject || "");
  if (next == null || !next.trim()) return;
  try {
    const d = state.subject
      ? await post("/api/subject", { subject: state.subject, rename: next.trim() })
      : await post("/api/subject", { subject: next.trim() });
    state.subject = d.subject;
    render();
  } catch (e) { toast(e.message, "bad"); }
});

$("urlBtn").addEventListener("click", async () => {
  const url = $("urlInput").value.trim();
  if (!url) return;
  const btn = $("urlBtn");
  btn.disabled = true; btn.textContent = "Reading…";
  try {
    const d = await post("/api/capture/url", { url, subject: state.subject });
    state.subject = d.subject;
    $("urlInput").value = "";
    state.stale = true;
    toast("Read that page in full.", "good");
    await refreshShots();
  } catch (e) { toast(e.message, "bad"); }
  finally { btn.disabled = false; btn.textContent = "Fetch"; }
});

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("question").value.trim();
  if (!q) return;
  $("question").value = "";
  ask(q);
});

$("mode").addEventListener("change", async () => {
  try { await post("/api/mode", { mode: $("mode").value }); } catch (e) { toast(e.message, "bad"); }
});

// Spacebar banks a capture — hands stay on the thing you're showing me. Not while
// typing, obviously, and not while the ask view is up.
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat) return;
  const t = e.target.tagName;
  if (t === "INPUT" || t === "TEXTAREA" || t === "SELECT") return;
  if (!$("captureView").classList.contains("hidden") && state.sharing) {
    e.preventDefault();
    bank();
  }
});

// ===== boot =====
async function boot() {
  try {
    const st = await api("/api/state");
    state.subject = st.subject;
    state.sealed = st.sealed;
    state.stale = st.stale;
    const sel = $("mode");
    if (!sel.options.length) {
      st.modes.forEach((m) => {
        const o = document.createElement("option");
        o.value = m.key; o.textContent = m.name; o.title = m.persona;
        sel.appendChild(o);
      });
    }
    sel.value = (st.modes.find((m) => m.name === st.mode) || {}).key || "answer";
    const s = st.sync || {};
    $("sync").className = "sync " + (!s.enabled ? "off" : s.error ? "bad" : "ok");
    $("sync").title = !s.enabled ? "Brain sync off (no token) — memory is local only"
      : s.error ? "Sync error: " + s.error : "Brain synced";
    state.shots = state.subject
      ? (await api("/api/shots?subject=" + encodeURIComponent(state.subject))).shots : [];
    render();
  } catch (e) {
    toast("Can't reach the server — " + e.message, "bad");
  }
}
boot().then(() => { if (state.sealed) { showAsk(); loadTurns(); } });
