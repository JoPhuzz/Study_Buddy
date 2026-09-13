// Study Buddy — bank captures, compact them, then ask.
//
// The browser is the eye: getDisplayMedia gives us the window, and each press of Capture
// grabs one frame and posts it. Frames are 1400px wide because interfaces are dense
// 11-13px text and a menu label that survives at 800px is mush at 600.
//
// The interface answers back. Sharing, reading, thinking and banking each have a
// visible state — the core in the top bar, the brackets on the stage, the shutter
// flash — because every one of them is something happening on a server you can't see,
// and a tool that gives you nothing back while it works feels broken even when it isn't.
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

// One counter, one lamp. Any number of overlapping requests, one honest indicator.
let busy = 0;
function working(on) {
  busy = Math.max(0, busy + (on ? 1 : -1));
  document.body.classList.toggle("busy", busy > 0);
}

let toastTimer = null;
function toast(msg, kind) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast hidden";
  void t.offsetWidth;                       // restart the entry animation for a second toast
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

// A frame is banked in about a millisecond and posted invisibly, so without this you
// press the button and nothing whatsoever happens. The flash is the receipt.
function shutter() {
  const f = $("flash");
  f.classList.remove("fire");
  void f.offsetWidth;
  f.classList.add("fire");
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
  shutter();
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
  document.body.classList.toggle("live", state.sharing);
  $("stage").classList.toggle("live", state.sharing);
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
    // Rows cascade in rather than appearing as a block — a PDF landing forty pages at
    // once should read as an arrival, not a repaint. Capped so page 40 isn't a wait.
    li.style.animationDelay = Math.min(i, 12) * 26 + "ms";
    li.appendChild(el("span", "shot-n", s.seq ? "#" + s.seq : "·"));
    const mid = el("div", "shot-mid");
    mid.appendChild(el("div", "shot-sum", s.summary || "…"));
    if (s.label) mid.appendChild(el("div", "shot-label", "“" + s.label + "”"));
    if (s.source && s.kind !== "screen") mid.appendChild(el("div", "shot-label", s.source));
    if (s.kind === "note") mid.appendChild(el("div", "shot-mine", "✎ your own words"));
    if (s.edited) mid.appendChild(el("div", "shot-edited", "✎ corrected by you"));
    li.appendChild(mid);
    if (!s.pending) {
      if (s.id) {
        const ed = el("button", "ghost tiny-btn", "✎");
        ed.title = "Read and correct what was recorded from this capture";
        ed.addEventListener("click", (ev) => { ev.stopPropagation(); editShot(s); });
        li.appendChild(ed);
      }
      const b = el("button", "ghost tiny-btn", "✕");
      b.title = "Bin this capture";
      b.addEventListener("click", (ev) => { ev.stopPropagation(); dropShot(s, i); });
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
  working(true);
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
    working(false);
    btn.textContent = was;
    btn.disabled = false;
    render();
  }
}

// ===== the mode rail =====
// Five sigils drawn on one 24-grid from the same three parts — a ring, a core, a
// vertical — so they read as one alphabet. These are five ways of reading the same
// brief; five icons borrowed from five different metaphors would say the opposite.
// The <select> stays in the DOM and stays authoritative: this is a skin over it, so
// the mode still round-trips to the server through exactly one path.
const SIGILS = {
  answer:  '<circle class="orbit" cx="12" cy="12" r="8.5" stroke-dasharray="30 7"/>'      // one point, ringed
         + '<circle cx="12" cy="12" r="2.9" fill="currentColor" stroke="none"/>',
  quote:   '<circle cx="12" cy="12" r="8.5"/><path d="M9 8.4v7.2M15 8.4v7.2"/>',          // held between two bars
  summary: '<circle class="orbit" cx="12" cy="12" r="8.5" stroke-dasharray="17 6"/>'      // rings closing inward
         + '<circle cx="12" cy="12" r="5"/>'
         + '<circle cx="12" cy="12" r="1.7" fill="currentColor" stroke="none"/>',
  compare: '<circle cx="8.7" cy="12" r="5.7"/><circle cx="15.3" cy="12" r="5.7"/>',       // the lens between two
  direct:  '<path d="M12 2.6v18.8"/>'                                                     // shortest path through
         + '<circle cx="12" cy="12" r="3.3" fill="currentColor" stroke="none"/>',
};

function sigilSvg(key) {
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
       + 'stroke-linecap="round" aria-hidden="true">'
       + (SIGILS[key] || '<circle cx="12" cy="12" r="8.5"/>') + '</svg>';
}

// The halo slides between sigils. It can only be placed once the rail has a layout, so
// a call while the ask view is hidden is a no-op and showAsk() places it on the way in.
function moveHalo(instant) {
  const on = $("modeRail").querySelector('[aria-selected="true"]');
  const halo = $("modeHalo");
  if (!on || !on.offsetWidth) return;
  if (instant) halo.classList.add("armed");
  halo.style.left = on.offsetLeft + "px";
  halo.style.width = on.offsetWidth + "px";
  if (instant) { void halo.offsetWidth; halo.classList.remove("armed"); }
}

function paintMode(key, animate) {
  let name = "";
  $("modeRail").querySelectorAll(".sigil").forEach((b) => {
    const on = b.dataset.key === key;
    b.setAttribute("aria-selected", on ? "true" : "false");
    b.tabIndex = on ? 0 : -1;
    if (on) name = b.dataset.name;
  });
  const label = $("modeName");
  if (label.textContent !== name) {
    label.textContent = name;
    if (animate) { label.classList.remove("swap"); void label.offsetWidth; label.classList.add("swap"); }
  }
  moveHalo(!animate);
}

function buildRail(modeList) {
  const rail = $("modeRail"), sel = $("mode");
  const keys = modeList.map((m) => m.key);
  const pick = (key) => {
    if (sel.value === key) return;
    sel.value = key;
    paintMode(key, true);
    sel.dispatchEvent(new Event("change"));
  };
  modeList.forEach((m) => {
    const b = el("button", "sigil");
    b.type = "button";
    b.dataset.key = m.key;
    b.dataset.name = m.name;
    b.setAttribute("role", "tab");
    b.setAttribute("aria-label", m.name);
    b.title = m.name + " — " + m.persona;
    b.innerHTML = sigilSvg(m.key);
    b.addEventListener("click", () => pick(m.key));
    rail.appendChild(b);
  });
  // Arrow keys walk the rail, the way a segmented control is supposed to.
  rail.addEventListener("keydown", (e) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    const next = keys[(keys.indexOf(sel.value) + step + keys.length) % keys.length];
    const btn = rail.querySelector('[data-key="' + next + '"]');
    pick(next);
    btn.focus();
  });
  addEventListener("resize", () => moveHalo(true));
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
  moveHalo(true);
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
  const thinking = addBubble("buddy thinking", "reading the brief");
  thinking.insertAdjacentHTML("beforeend", '<span class="pulse"><i></i><i></i><i></i></span>');
  working(true);
  try {
    const d = await post("/api/ask", { question: q, subject: state.subject });
    thinking.classList.remove("thinking");
    setBubble(thinking, d.answer);
    // Two models can answer now — hover tells you which one did, and what it cost.
    thinking.title = d.model + (d.cost ? ` · $${d.cost.toFixed(4)}` : " · free");
    if (d.truncated) {
      // It ran out of room even after the shorter-answer retry. Never leave this
      // implicit — a reply cut off mid-word reads as a complete thought if you don't
      // happen to notice the missing full stop.
      const cut = el("div", "cut-note",
        `⚠ Cut off — ${d.mode} mode caps the answer length. Ask for the rest, or switch `
        + `to Summary or Compare for a longer one.`);
      thinking.appendChild(cut);
      chat.scrollTop = chat.scrollHeight;
    }
  } catch (e) {
    thinking.classList.remove("thinking");
    thinking.classList.add("err");
    thinking.textContent = e.message;
  } finally {
    working(false);
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

// Editing is not a hole in the closed world — you were the one looking at the screen,
// so you know things the reader never could. Corrections are stamped so the brief can
// still be audited afterwards.
function editorSheet(title, opts) {
  sheet(title, (body) => {
    if (opts.hint) body.appendChild(el("p", "hint", opts.hint));
    let summaryEl = null;
    if (opts.summary !== undefined) {
      body.appendChild(el("label", "edit-label", opts.summaryLabel || "Summary — the line in the list"));
      summaryEl = el("input", "label-input");
      summaryEl.type = "text";
      summaryEl.value = opts.summary || "";
      body.appendChild(summaryEl);
    }
    body.appendChild(el("label", "edit-label", opts.bodyLabel || "The record"));
    const area = el("textarea", "edit-area");
    area.value = opts.text || "";
    body.appendChild(area);
    const row = el("div", "edit-actions");
    const save = el("button", "primary", opts.saveLabel || "Save");
    const cancel = el("button", "ghost", "Cancel");
    cancel.addEventListener("click", () => $("sheet").classList.add("hidden"));
    save.addEventListener("click", async () => {
      save.disabled = true; save.textContent = "Saving…";
      try {
        await opts.onSave(area.value, summaryEl ? summaryEl.value : undefined);
        $("sheet").classList.add("hidden");
      } catch (e) {
        toast(e.message, "bad");
        save.disabled = false; save.textContent = opts.saveLabel || "Save";
      }
    });
    // ⌘↩ / Ctrl+↩ saves from inside the text, so a note can be banked without
    // reaching for the mouse — the same reflex as Space on the capture button.
    body.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !save.disabled) {
        e.preventDefault();
        save.click();
      }
    });
    row.appendChild(save); row.appendChild(cancel);
    body.appendChild(row);
    area.focus();
  });
}

function editShot(shot) {
  editorSheet(`Capture #${shot.seq}`, {
    hint: "This is the only record of that capture — the brief is rebuilt from it, so a "
        + "correction here is the durable one. Press Done afterwards to fold it in.",
    summary: shot.summary,
    bodyLabel: "What was recorded",
    text: shot.note || "",
    onSave: async (note, summary) => {
      await api("/api/shots/" + shot.id, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note, summary }),
      });
      toast(`Capture #${shot.seq} corrected — press Done to fold it into the brief.`, "good");
      state.stale = true;
      await refreshShots();
    },
  });
}

// Your own words as a capture. It lands in the list, is compacted with the rest,
// and answers can draw on it — attributed to you, never to a source you captured.
function writeNote() {
  editorSheet("Write a note", {
    hint: "Whatever you type is banked exactly as written, marked as yours. Press Done "
        + "afterwards to fold it into the brief.",
    summary: "",
    summaryLabel: "Title — the line in the list (optional)",
    bodyLabel: "Your note",
    saveLabel: "Bank it",
    text: "",
    onSave: async (text, title) => {
      if (!text.trim()) throw new Error("Nothing to bank — the note is empty.");
      const d = await post("/api/capture/note", { title, text, subject: state.subject });
      state.subject = d.subject;
      state.stale = true;
      toast(`Note #${d.seq} banked — press Done to fold it into the brief.`, "good");
      await refreshShots();
    },
  });
}

async function editBrief() {
  const d = await api("/api/brief?subject=" + encodeURIComponent(state.subject || ""));
  if (!d.brief) { toast("Nothing sealed yet.", "bad"); return; }
  editorSheet("Edit brief — " + (state.subject || ""), {
    hint: "Answers come from this text and nothing else. It survives the next Done — "
        + "compaction is told to carry forward what new captures don't change — but "
        + "correcting the capture it came from is the more durable fix.",
    bodyLabel: "The brief",
    text: d.brief.text,
    onSave: async (text) => {
      await api("/api/brief", {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, subject: state.subject }),
      });
      toast("Brief updated.", "good");
    },
  });
}

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
    const edit = el("button", "ghost", "✎ Edit this brief");
    edit.addEventListener("click", () => editBrief().catch((e) => toast(e.message, "bad")));
    body.appendChild(el("div", "edit-actions")).appendChild(edit);
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

// ===== files: drop or pick =====
// A PDF arrives as one capture per page, so a single drop can bank forty things. The
// upload is serialised with the screen-capture queue for the same reason that queue
// exists: order is what lets the reader recognise page 4 as following page 3.
async function uploadFile(file) {
  const row = { pending: ++pendingId, summary: `reading ${file.name}…` };
  state.shots.push(row);
  render();
  working(true);
  const form = new FormData();
  form.append("file", file);
  if (state.subject) form.append("subject", state.subject);
  try {
    const r = await fetch("/api/capture/file", { method: "POST", body: form });
    if (r.status === 401) { location.href = "/login"; return; }
    const d = await r.json().catch(() => ({ ok: false, error: "Bad response from server." }));
    if (!d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    state.subject = d.subject;
    state.stale = true;
    const n = d.n_banked;
    toast(`${file.name}: ${n} capture${n === 1 ? "" : "s"} banked`
          + (d.cost ? ` · $${d.cost.toFixed(4)}` : " · free"), "good");
    (d.notes || []).forEach((note) => toast(note, "bad"));
  } catch (e) {
    toast(`${file.name}: ${e.message}`, "bad");
  } finally {
    working(false);
    const i = state.shots.indexOf(row);
    if (i >= 0) state.shots.splice(i, 1);
    await refreshShots();
    render();
  }
}

async function uploadAll(fileList) {
  const list = [...fileList];
  if (!list.length) return;
  for (const f of list) await uploadFile(f);   // in order, one at a time
}

$("pickBtn").addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", (e) => {
  uploadAll(e.target.files);
  e.target.value = "";        // so re-picking the same file fires again
});

// The veil is pointer-events:none, so these listeners stay on the document and the drop
// lands wherever it was released.
let dragDepth = 0;
document.addEventListener("dragenter", (e) => {
  if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
  e.preventDefault();
  if (++dragDepth === 1) $("dropVeil").classList.remove("hidden");
});
document.addEventListener("dragover", (e) => {
  if ([...(e.dataTransfer?.types || [])].includes("Files")) e.preventDefault();
});
document.addEventListener("dragleave", () => {
  if (--dragDepth <= 0) { dragDepth = 0; $("dropVeil").classList.add("hidden"); }
});
document.addEventListener("drop", (e) => {
  if (!e.dataTransfer?.files?.length) return;
  e.preventDefault();
  dragDepth = 0;
  $("dropVeil").classList.add("hidden");
  uploadAll(e.dataTransfer.files);
});

// ===== wiring =====
$("shareBtn").addEventListener("click", share);
$("writeBtn").addEventListener("click", writeNote);
$("writeHint").addEventListener("click", writeNote);
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
  working(true);
  try {
    const d = await post("/api/capture/url", { url, subject: state.subject });
    state.subject = d.subject;
    $("urlInput").value = "";
    state.stale = true;
    toast("Read that page in full.", "good");
    await refreshShots();
  } catch (e) { toast(e.message, "bad"); }
  finally { working(false); btn.disabled = false; btn.textContent = "Fetch"; }
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
      buildRail(st.modes);
    }
    sel.value = (st.modes.find((m) => m.name === st.mode) || {}).key || "answer";
    paintMode(sel.value, false);
    const s = st.sync || {};
    $("sync").className = "sync " + (!s.enabled ? "off" : s.error ? "bad" : "ok");
    $("sync").title = !s.enabled ? "Brain sync off (no token) — memory is local only"
      : s.error ? "Sync error: " + s.error : "Brain synced";
    state.shots = state.subject
      ? (await api("/api/shots?subject=" + encodeURIComponent(state.subject))).shots : [];
    render();
  } catch (e) {
    // A configuration problem is not a connectivity problem, and saying so sends you
    // looking in the wrong place on a first deploy.
    const net = /fetch|network|load failed/i.test(e.message);
    toast(net ? "Can't reach the server — " + e.message : e.message, "bad");
  }
}
boot().then(() => { if (state.sealed) { showAsk(); loadTurns(); } });
