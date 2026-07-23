"""Turns a real EXPLAIN ANALYZE plan tree into a compact text summary for
the LLM (cheaper and more reliable to reason over than the full JSON)."""


def summarize_plan(plan: dict) -> str:
    """One-line-per-node text summary of a plan tree, for feeding to the LLM
    as compact context (cheaper than the full JSON)."""
    lines = []

    def _rec(node: dict, depth: int) -> None:
        parts = [node.get("Node Type", "?")]
        if node.get("Relation Name"):
            parts.append(f'on "{node["Relation Name"]}"')
        if node.get("Filter"):
            parts.append(f'filter={node["Filter"]}')
        if node.get("Rows Removed by Filter"):
            parts.append(f'rows_removed={node["Rows Removed by Filter"]}')
        if node.get("Actual Total Time") is not None:
            parts.append(f'time={node["Actual Total Time"]}ms')
        lines.append("  " * depth + " ".join(parts))
        for child in node.get("Plans", []):
            _rec(child, depth + 1)

    _rec(plan, 0)
    return "\n".join(lines)
