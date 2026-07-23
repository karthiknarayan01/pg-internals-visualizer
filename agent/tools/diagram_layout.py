"""Region-based diagram layout: Python is the sole authority on pixel
geometry (position, box size, wrapped text lines) for every node/region in
the diagram. The frontend (web/app.js) just draws whatever geometry it's
given — no layout/wrapping logic duplicated client-side, no risk of the two
disagreeing about whether something overlaps.

The PG Internals Expert agent decides *which* nodes/edges/regions exist and
what they say (grounded in the matching reference diagram + real EXPLAIN
facts); this module turns that content into non-overlapping pixel geometry,
the same "structure=deterministic, wording=LLM" split used throughout this
project.
"""
CHAR_W = 10.5  # empirically tuned against the Virgil hand-drawn font (see web/app.js history)
TITLE_LINE_H = 19
DETAIL_LINE_H = 14
BOX_PAD = 10
MAX_CHARS = 24
ROW_GAP = 18
REGION_LABEL_H = 40

# Fixed left-to-right column layout, matching the reference diagrams' own
# spatial arrangement (client/postmaster at far left, backend process next,
# then local memory, shared memory, disk at far right). Not every region is
# used by every query kind (e.g. a trivial SELECT may not need "disk").
REGION_GEOMETRY = {
    "client": {"x": 0, "w": 220},
    "backend_process": {"x": 260, "w": 400},
    "local_memory": {"x": 700, "w": 320},
    "shared_memory": {"x": 1060, "w": 360},
    "disk": {"x": 1460, "w": 400},
}
DEFAULT_REGION = "backend_process"


def wrap_text(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    words = str(text).split()
    lines: list[str] = []
    cur = ""
    for w in words:
        attempt = f"{cur} {w}".strip()
        if len(attempt) > max_chars and cur:
            lines.append(cur)
            cur = w
        else:
            cur = attempt
    if cur:
        lines.append(cur)
    return lines or [""]


def layout_box(label: str, detail: list[str] | None, min_width: int = 150, max_chars: int = MAX_CHARS) -> dict:
    """Returns {"w", "h", "lines": [{"text", "bold"}]} for one box."""
    title_lines = [{"text": t, "bold": True} for t in wrap_text(label, max_chars)]
    detail_lines = [{"text": t, "bold": False} for d in (detail or []) for t in wrap_text(d, max_chars)]
    lines = title_lines + detail_lines
    longest = max([min_width / CHAR_W, 8] + [len(l["text"]) for l in lines])
    w = max(min_width, round(longest * CHAR_W) + BOX_PAD * 2)
    h = (
        BOX_PAD * 2
        + len(title_lines) * TITLE_LINE_H
        + len(detail_lines) * DETAIL_LINE_H
        + (DETAIL_LINE_H if detail_lines else 0)
    )
    return {"w": w, "h": h, "lines": lines}


class RegionDiagramBuilder:
    """Accumulates regions (dashed background containers) and nodes stacked
    within them at stable, non-overlapping positions assigned once."""

    def __init__(self):
        self.regions: dict[str, dict] = {}
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._cursor_y: dict[str, int] = {}

    def add_region(self, key: str, label: str) -> dict:
        if key in self.regions:
            return self.regions[key]
        geom = REGION_GEOMETRY.get(key, REGION_GEOMETRY[DEFAULT_REGION])
        region = {"id": key, "label": label, "x": geom["x"], "y": 0, "w": geom["w"], "h": REGION_LABEL_H}
        self.regions[key] = region
        self._cursor_y[key] = REGION_LABEL_H
        return region

    def add_node(self, node_id: str, label: str, detail: list[str] | None, kind: str, region_key: str) -> dict:
        if node_id in self.nodes:
            return self.nodes[node_id]
        region = self.regions.get(region_key) or self.add_region(region_key, region_key.replace("_", " ").upper())
        layout = layout_box(label, detail, min_width=region["w"] - 2 * BOX_PAD - 20)
        x = region["x"] + (region["w"] - layout["w"]) / 2
        y = self._cursor_y[region_key]
        node = {
            "id": node_id,
            "label": label,
            "detail": detail or [],
            "kind": kind,
            "region": region_key,
            "x": round(x),
            "y": round(y),
            "w": layout["w"],
            "h": layout["h"],
            "lines": layout["lines"],
        }
        self.nodes[node_id] = node
        self._cursor_y[region_key] = y + layout["h"] + ROW_GAP
        region["h"] = max(region["h"], self._cursor_y[region_key])
        return node

    def add_edge(self, frm: str, to: str, label: str) -> dict:
        edge = {"id": f"{frm}->{to}", "from": frm, "to": to, "label": label}
        self.edges.append(edge)
        return edge

    def to_dict(self) -> dict:
        return {"regions": list(self.regions.values()), "nodes": list(self.nodes.values()), "edges": self.edges}


def seed_intro_nodes(builder: RegionDiagramBuilder, backend_pid: int | None) -> None:
    """Pre-seeds the connection/parse chain everyone's query needs, so the
    Expert agent only has to author the query-specific parts. Fixed ids
    (client/postmaster/backend/snapshot) are referenced by the Expert's own
    steps and edges."""
    builder.add_region("client", "CLIENT")
    builder.add_region("backend_process", "BACKEND PROCESS")

    builder.add_node("client", "Client (psql / app)", ["over libpq"], "client", "client")
    builder.add_node("postmaster", "Postmaster (listens on :5432)", ["accepts the connection"], "server", "backend_process")
    builder.add_node(
        "backend",
        f"Backend process (PID {backend_pid})" if backend_pid else "Backend process",
        ["forked one per client connection"],
        "process",
        "backend_process",
    )
    builder.add_node("snapshot", "MVCC snapshot", ["taken at statement/transaction start"], "memory", "backend_process")

    builder.add_edge("client", "postmaster", "1  connect")
    builder.add_edge("postmaster", "backend", "fork backend")
    builder.add_edge("backend", "snapshot", "2  obtain snapshot")
