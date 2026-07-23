"""Parses the hand-authored .excalidraw reference diagrams (one per common
SQL statement shape) into compact structural summaries — region names and
their contents, ordered step labels, arrow/flow labels, and the legend.

Never hands raw pixel-precise Excalidraw JSON to an LLM: local models are
unreliable at reasoning over exact coordinates, and the content (what
regions/steps/labels exist and how they relate) is what actually grounds a
good diagram, not the pixel data.
"""
import json
import re
from pathlib import Path

REFERENCE_DIR = Path(__file__).parent.parent.parent / "reference-set-pg-diagrams"

_FILES = {
    "select_simple": "01_select_simple.excalidraw",
    "select_join": "02_select_join.excalidraw",
    "insert": "03_insert.excalidraw",
    "update": "04_update.excalidraw",
    "delete": "05_delete.excalidraw",
    "transaction": "06_transaction_commit.excalidraw",
}

_STEP_RE = re.compile(r"^(t\d+\+?|\d+[a-z]?)[\.\)]?\s")
_HEADING_RE = re.compile(r"^[A-Z][A-Z0-9 /()+_'-]{2,40}$")


def _rect_contains(rect: dict, x: float, y: float) -> bool:
    return rect["x"] <= x <= rect["x"] + rect["width"] and rect["y"] <= y <= rect["y"] + rect["height"]


def _parse_file(path: Path) -> dict:
    data = json.loads(path.read_text())
    elements = data["elements"]

    regions = [
        e for e in elements if e["type"] == "rectangle" and e.get("strokeStyle") == "dashed" and e["width"] > 150 and e["height"] > 150
    ]
    texts = [e for e in elements if e["type"] == "text" and e.get("text")]

    # A region's heading is the shortest, most-uppercase text near its top.
    region_summaries = []
    claimed_text_ids = set()
    for rect in regions:
        candidates = [t for t in texts if _rect_contains(rect, t["x"], t["y"]) and t["y"] < rect["y"] + 40]
        heading_candidates = [t for t in candidates if _HEADING_RE.match(t["text"].split("\n")[0])]
        pool = heading_candidates or candidates
        heading = min(pool, key=lambda t: len(t["text"])) if pool else None
        label = heading["text"].split("\n")[0] if heading else "Region"
        if heading:
            claimed_text_ids.add(heading["id"])

        members = sorted(
            (t for t in texts if _rect_contains(rect, t["x"], t["y"]) and t["id"] not in claimed_text_ids),
            key=lambda t: (t["y"], t["x"]),
        )
        items = []
        for t in members:
            claimed_text_ids.add(t["id"])
            items.append(t["text"].replace("\n", " "))
        region_summaries.append({"label": label, "items": items})

    steps = []
    for t in sorted(texts, key=lambda t: (t["y"], t["x"])):
        first_line = t["text"].split("\n")[0]
        if _STEP_RE.match(first_line):
            steps.append(t["text"].replace("\n", " "))
            claimed_text_ids.add(t["id"])

    legend_items = []
    legend_heading = next((t for t in texts if t["text"].strip() == "Legend"), None)
    if legend_heading:
        claimed_text_ids.add(legend_heading["id"])
        for t in texts:
            if t["id"] in claimed_text_ids:
                continue
            if abs(t["x"] - legend_heading["x"]) < 60 and 0 < t["y"] - legend_heading["y"] < 200:
                legend_items.append(t["text"].replace("\n", " "))
                claimed_text_ids.add(t["id"])

    return {
        "regions": region_summaries,
        "steps": steps,
        "legend": legend_items,
    }


def map_region_key(region_label: str, items: list[str] | None = None) -> str | None:
    """Reference diagrams label regions in the book's own words (e.g.
    "BACKEND PROCESS  (PID 101)", "LOCAL (per-backend) MEMORY"), which don't
    match the app's fixed region keys verbatim. Mapping this deterministically
    in Python — instead of asking the LLM to infer it — removes an entire
    class of error: a prior version of the Expert prompt left this to the
    model's judgment and it sometimes created a *second*, differently-keyed
    region whose label duplicated the app's pre-seeded "BACKEND PROCESS"
    region instead of reusing it.

    Checks "PROCESS" rather than "BACKEND" deliberately — "LOCAL
    (per-backend) MEMORY" and "SHARED MEMORY (visible to all backends)"
    both contain the substring "backend" without being about the backend
    PROCESS region, which a naive "BACKEND" in label check misclassified.

    Returns None for a region the app has no Expert-authorable equivalent
    for (e.g. the reference's own "CLIENT" region — the client node/edge are
    already pre-seeded, so there's nothing for the Expert to add there)."""
    up = region_label.upper()
    if "PROCESS" in up:
        return "backend_process"
    if "CLIENT" in up:
        return None
    if "SHARED" in up:
        return "shared_memory"
    if "DISK" in up:
        return "disk"
    if "LOCAL" in up or "MEMORY" in up:
        return "local_memory"
    # Label alone gave no signal (e.g. a plain "commit:" heading in the
    # update/delete reference files) — sniff the region's own content
    # instead of guessing blind.
    combined = " ".join(items or []).upper()
    if "DISK" in combined or "WAL" in combined or "FSYNC" in combined:
        return "disk"
    if "SHARED" in combined or "BUFFER POOL" in combined or "CLOG" in combined:
        return "shared_memory"
    return "local_memory"


def build_region_checklist(reference_summary: dict, max_item_chars: int = 110) -> list[str]:
    """Flattens every reference region's items into `"[region_key] item
    text"` strings, pre-mapped to the app's actual region keys. Handed to
    the Expert as a concrete checklist to translate item-by-item (keep/adapt
    or skip-if-inapplicable) rather than a JSON blob it has to summarize from
    scratch — the former is far more reliably followed by a local model."""
    checklist = []
    for region in reference_summary.get("regions", []):
        key = map_region_key(region["label"], region["items"])
        if key is None:
            continue
        for item in region["items"]:
            checklist.append(f"[{key}] {item[:max_item_chars]}")
    return checklist


_CACHE: dict[str, dict] = {}


def get_reference_summary(query_kind: str) -> dict:
    """Compact structural summary for one query kind: {"regions": [...],
    "steps": [...], "legend": [...]}. Parsed once and cached; falls back to
    select_simple if the kind is unrecognized."""
    filename = _FILES.get(query_kind, _FILES["select_simple"])
    if filename not in _CACHE:
        _CACHE[filename] = _parse_file(REFERENCE_DIR / filename)
    return _CACHE[filename]


_JOIN_RE = re.compile(r"\bjoin\b", re.IGNORECASE)
_KIND_PATTERNS = [
    (re.compile(r"^\s*(begin|commit|start\s+transaction)\b", re.IGNORECASE), "transaction"),
    (re.compile(r"^\s*insert\b", re.IGNORECASE), "insert"),
    (re.compile(r"^\s*update\b", re.IGNORECASE), "update"),
    (re.compile(r"^\s*delete\b", re.IGNORECASE), "delete"),
]


def classify_query_kind(sql: str) -> str:
    """Best-effort classification of a SQL statement into one of the six
    reference-diagram categories, used to pick which reference summary
    grounds the Expert's draft."""
    for pattern, kind in _KIND_PATTERNS:
        if pattern.match(sql):
            return kind
    if _JOIN_RE.search(sql):
        return "select_join"
    return "select_simple"
