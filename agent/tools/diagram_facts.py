"""Deterministic, Python-computed facts handed to the Visual Critique agent
as text — no vision model involved. Because `diagram_layout.py` stacks
nodes within fixed, non-overlapping regions, geometric overlap is mostly
ruled out by construction; what's actually worth a human (or LLM) critique
is content completeness against the matching reference diagram and jargon
that would confuse the target audience."""
import re

from agent.tools.reference_diagrams import map_region_key

# Bare PostgreSQL-internal jargon that should never appear as a naked,
# untranslated label — mirrors the "process/data-movement, not jargon"
# framing used throughout this project's prompts.
_JARGON_TERMS = [
    "seq scan", "index scan", "plan tree", "planner", "optimizer",
    "executor node", "modifytable", "nodetag",
]


def _find_jargon(text: str) -> list[str]:
    low = text.lower()
    return [term for term in _JARGON_TERMS if term in low]


_SEEDED_NODE_IDS = {"client", "postmaster", "backend", "snapshot"}


def compute_diagram_facts(diagram: dict, reference_summary: dict) -> dict:
    """Returns objective, structured facts for the critique prompt:
    - jargon_hits: {node_id: [terms]} for any node whose label/detail contains raw jargon
    - region_coverage: which reference regions are present/missing (by loose name match)
    - node_count / reference_region_count for a rough richness comparison
    - orphan_edges: edges referencing a node id that doesn't exist (a real bug, not a style nit)
    - authored_node_count / reference_item_count / richness_ratio: whether the
      draft is drastically sparser than the reference (the single most common
      failure mode observed for this prompt — a local model paraphrasing the
      reference into a handful of boxes instead of translating it item-by-item)
    """
    node_ids = {n["id"] for n in diagram.get("nodes", [])}

    jargon_hits = {}
    for node in diagram.get("nodes", []):
        text = node["label"] + " " + " ".join(node.get("detail", []))
        hits = _find_jargon(text)
        if hits:
            jargon_hits[node["id"]] = hits

    # Regions mapping to `backend_process` or `None` (client) are
    # intentionally NEVER in `diagram["regions"]` — they're pre-seeded by
    # the app, not authored by the Expert (see PG_EXPERT_INSTRUCTION), so
    # comparing against draft regions would always flag them "missing" even
    # when the Expert did exactly the right thing by not redeclaring them.
    ref_region_labels = [
        r["label"] for r in reference_summary.get("regions", [])
        if map_region_key(r["label"], r["items"]) not in (None, "backend_process")
    ]
    diagram_region_labels = [r["label"].upper() for r in diagram.get("regions", [])]
    missing_regions = [
        rl for rl in ref_region_labels
        if not any(_loose_match(rl, dl) for dl in diagram_region_labels)
    ]

    orphan_edges = [
        e["id"] for e in diagram.get("edges", [])
        if e["from"] not in node_ids or e["to"] not in node_ids
    ]

    reference_item_count = sum(len(r["items"]) for r in reference_summary.get("regions", []))
    authored_node_count = len([n for n in diagram.get("nodes", []) if n["id"] not in _SEEDED_NODE_IDS])
    richness_ratio = round(authored_node_count / reference_item_count, 2) if reference_item_count else 1.0

    return {
        "jargon_hits": jargon_hits,
        "reference_region_count": len(ref_region_labels),
        "diagram_region_count": len(diagram.get("regions", [])),
        "missing_regions_vs_reference": missing_regions,
        "reference_step_count": len(reference_summary.get("steps", [])),
        "diagram_node_count": len(diagram.get("nodes", [])),
        "reference_item_count": reference_item_count,
        "authored_node_count": authored_node_count,
        "richness_ratio": richness_ratio,
        "orphan_edges": orphan_edges,
        "has_legend": bool(diagram.get("legend")),
    }


def _loose_match(a: str, b: str) -> bool:
    a_words = set(re.findall(r"[A-Z]{3,}", a.upper()))
    b_words = set(re.findall(r"[A-Z]{3,}", b.upper()))
    return bool(a_words & b_words)
