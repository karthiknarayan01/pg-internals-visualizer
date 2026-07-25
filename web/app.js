// v9: the Expert LLM emits a *complete* Excalidraw-shaped diagram (real
// x/y/width/height/color per element — see agent/prompts.py::PG_EXPERT_
// INSTRUCTION) plus a slideshow of {title, narration, focus_box_ids,
// viewport} slides. This file no longer computes or interprets any
// geometry/color itself — it draws each rectangle/text/arrow element using
// its own stored style, and pans/zooms to each step's own viewport.
const CANVAS_PADDING = 60;
const SVG_NS = "http://www.w3.org/2000/svg";
const MAX_ZOOM = 2.5;

const state = {
  analysis: null,
  deck: "before",
  stepIndex: 0,
  renderedDeck: null, // which deck's diagram is currently built in the DOM
  playing: false,
  playTimer: null,
  canvasOffset: { x: CANVAS_PADDING, y: CANVAS_PADDING }, // diagram-space -> SVG-space
  fullCanvasSize: { w: 0, h: 0 }, // unscaled SVG size, for resetting zoom
};

const el = {
  sql: document.getElementById("sql"),
  ddlSection: document.getElementById("ddl-section"),
  ddl: document.getElementById("ddl"),
  isolationLevel: document.getElementById("isolation-level"),
  analyzeBtn: document.getElementById("analyze-btn"),
  status: document.getElementById("status"),
  schemaNote: document.getElementById("schema-note"),
  results: document.getElementById("results"),
  tabs: document.querySelectorAll(".tab"),
  afterTab: document.getElementById("after-tab"),
  canvas: document.getElementById("diagram-canvas"),
  svg: document.getElementById("diagram-svg"),
  slideTitle: document.getElementById("slide-title"),
  slideCounter: document.getElementById("slide-counter"),
  narration: document.getElementById("narration"),
  prevBtn: document.getElementById("prev-btn"),
  nextBtn: document.getElementById("next-btn"),
  playBtn: document.getElementById("play-btn"),
  dots: document.getElementById("dots"),
  recommendations: document.getElementById("recommendations"),
  recList: document.getElementById("rec-list"),
};

el.analyzeBtn.addEventListener("click", runAnalysis);
el.prevBtn.addEventListener("click", () => { stopPlaying(); moveStep(-1); });
el.nextBtn.addEventListener("click", () => { stopPlaying(); moveStep(1); });
el.playBtn.addEventListener("click", togglePlaying);
document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") { stopPlaying(); moveStep(-1); }
  if (e.key === "ArrowRight") { stopPlaying(); moveStep(1); }
});
el.tabs.forEach((tab) =>
  tab.addEventListener("click", () => {
    if (tab.disabled) return;
    stopPlaying();
    el.tabs.forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    state.deck = tab.dataset.deck;
    state.stepIndex = 0;
    renderDeck();
  })
);

// ---- SSE-streamed analysis: status events update el.status, a final
// "result" event carries the full payload. ----

async function runAnalysis() {
  const sql = el.sql.value.trim();
  if (!sql) return;

  el.analyzeBtn.disabled = true;
  el.status.textContent = "Starting analysis...";
  el.status.classList.remove("error");
  el.results.classList.add("hidden");
  el.schemaNote.classList.add("hidden");

  try {
    const payload = { sql, isolation_level: el.isolationLevel.value };
    if (el.ddl.value.trim()) payload.ddl = el.ddl.value.trim();

    const resp = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      // sse-starlette sends CRLF line endings ("\r\n\r\n" between events) —
      // normalize to LF first or indexOf("\n\n") never matches and no event
      // is ever parsed.
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");

      let sepIndex;
      while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        handleSseEvent(rawEvent);
      }
    }
  } catch (err) {
    el.status.textContent = "Request failed: " + err;
    el.status.classList.add("error");
  } finally {
    el.analyzeBtn.disabled = false;
  }
}

function handleSseEvent(rawEvent) {
  let eventName = "message";
  const dataLines = [];
  for (const line of rawEvent.split("\n")) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return;
  const data = JSON.parse(dataLines.join("\n"));

  if (eventName === "status") {
    el.status.textContent = data.message;
    el.status.classList.remove("error");
  } else if (eventName === "result") {
    handleResult(data);
  }
}

function handleResult(data) {
  if (data.status === "needs_schema") {
    el.ddlSection.classList.remove("hidden");
    el.status.textContent =
      data.message || "Couldn't auto-infer a schema for this query — fill in the DDL above and analyze again.";
    el.status.classList.add("error");
    return;
  }
  if (data.status === "error") {
    el.status.textContent = data.message || "Something went wrong.";
    el.status.classList.add("error");
    return;
  }

  state.analysis = data;
  state.deck = "before";
  state.stepIndex = 0;
  state.renderedDeck = null;
  el.status.textContent = "";
  el.ddlSection.classList.add("hidden");
  el.results.classList.remove("hidden");
  el.afterTab.disabled = !data.after_steps;
  el.tabs.forEach((t) => t.classList.remove("active"));
  document.querySelector('.tab[data-deck="before"]').classList.add("active");

  if (data.schema_note) {
    el.schemaNote.textContent = data.schema_note;
    el.schemaNote.classList.remove("hidden");
  }

  renderRecommendations();
  renderDeck();
}

function currentDiagram() {
  if (!state.analysis) return null;
  return state.deck === "before" ? state.analysis.before_diagram : state.analysis.after_diagram;
}

function currentSteps() {
  if (!state.analysis) return [];
  return state.deck === "before" ? state.analysis.before_steps : state.analysis.after_steps || [];
}

function moveStep(delta) {
  const steps = currentSteps();
  if (!steps.length) return;
  const next = state.stepIndex + delta;
  if (next < 0 || next > steps.length - 1) {
    if (delta > 0) stopPlaying();
    return;
  }
  state.stepIndex = next;
  applyStepState();
}

function togglePlaying() {
  if (state.playing) {
    stopPlaying();
    return;
  }
  const steps = currentSteps();
  if (!steps.length) return;
  if (state.stepIndex >= steps.length - 1) state.stepIndex = 0;
  state.playing = true;
  el.playBtn.textContent = "⏸"; // pause symbol
  el.playBtn.classList.add("playing");
  applyStepState();
  state.playTimer = setInterval(() => {
    const s = currentSteps();
    if (state.stepIndex >= s.length - 1) {
      stopPlaying();
      return;
    }
    state.stepIndex += 1;
    applyStepState();
  }, 2500);
}

function stopPlaying() {
  if (state.playTimer) clearInterval(state.playTimer);
  state.playTimer = null;
  state.playing = false;
  el.playBtn.textContent = "▶"; // play symbol
  el.playBtn.classList.remove("playing");
}

function renderRecommendations() {
  const recs = state.analysis.recommendations || [];
  if (!recs.length) {
    el.recommendations.classList.add("hidden");
    return;
  }
  el.recommendations.classList.remove("hidden");
  el.recList.innerHTML = recs
    .map(
      (r) => `<div class="rec-item">
        <code>${escapeHtml(r.index_sql)}</code>
        <p>${escapeHtml(r.rationale)}</p>
      </div>`
    )
    .join("");
}

// ---- Hand-drawn (rough.js) rendering of the Expert's own raw Excalidraw
// elements — every rectangle/text/arrow uses its own stored x/y/width/
// height/color/seed, never a client-computed one. ----

function seedFromInt(n) {
  return ((n || 1) % 2000) + 1;
}

function drawRawRect(rc, svg, e, offsetX, offsetY) {
  const x = e.x + offsetX, y = e.y + offsetY;
  const g = document.createElementNS(SVG_NS, "g");
  g.dataset.id = e._id;
  g.classList.add("rough-el", "state-pending");
  const opts = {
    fill: e.backgroundColor === "transparent" ? "transparent" : e.backgroundColor,
    fillStyle: e.fillStyle || "solid",
    stroke: e.strokeColor,
    strokeWidth: e.strokeWidth || 1.5,
    roughness: e.roughness != null ? e.roughness : 1,
    seed: seedFromInt(e.seed),
  };
  if (e.strokeStyle === "dashed") opts.strokeLineDash = [8, 6];
  g.appendChild(rc.rectangle(x, y, e.width, e.height, opts));
  svg.appendChild(g);
}

function drawRawText(svg, e, offsetX, offsetY) {
  const x = e.x + offsetX, y = e.y + offsetY;
  const g = document.createElementNS(SVG_NS, "g");
  g.dataset.id = e._id;
  g.classList.add("rough-el", "state-pending");
  const fontSize = e.fontSize || 16;
  const lineH = fontSize * (e.lineHeight || 1.25);
  const text = document.createElementNS(SVG_NS, "text");
  text.setAttribute("class", "hand-text");
  text.setAttribute("font-size", fontSize);
  text.setAttribute("fill", e.strokeColor || "#1e1e1e");
  const anchor = e.textAlign === "center" ? "middle" : e.textAlign === "right" ? "end" : "start";
  text.setAttribute("text-anchor", anchor);
  const tx = anchor === "middle" ? x + e.width / 2 : anchor === "end" ? x + e.width : x;
  String(e.text || "").split("\n").forEach((line, i) => {
    const tspan = document.createElementNS(SVG_NS, "tspan");
    tspan.setAttribute("x", tx);
    tspan.setAttribute("y", y + fontSize + i * lineH);
    tspan.textContent = line;
    text.appendChild(tspan);
  });
  g.appendChild(text);
  svg.appendChild(g);
}

function drawRawArrow(rc, svg, e, offsetX, offsetY) {
  // `points` can have more than 2 entries (one or more bend points) when the
  // Expert routes an arrow around a box instead of straight through it —
  // linearPath handles both a straight 2-point case and a bent path the
  // same way, so there's no special-casing needed here.
  const pts = e.points.map(([px, py]) => [e.x + px + offsetX, e.y + py + offsetY]);
  const g = document.createElementNS(SVG_NS, "g");
  g.dataset.id = e._id;
  g.classList.add("rough-el", "state-pending");
  const path = rc.linearPath(pts, {
    stroke: e.strokeColor,
    strokeWidth: e.strokeWidth || 1.5,
    roughness: e.roughness != null ? e.roughness : 1,
    seed: seedFromInt(e.seed),
  });
  if (e.endArrowhead === "arrow") {
    const innerPath = path.querySelector("path") || path;
    innerPath.setAttribute("marker-end", "url(#arrowhead)");
  }
  g.appendChild(path);
  svg.appendChild(g);
}

// ---- Persistent diagram: built once per deck, never rebuilt between steps ----

function renderDeck() {
  const diagram = currentDiagram();
  if (!diagram) return;

  el.svg.innerHTML = "";
  el.svg.style.transform = "";
  const defs = document.createElementNS(SVG_NS, "defs");
  defs.innerHTML = `<marker id="arrowhead" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
    <path d="M0,0 L8,4 L0,8 Z" fill="#495057" /></marker>`;
  el.svg.appendChild(defs);

  const rc = rough.svg(el.svg);
  const entries = Object.entries(diagram.elements || {}).map(([id, e]) => ({ ...e, _id: id }));

  let minX = 0, minY = 0, maxX = 0, maxY = 0;
  if (entries.length) {
    minX = Math.min(...entries.map((e) => e.x));
    minY = Math.min(...entries.map((e) => e.y));
    maxX = Math.max(...entries.map((e) => e.x + e.width));
    maxY = Math.max(...entries.map((e) => e.y + e.height));
  }
  const offsetX = CANVAS_PADDING - minX;
  const offsetY = CANVAS_PADDING - minY;
  state.canvasOffset = { x: offsetX, y: offsetY };

  // Biggest rects first (dashed region frames are always largest, so this
  // alone reproduces "small boxes sit visually inside big ones"), then
  // arrows, then text on top.
  const rects = entries.filter((e) => e.type === "rectangle").sort((a, b) => b.width * b.height - a.width * a.height);
  const arrows = entries.filter((e) => e.type === "arrow");
  const texts = entries.filter((e) => e.type === "text");
  rects.forEach((e) => drawRawRect(rc, el.svg, e, offsetX, offsetY));
  arrows.forEach((e) => drawRawArrow(rc, el.svg, e, offsetX, offsetY));
  texts.forEach((e) => drawRawText(el.svg, e, offsetX, offsetY));

  const totalW = maxX - minX + 2 * CANVAS_PADDING;
  const totalH = maxY - minY + 2 * CANVAS_PADDING;
  state.fullCanvasSize = { w: totalW, h: totalH };
  el.canvas.style.width = `${totalW}px`;
  el.canvas.style.height = `${totalH}px`;
  el.svg.setAttribute("width", totalW);
  el.svg.setAttribute("height", totalH);

  state.renderedDeck = state.deck;
  applyStepState();
}

// Pans/zooms the scrollable wrapper so `viewport` (in original
// diagram-space coordinates, per the Expert's own slide JSON) fills the
// visible area — `null` resets to the full, unzoomed diagram.
function applyViewport(viewport) {
  const wrap = el.canvas.parentElement;
  if (!viewport || !viewport.width || !viewport.height) {
    el.svg.style.transform = "";
    el.canvas.style.width = `${state.fullCanvasSize.w}px`;
    el.canvas.style.height = `${state.fullCanvasSize.h}px`;
    wrap.scrollTo({ left: 0, top: 0, behavior: "smooth" });
    return;
  }
  const offset = state.canvasOffset;
  const vx = viewport.x + offset.x, vy = viewport.y + offset.y;
  const vw = viewport.width, vh = viewport.height;
  const availW = wrap.clientWidth, availH = wrap.clientHeight;
  const scale = Math.min(availW / vw, availH / vh, MAX_ZOOM);

  el.svg.style.transformOrigin = "0 0";
  el.svg.style.transform = `scale(${scale})`;
  el.canvas.style.width = `${state.fullCanvasSize.w * scale}px`;
  el.canvas.style.height = `${state.fullCanvasSize.h * scale}px`;
  wrap.scrollTo({
    left: Math.max(0, vx * scale - (availW - vw * scale) / 2),
    top: Math.max(0, vy * scale - (availH - vh * scale) / 2),
    behavior: "smooth",
  });
}

// ---- Stepping: only toggles active/visited/pending state, never rebuilds boxes ----

function applyStepState() {
  if (state.renderedDeck !== state.deck) return; // diagram still building
  const steps = currentSteps();
  const step = steps[state.stepIndex];
  if (!step) return;

  const visited = new Set();
  for (let i = 0; i < state.stepIndex; i++) {
    (steps[i].element_ids || []).forEach((id) => visited.add(id));
  }
  const active = new Set(step.element_ids || []);

  el.svg.querySelectorAll(".rough-el").forEach((g) => {
    const id = g.dataset.id;
    g.classList.remove("state-active", "state-visited", "state-pending");
    g.classList.add(active.has(id) ? "state-active" : visited.has(id) ? "state-visited" : "state-pending");
  });

  el.slideTitle.textContent = step.title;
  el.slideCounter.textContent = `${state.stepIndex + 1} / ${steps.length}`;
  el.narration.textContent = step.narration || "";

  el.dots.innerHTML = steps
    .map((_, i) => `<span class="dot ${i === state.stepIndex ? "active" : ""}" data-i="${i}"></span>`)
    .join("");
  el.dots.querySelectorAll(".dot").forEach((dot) =>
    dot.addEventListener("click", () => {
      stopPlaying();
      state.stepIndex = Number(dot.dataset.i);
      applyStepState();
    })
  );

  el.prevBtn.disabled = state.stepIndex === 0;
  el.nextBtn.disabled = state.stepIndex === steps.length - 1;

  applyViewport(step.viewport);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
