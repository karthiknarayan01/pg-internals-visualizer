"""Deterministic safety net over the Expert's own pixel layout.

The Expert (agent/prompts.py::PG_EXPERT_INSTRUCTION) now owns full pixel
geometry for the diagram it draws — a deliberate v9/v10 architecture choice
to let it control everything about the visual. In practice, even with
explicit spacing/overlap instructions and a "mentally verify" self-check,
an LLM asked to hand-place dozens of absolute x/y/width/height values
sometimes gets pairs of boxes overlapping or text that doesn't fit its own
box. Rather than re-introducing full Python-computed layout (rejected
twice this project — see memory/README history), this module only nudges
things apart *after* the fact: box positions are corrected to remove
overlap while preserving the Expert's own composition/order as closely as
possible, and text boxes are resized to actually fit their own content.

Deliberately NOT attempted here: routing arrows around obstacles (a much
bigger algorithmic problem — visibility graphs / pathfinding) or reflowing
the whole diagram. If overlap is still visually bad after this pass, the
next lever is asking the Expert for a sparser diagram, not more Python
layout logic here.
"""
import math

MIN_GAP = 16.0
MAX_ITERATIONS = 60
# Rough px-per-character estimate at a given fontSize, matching the same
# "assume each character is ~0.6x the font size wide" rule the prompt
# itself now states, so this pass agrees with what the Expert was told.
CHAR_WIDTH_RATIO = 0.6
LINE_HEIGHT_RATIO = 1.3


def _rects_overlap(a: dict, b: dict) -> bool:
    return not (
        a["x"] + a["width"] <= b["x"]
        or b["x"] + b["width"] <= a["x"]
        or a["y"] + a["height"] <= b["y"]
        or b["y"] + b["height"] <= a["y"]
    )


def _contains(outer: dict, inner: dict) -> bool:
    return (
        outer["x"] <= inner["x"]
        and outer["y"] <= inner["y"]
        and outer["x"] + outer["width"] >= inner["x"] + inner["width"]
        and outer["y"] + outer["height"] >= inner["y"] + inner["height"]
    )


def _fix_text_fit(elements: dict) -> None:
    """Grows (never shrinks) a text element's own box if its content
    wouldn't actually fit at its stated fontSize — the single biggest
    cause of "text spilling into the next box.\""""
    for el in elements.values():
        if el.get("type") != "text":
            continue
        text = str(el.get("text", ""))
        lines = text.split("\n")
        font_size = el.get("fontSize") or 16
        line_height = el.get("lineHeight") or 1.25
        needed_h = len(lines) * font_size * line_height * (LINE_HEIGHT_RATIO / 1.25)
        if el.get("height", 0) < needed_h:
            el["height"] = needed_h
        longest = max((len(l) for l in lines), default=0)
        needed_w = longest * font_size * CHAR_WIDTH_RATIO
        if el.get("width", 0) < needed_w:
            el["width"] = needed_w


def _resolve_rect_overlaps(rects: dict) -> dict[str, list[float]]:
    """Iteratively separates overlapping, non-nested rectangles by pushing
    each pair apart along whichever axis has the smaller overlap. Returns
    the accumulated (dx, dy) per rect id, so callers can move dependent
    elements (text, arrow endpoints) by the same amount."""
    moved = {rid: [0.0, 0.0] for rid in rects}
    ids = list(rects.keys())
    for _ in range(MAX_ITERATIONS):
        any_overlap = False
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = rects[ids[i]], rects[ids[j]]
                if _contains(a, b) or _contains(b, a):
                    continue  # intentional parent/child nesting — never separated
                if not _rects_overlap(a, b):
                    continue
                any_overlap = True
                ax1, ay1 = a["x"], a["y"]
                ax2, ay2 = ax1 + a["width"], ay1 + a["height"]
                bx1, by1 = b["x"], b["y"]
                bx2, by2 = bx1 + b["width"], by1 + b["height"]
                overlap_x = min(ax2, bx2) - max(ax1, bx1)
                overlap_y = min(ay2, by2) - max(ay1, by1)
                push = MIN_GAP / 2 + 1
                if overlap_x < overlap_y:
                    dirn = 1 if (ax1 + ax2) < (bx1 + bx2) else -1
                    delta = dirn * (overlap_x / 2 + push)
                    a["x"] -= delta
                    b["x"] += delta
                    moved[ids[i]][0] -= delta
                    moved[ids[j]][0] += delta
                else:
                    dirn = 1 if (ay1 + ay2) < (by1 + by2) else -1
                    delta = dirn * (overlap_y / 2 + push)
                    a["y"] -= delta
                    b["y"] += delta
                    moved[ids[i]][1] -= delta
                    moved[ids[j]][1] += delta
        if not any_overlap:
            break
    return moved


def _nearest_rect_id(rects: dict, px: float, py: float) -> str | None:
    best_id, best_dist = None, math.inf
    for rid, r in rects.items():
        cx, cy = r["x"] + r["width"] / 2, r["y"] + r["height"] / 2
        d = (px - cx) ** 2 + (py - cy) ** 2
        if d < best_dist:
            best_id, best_dist = rid, d
    return best_id


def resolve_overlaps(elements: dict) -> dict:
    """Mutates and returns `elements` (a dict keyed by id, as produced by
    `agent.agent.materialize_diagram`): separates overlapping non-nested
    rectangles, grows text boxes that don't fit their own content, and
    shifts owned text / arrow endpoints along with whatever rectangle they
    were originally anchored to, so the diagram stays visually coherent
    after the nudge instead of leaving labels/arrows behind."""
    rects = {eid: e for eid, e in elements.items() if e.get("type") == "rectangle"}
    texts = {eid: e for eid, e in elements.items() if e.get("type") == "text"}
    arrows = {eid: e for eid, e in elements.items() if e.get("type") == "arrow"}

    if not rects:
        _fix_text_fit(elements)
        return elements

    # Snapshot which rect (if any) each text/arrow-endpoint was closest to
    # BEFORE moving anything, so post-move deltas can be applied to keep
    # them visually attached to "their" box.
    text_owner = {}
    for tid, t in texts.items():
        tcx = t.get("x", 0) + t.get("width", 0) / 2
        tcy = t.get("y", 0) + t.get("height", 0) / 2
        owners = [rid for rid, r in rects.items() if _contains(r, {"x": tcx, "y": tcy, "width": 0, "height": 0})]
        if owners:
            text_owner[tid] = min(owners, key=lambda rid: rects[rid]["width"] * rects[rid]["height"])

    arrow_anchors = {}
    for aid, a in arrows.items():
        pts = a.get("points") or [[0, 0], [0, 0]]
        x1, y1 = a.get("x", 0) + pts[0][0], a.get("y", 0) + pts[0][1]
        x2, y2 = a.get("x", 0) + pts[-1][0], a.get("y", 0) + pts[-1][1]
        arrow_anchors[aid] = (_nearest_rect_id(rects, x1, y1), _nearest_rect_id(rects, x2, y2))

    moved = _resolve_rect_overlaps(rects)

    for tid, rid in text_owner.items():
        dx, dy = moved.get(rid, [0.0, 0.0])
        texts[tid]["x"] = texts[tid].get("x", 0) + dx
        texts[tid]["y"] = texts[tid].get("y", 0) + dy

    for aid, (r0, r1) in arrow_anchors.items():
        a = arrows[aid]
        dx0, dy0 = moved.get(r0, [0.0, 0.0]) if r0 else [0.0, 0.0]
        dx1, dy1 = moved.get(r1, [0.0, 0.0]) if r1 else [0.0, 0.0]
        if dx0 == dy0 == dx1 == dy1 == 0.0:
            continue
        # Shifting the arrow's own origin by (dx0, dy0) already moves every
        # point — including point 0 — by that amount, so point 0's relative
        # coords stay untouched; only the END point needs an *additional*
        # relative adjustment of (dx1-dx0) to land on its own target delta
        # of dx1 instead of dx0. (Any middle bend point is left as-is: an
        # approximation — it moves rigidly with the start, not
        # re-interpolated — acceptable for a nudge pass, not a full
        # re-route.)
        pts = a.get("points") or [[0, 0], [0, 0]]
        new_pts = [list(p) for p in pts]
        end_dx, end_dy = dx1 - dx0, dy1 - dy0
        new_pts[-1] = [pts[-1][0] + end_dx, pts[-1][1] + end_dy]
        a["x"] = a.get("x", 0) + dx0
        a["y"] = a.get("y", 0) + dy0
        a["points"] = new_pts

    _fix_text_fit(elements)
    return elements
