// Node/region geometry (x, y, w, h, wrapped text lines) is computed
// entirely server-side (agent/tools/diagram_layout.py) — this file only
// draws whatever it's given via rough.js, with a fixed canvas padding.
const CANVAS_PADDING = 60;
const SVG_NS = "http://www.w3.org/2000/svg";

const KIND_COLORS = {
  client: { fill: "#a5d8ff", stroke: "#1864ab" },
  server: { fill: "#d0bfff", stroke: "#5f3dc4" },
  process: { fill: "#b2f2bb", stroke: "#2b8a3e" },
  memory: { fill: "#ffe066", stroke: "#e67700" },
  storage: { fill: "#eebefa", stroke: "#9c36b5" },
  structure: { fill: "#99e9f2", stroke: "#0b7285" },
  executor: { fill: "#a5d8ff", stroke: "#1971c2" },
};
const REGION_COLOR = { fill: "transparent", stroke: "#868e96" };

const state = {
  analysis: null,
  deck: "before",
  stepIndex: 0,
  renderedDeck: null, // which deck's diagram is currently built in the DOM
  playing: false,
  playTimer: null,
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
  compareBar: document.getElementById("compare-bar"),
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

  renderCompareBar();
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

function renderCompareBar() {
  const a = state.analysis;
  if (!a.after_stats) {
    el.compareBar.innerHTML = `<div class="metric"><span>Execution time</span><b>${fmtMs(a.before_stats.execution_time_ms)}</b></div>
      <div class="metric"><span>No optimization needed / found</span></div>`;
    return;
  }
  const before = a.before_stats.execution_time_ms;
  const after = a.after_stats.execution_time_ms;
  const speedup = before && after ? (before / after).toFixed(1) : "-";
  el.compareBar.innerHTML = `
    <div class="metric"><span>Before</span><b>${fmtMs(before)}</b></div>
    <div class="metric"><span>After</span><b>${fmtMs(after)}</b></div>
    <div class="metric ${speedup >= 1 ? "good" : "warn"}"><span>Speedup</span><b>${speedup}&times;</b></div>
  `;
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

// ---- Hand-drawn (rough.js) rendering — all geometry comes from the server ----

function seedFor(id) {
  let hash = 0;
  for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
  return (hash % 2000) + 1;
}

const TITLE_LINE_H = 19;
const DETAIL_LINE_H = 14;
const BOX_PAD = 10;

// Draws one rough rectangle + its pre-wrapped text (from the server), in a
// <g> whose opacity class drives active/visited/pending — cheap: no need
// to regenerate the rough sketch on every step change.
function drawBox(rc, svg, x, y, w, h, lines, colors, id, extraClass) {
  const g = document.createElementNS(SVG_NS, "g");
  if (id) g.id = `node-${id}`;
  g.classList.add("rough-node", "state-pending");
  if (extraClass) g.classList.add(extraClass);

  const rect = rc.rectangle(x, y, w, h, {
    fill: colors.fill,
    fillStyle: "hachure",
    fillWeight: 1.5,
    stroke: colors.stroke,
    strokeWidth: 1.6,
    roughness: 1.6,
    seed: id ? seedFor(id) : 7,
  });
  g.appendChild(rect);

  if (lines && lines.length) {
    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("x", x + BOX_PAD);
    text.setAttribute("y", y + BOX_PAD + 12);
    text.setAttribute("class", "hand-text");
    lines.forEach((line, i) => {
      const tspan = document.createElementNS(SVG_NS, "tspan");
      tspan.setAttribute("x", x + BOX_PAD);
      tspan.setAttribute("dy", i === 0 ? 0 : line.bold ? TITLE_LINE_H : DETAIL_LINE_H);
      tspan.textContent = line.text;
      if (line.bold) tspan.setAttribute("font-weight", "700");
      text.appendChild(tspan);
    });
    g.appendChild(text);
  }
  svg.appendChild(g);
  return { group: g, x, y, w, h };
}

// ---- Persistent diagram: built once per deck, never rebuilt between steps ----

function renderDeck() {
  const diagram = currentDiagram();
  if (!diagram) return;

  el.svg.innerHTML = "";
  const defs = document.createElementNS(SVG_NS, "defs");
  defs.innerHTML = `<marker id="arrowhead" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
    <path d="M0,0 L8,4 L0,8 Z" fill="#495057" /></marker>`;
  el.svg.appendChild(defs);

  const rc = rough.svg(el.svg);
  const rects = {};
  let maxX = 0;
  let maxY = 0;

  // Regions draw first (background dashed containers), nodes on top.
  (diagram.regions || []).forEach((region) => {
    const x = region.x + CANVAS_PADDING;
    const y = region.y + CANVAS_PADDING;
    const g = document.createElementNS(SVG_NS, "g");
    g.classList.add("rough-region");
    const rect = rc.rectangle(x, y, region.w, region.h, {
      fill: "transparent",
      stroke: REGION_COLOR.stroke,
      strokeWidth: 1.4,
      strokeLineDash: [8, 6],
      roughness: 1.2,
      seed: seedFor(region.id),
    });
    g.appendChild(rect);
    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", x + 10);
    label.setAttribute("y", y + 20);
    label.setAttribute("class", "hand-text region-label");
    label.textContent = region.label;
    g.appendChild(label);
    el.svg.appendChild(g);
    maxX = Math.max(maxX, x + region.w);
    maxY = Math.max(maxY, y + region.h);
  });

  diagram.nodes.forEach((node) => {
    const x = node.x + CANVAS_PADDING;
    const y = node.y + CANVAS_PADDING;
    const colors = KIND_COLORS[node.kind] || KIND_COLORS.executor;
    const box = drawBox(rc, el.svg, x, y, node.w, node.h, node.lines, colors, node.id);
    rects[node.id] = box;
    maxX = Math.max(maxX, x + node.w);
    maxY = Math.max(maxY, y + node.h);
  });

  drawEdges(rc, diagram, rects);

  el.canvas.style.width = `${maxX + CANVAS_PADDING}px`;
  el.canvas.style.height = `${maxY + CANVAS_PADDING}px`;
  el.svg.setAttribute("width", maxX + CANVAS_PADDING);
  el.svg.setAttribute("height", maxY + CANVAS_PADDING);

  renderLegend(diagram.legend);

  state.nodeRects = rects;
  state.renderedDeck = state.deck;
  applyStepState();
}

function renderLegend(legend) {
  if (!legend || !legend.length) return;
  const g = document.createElementNS(SVG_NS, "g");
  g.classList.add("rough-legend");
  legend.forEach((item, i) => {
    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("x", 8);
    text.setAttribute("y", el.svg.getAttribute("height") ? Number(el.svg.getAttribute("height")) - 10 - i * 14 : 20);
    text.setAttribute("class", "hand-text legend-item");
    text.textContent = `${item.label}: ${item.meaning}`;
    g.appendChild(text);
  });
  el.svg.appendChild(g);
}

function rectEdgePoint(rect, tx, ty) {
  const cx = rect.x + rect.w / 2;
  const cy = rect.y + rect.h / 2;
  const dx = tx - cx;
  const dy = ty - cy;
  if (dx === 0 && dy === 0) return { x: cx, y: cy };
  const halfW = rect.w / 2 + 4;
  const halfH = rect.h / 2 + 4;
  const scale = 1 / Math.max(Math.abs(dx) / halfW, Math.abs(dy) / halfH);
  return { x: cx + dx * scale, y: cy + dy * scale };
}

function drawEdges(rc, diagram, rects) {
  diagram.edges.forEach((edge) => {
    const f = rects[edge.from];
    const t = rects[edge.to];
    if (!f || !t) return;
    const fCenter = { x: f.x + f.w / 2, y: f.y + f.h / 2 };
    const tCenter = { x: t.x + t.w / 2, y: t.y + t.h / 2 };
    const p1 = rectEdgePoint(f, tCenter.x, tCenter.y);
    const p2 = rectEdgePoint(t, fCenter.x, fCenter.y);

    const g = document.createElementNS(SVG_NS, "g");
    g.id = `edge-${edge.from}-${edge.to}`;
    g.classList.add("rough-edge", "state-pending");

    const line = rc.line(p1.x, p1.y, p2.x, p2.y, {
      stroke: "#495057",
      strokeWidth: 1.6,
      roughness: 1.4,
      seed: seedFor(edge.from + edge.to),
    });
    const innerPath = line.querySelector("path") || line;
    innerPath.setAttribute("marker-end", "url(#arrowhead)");
    g.appendChild(line);

    if (edge.label) {
      const mx = (p1.x + p2.x) / 2;
      const my = (p1.y + p2.y) / 2 - 6;
      const halo = document.createElementNS(SVG_NS, "text");
      halo.setAttribute("x", mx);
      halo.setAttribute("y", my);
      halo.setAttribute("text-anchor", "middle");
      halo.setAttribute("class", "hand-text edge-label-halo");
      halo.textContent = edge.label;
      g.appendChild(halo);

      const text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("x", mx);
      text.setAttribute("y", my);
      text.setAttribute("text-anchor", "middle");
      text.setAttribute("class", "hand-text edge-label");
      text.textContent = edge.label;
      g.appendChild(text);
    }

    el.svg.appendChild(g);
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
    steps[i].highlight_node_ids.forEach((id) => visited.add(id));
  }
  const active = new Set(step.highlight_node_ids);

  el.svg.querySelectorAll(".rough-node").forEach((g) => {
    const id = g.id.replace(/^node-/, "");
    g.classList.remove("state-active", "state-visited", "state-pending");
    g.classList.add(active.has(id) ? "state-active" : visited.has(id) ? "state-visited" : "state-pending");
  });
  el.svg.querySelectorAll(".rough-edge").forEach((g) => {
    const [, from, to] = g.id.match(/^edge-(.+)-(.+)$/);
    const bothActive = active.has(from) && active.has(to);
    const bothSeen = (active.has(from) || visited.has(from)) && (active.has(to) || visited.has(to));
    g.classList.remove("state-active", "state-visited", "state-pending");
    g.classList.add(bothActive ? "state-active" : bothSeen ? "state-visited" : "state-pending");
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

  const activeGroup = document.getElementById(`node-${step.highlight_node_ids[0]}`);
  if (activeGroup) activeGroup.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
}

function fmtMs(v) {
  return v == null ? "-" : `${v.toFixed ? v.toFixed(2) : v} ms`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
