"""ADK agent definition — frozen v10+ architecture.

`pg_expert_agent` is the entire product: one self-contained call that takes
just the SQL (and optional DDL) — no docker, no sandbox, no real EXPLAIN
execution — and does the EXPLAIN itself, by reasoning about PostgreSQL's
real planner/executor behavior (see PART 0 of
`agent/prompts.py::PG_EXPERT_INSTRUCTION`), then produces a *complete*
Excalidraw diagram plus an unlimited-length slideshow in one response. It
also recommends at most one index; when it does, `run_direct_expert` calls
it a second time assuming that index exists, to produce a genuine
before/after comparison — both sides reasoned, neither one measured
against a real database.

Runs on an advanced/high-reasoning Claude model (see `_CLAUDE_MODEL`)
since it now does meaningfully more analytical work (guessing a realistic
query plan, plus drawing a faithful diagram) than earlier versions of this
project did. `agent/tools/layout_fixup.py::resolve_overlaps` is the only
deterministic post-processing step left — a safety net over the Expert's
own pixel layout, not a replacement for it.
"""
import json
import os
import re
from typing import Awaitable, Callable

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import InMemoryRunner
from google.genai import types

from agent.prompts import PG_EXPERT_INSTRUCTION
from agent.tools.layout_fixup import resolve_overlaps

# The Expert single-handedly reasons out a realistic EXPLAIN plan AND draws
# the diagram (see PART 0 of PG_EXPERT_INSTRUCTION) — real analytical work,
# not just drawing, hence an advanced/high-reasoning model rather than a
# mid-tier one. Requires ANTHROPIC_API_KEY (see README/.env.example;
# separate, pay-as-you-go API billing — not covered by a claude.ai/Claude
# Code subscription).
_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

# One Expert call now has to do a LOT (reason out a full plan, draw a
# complete diagram, write potentially 20-30+ narrated slides) — litellm's
# own default request timeout is 600s, which a response this big can
# genuinely exceed; it was measured actually failing at exactly 600.0s in
# testing, silently falling back to an empty diagram (the timeout is
# caught by run_expert_full_generation's broad except, so nothing crashes
# — it just looks like the model returned nothing). `types.HttpOptions.
# timeout` is documented as milliseconds, but ADK's LiteLlm passes it
# straight through to litellm's `timeout` kwarg with NO ms->s conversion
# (verified by reading google/adk/models/lite_llm.py) — litellm's own
# `timeout` is in seconds, so the number set here IS seconds, not
# milliseconds, despite what the field name/docstring implies.
_EXPERT_TIMEOUT_SECONDS = int(os.environ.get("EXPERT_TIMEOUT_SECONDS", "900"))

StatusCallback = Callable[[str], Awaitable[None]]

pg_expert_agent = Agent(
    name="pg_internals_expert",
    # claude-opus-4-8 only accepts temperature=1 — any other value is a
    # hard litellm.UnsupportedParamsError.
    model=LiteLlm(model=f"anthropic/{_CLAUDE_MODEL}"),
    generate_content_config=types.GenerateContentConfig(
        temperature=1.0,
        http_options=types.HttpOptions(timeout=_EXPERT_TIMEOUT_SECONDS),
    ),
    instruction=PG_EXPERT_INSTRUCTION,
)

# ADK requires a root_agent to be discoverable when run via `adk web` for
# manual debugging; requests are handled programmatically via
# `run_direct_expert` instead.
root_agent = pg_expert_agent


async def _run_agent_turn(agent: Agent, prompt: str) -> str:
    runner = InMemoryRunner(agent=agent, app_name="pg-internals-visualizer")
    user_id, session_id = "local-user", "session-1"
    await runner.session_service.create_session(
        app_name="pg-internals-visualizer", user_id=user_id, session_id=session_id
    )
    message = types.Content(role="user", parts=[types.Part(text=prompt)])

    final_text = ""
    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=message):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(p.text or "" for p in event.content.parts)
    return final_text


# ---- Expert: single self-contained call (full diagram + slideshow) ----

def _extract_diagram_and_slides(text: str) -> tuple[dict, list[dict], dict | None]:
    """PG_EXPERT_INSTRUCTION asks for two raw JSON objects back to back
    (diagram, then a newline, then `{"slides": [...], "recommendation": ...}`)
    with no markdown fences — parse defensively anyway (strip a fence if one
    slips in), then use `raw_decode` to peel off the first JSON value and
    parse the remainder as the second, rather than assuming a single `{...}`
    blob."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    diagram, idx = decoder.raw_decode(text)
    remainder = text[idx:].strip()
    slideshow, _ = decoder.raw_decode(remainder)
    return diagram, slideshow.get("slides", []), slideshow.get("recommendation")


def _fallback_diagram_and_slides(sql: str) -> tuple[dict, list[dict], dict | None]:
    diagram = {"type": "excalidraw", "elements": []}
    slides = [
        {
            "slide": 1,
            "title": "Query executed",
            "focus_box_ids": [],
            "viewport": None,
            "narration": f"PostgreSQL executed: {sql.strip()[:200]}",
        }
    ]
    return diagram, slides, None


def _expert_prompt(sql: str, isolation_level: str, ddl: str | None, assume_index: str | None) -> str:
    parts = [
        f"SQL query:\n{sql}",
        f"isolation_level: {isolation_level}",
    ]
    if ddl:
        parts.append(f"DDL/schema for the tables involved:\n{ddl}")
    else:
        parts.append(
            "No DDL/schema was given — infer a plausible one yourself from how "
            "the query uses each table/column (per PART 0), and note that "
            "assumption briefly in the diagram."
        )
    if assume_index:
        parts.append(
            f"This is the AFTER pass (see BEFORE/AFTER in PART 2): assume this index "
            f"now exists and is used wherever it genuinely helps this query: "
            f"{assume_index}\nReason out how the plan changes with it present, and draw "
            f"that new plan — set recommendation to null on this pass."
        )
    else:
        parts.append(
            "This is the BEFORE pass (see BEFORE/AFTER in PART 2): assume no index "
            "exists on the columns this query filters/joins/orders on, unless the "
            "given DDL explicitly declares one."
        )
    return "\n\n".join(parts)


async def run_expert_full_generation(
    sql: str,
    isolation_level: str,
    ddl: str | None,
    assume_index: str | None = None,
    on_status: StatusCallback | None = None,
) -> tuple[dict, list[dict], dict | None]:
    """Single Expert call: the whole diagram + slideshow (+ a recommendation
    on the "before" pass) in one response — no real EXPLAIN execution, no
    reference grounding, no critique/revise round. The Expert reasons out
    its own realistic plan (PART 0 of PG_EXPERT_INSTRUCTION) and does its
    own self-check before emitting. Never raises: any LLM/parse failure
    falls back to a minimal safe diagram, so the pipeline is never blocked."""
    if on_status:
        status = (
            "Applying the recommended index and re-imagining the plan..."
            if assume_index
            else "Reasoning through the query plan and building a visualization..."
        )
        await on_status(status)
    try:
        text = await _run_agent_turn(pg_expert_agent, _expert_prompt(sql, isolation_level, ddl, assume_index))
        return _extract_diagram_and_slides(text)
    except Exception as exc:
        print(f"run_expert_full_generation: failed, using fallback ({exc})")
        return _fallback_diagram_and_slides(sql)


def materialize_diagram(diagram: dict, slides: list[dict]) -> tuple[dict, list[dict]]:
    """Turns the Expert's own fully-positioned `elements` array into a dict
    keyed by id (what the frontend renderer expects), runs the deterministic
    overlap-resolution safety net over it (agent/tools/layout_fixup.py —
    the Expert owns layout, this only nudges apart what didn't come out
    right), and turns slides into steps with resolved `element_ids`."""
    elements = {el["id"]: el for el in diagram.get("elements") or [] if el.get("id")}
    resolve_overlaps(elements)

    steps = []
    for i, s in enumerate(slides):
        steps.append(
            {
                "index": i,
                "title": s.get("title", ""),
                "narration": s.get("narration", ""),
                "element_ids": [eid for eid in s.get("focus_box_ids") or [] if eid in elements],
                "viewport": s.get("viewport"),
            }
        )
    if not steps:
        steps = [{"index": 0, "title": "Result", "narration": "The query executed.", "element_ids": [], "viewport": None}]
    return {"elements": elements}, steps


async def run_direct_expert(sql: str, ddl: str | None, isolation_level: str, on_status: StatusCallback) -> dict:
    """The whole product: no orchestrator, no docker, no sandbox, no real
    EXPLAIN — the Expert reasons out its own plan and draws it, twice if it
    recommends an index: once assuming no index (before), once assuming
    that index exists (after)."""
    try:
        before_raw, before_slides, recommendation = await run_expert_full_generation(
            sql, isolation_level, ddl, assume_index=None, on_status=on_status
        )
    except Exception as exc:
        print(f"run_direct_expert: before pass failed ({exc})")
        return {"status": "error", "message": "Something went wrong while building the visualization."}

    before_diagram, before_steps = materialize_diagram(before_raw, before_slides)

    after_diagram, after_steps, recommendations = None, None, []
    if recommendation and recommendation.get("index_sql"):
        try:
            after_raw, after_slides, _ = await run_expert_full_generation(
                sql, isolation_level, ddl, assume_index=recommendation["index_sql"], on_status=on_status
            )
            after_diagram, after_steps = materialize_diagram(after_raw, after_slides)
            recommendations.append(
                {"index_sql": recommendation.get("index_sql", ""), "rationale": recommendation.get("rationale", "")}
            )
        except Exception as exc:
            print(f"run_direct_expert: after pass failed, showing before-only ({exc})")

    return {
        "status": "success",
        "schema_note": None,
        "before_diagram": before_diagram,
        "before_steps": before_steps,
        "before_stats": {"planning_time_ms": None, "execution_time_ms": None},
        "after_diagram": after_diagram,
        "after_steps": after_steps,
        "after_stats": {"planning_time_ms": None, "execution_time_ms": None} if after_diagram else None,
        "recommendations": recommendations,
    }
