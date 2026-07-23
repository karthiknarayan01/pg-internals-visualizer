"""ADK agent definitions for the v5 multi-agent architecture:

- `orchestrator_agent`-shaped agents (one built per request, tools bound to
  that request's `OrchestratorContext`) own the whole "answer a SQL query
  with a visualization" job — deciding, via real tool calls, to stand up the
  sandbox, resolve schema, run EXPLAIN, consult the PG Internals Expert, and
  (if it recommends an optimization) apply it and consult the Expert again.
- `pg_expert_agent` drafts/revises the diagram+steps JSON, grounded in real
  EXPLAIN facts, retrieved interdb.jp passages, and a matching reference
  diagram's structure.
- `critique_agent` reviews a draft against that reference structure plus
  Python-computed objective facts (jargon, orphan edges, missing regions)
  and either approves it or returns specific feedback for the Expert to
  address, capped at a small number of rounds.

Mirrors the "structure=deterministic Python, wording=LLM" split used
throughout this project: `agent/tools/diagram_layout.py` is the only thing
that ever assigns pixel geometry; the LLM only decides content.
"""
import json
import os
import re
from typing import Awaitable, Callable

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import InMemoryRunner
from google.genai import types

from agent.prompts import CRITIQUE_INSTRUCTION, ORCHESTRATOR_INSTRUCTION, PG_EXPERT_INSTRUCTION
from agent.tools import explain_tool as et
from agent.tools import optimizer as opt
from agent.tools import postgres_sandbox as pgs
from agent.tools.diagram_facts import compute_diagram_facts
from agent.tools.diagram_layout import RegionDiagramBuilder, seed_intro_nodes
from agent.tools.knowledge_base import query_internals
from agent.tools.reference_diagrams import build_region_checklist, classify_query_kind, get_reference_summary

_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:14b")
_OLLAMA_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
MAX_CRITIQUE_ROUNDS = int(os.environ.get("MAX_CRITIQUE_ROUNDS", "1"))

StatusCallback = Callable[[str], Awaitable[None]]

pg_expert_agent = Agent(
    name="pg_internals_expert",
    model=LiteLlm(model=f"ollama_chat/{_OLLAMA_MODEL}", api_base=_OLLAMA_API_BASE),
    generate_content_config=types.GenerateContentConfig(temperature=0.2),
    instruction=PG_EXPERT_INSTRUCTION,
)

critique_agent = Agent(
    name="visual_critique",
    model=LiteLlm(model=f"ollama_chat/{_OLLAMA_MODEL}", api_base=_OLLAMA_API_BASE),
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    instruction=CRITIQUE_INSTRUCTION,
)

# ADK requires a root_agent to be discoverable when run via `adk web` for
# manual debugging; the orchestrator is built per-request instead (its
# tools are bound to that request's context) and invoked programmatically.
root_agent = pg_expert_agent


# ---- JSON/SQL extraction helpers ----

def _extract_json_object(text: str) -> dict:
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if match:
        text = match.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            text = text[start : end + 1]
    return json.loads(text)


_ALLOWED_STATEMENT_RE = re.compile(r"^\s*(CREATE\s+TABLE|CREATE\s+INDEX|INSERT)\b", re.IGNORECASE)


def _validate_inferred_sql(sql_text: str) -> bool:
    """Only allow CREATE TABLE / CREATE INDEX / INSERT statements — this SQL
    is LLM-generated and, even though it only ever runs inside a disposable
    per-request sandbox database, there's no reason to let it do anything
    beyond schema creation and data seeding."""
    statements = [s.strip() for s in sql_text.split(";") if s.strip()]
    if not statements:
        return False
    return all(_ALLOWED_STATEMENT_RE.match(s) for s in statements)


async def _run_agent_turn(agent: Agent, prompt: str) -> str:
    runner = InMemoryRunner(agent=agent, app_name="pg-explain-visualizer")
    user_id, session_id = "local-user", "session-1"
    await runner.session_service.create_session(
        app_name="pg-explain-visualizer", user_id=user_id, session_id=session_id
    )
    message = types.Content(role="user", parts=[types.Part(text=prompt)])

    final_text = ""
    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=message):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(p.text or "" for p in event.content.parts)
    return final_text


# ---- Grounding: pre-fetch KB passages relevant to the real plan's node types ----

_NODE_TYPE_CONCEPTS = {
    "Seq Scan": "sequential scan buffer pool buffer tag pin unpin descriptor page layout line pointer heap tuple header xmin xmax",
    "Index Scan": "btree index page structure lookup buffer pool buffer tag heap fetch by tid visibility map",
    "Index Only Scan": "btree index page structure lookup buffer pool buffer tag heap fetch by tid visibility map",
    "Bitmap Heap Scan": "bitmap index scan bitmap heap scan buffer pool page bitmap",
    "Bitmap Index Scan": "bitmap index scan bitmap heap scan buffer pool page bitmap",
    "Hash Join": "hash join hash table bucket work_mem probe build",
    "Nested Loop": "nested loop join inner outer scan repeated",
    "Merge Join": "merge join sorted inputs merge algorithm",
    "Hash": "hash table bucket structure work_mem build phase hash function",
    "Sort": "tuplesort work_mem in-memory sort external merge disk spill run",
    "Aggregate": "hash aggregate group hash table work_mem accumulate state",
    "HashAggregate": "hash aggregate group hash table work_mem accumulate state",
    "ModifyTable": "heap insert update delete write-ahead log wal buffer dirty page",
}
_DEFAULT_CONCEPTS = [
    "mvcc snapshot visibility transaction isolation xmin xmax",
    "postgresql process architecture buffer manager shared memory",
]


def _concepts_for_plan_summary(plan_summary: str) -> list[str]:
    hits = [concept for node_type, concept in _NODE_TYPE_CONCEPTS.items() if node_type in plan_summary]
    combined, seen, result = _DEFAULT_CONCEPTS + hits, set(), []
    for c in combined:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result[:5]


def _fetch_grounding_passages(plan_summary: str) -> list[str]:
    passages = []
    for concept in _concepts_for_plan_summary(plan_summary):
        result = query_internals(concept, n_results=1)
        if result.get("status") == "success":
            passages.extend(p["text"][:600] for p in result["passages"])
    return passages


# ---- Expert <-> Critique loop ----

def _fallback_draft(plan_summary: str) -> dict:
    return {
        "regions": [{"key": "backend_process", "label": "BACKEND PROCESS"}],
        "nodes": [
            {
                "id": "exec1",
                "label": "Backend executes the query",
                "detail": [plan_summary.strip().splitlines()[0][:60]] if plan_summary.strip() else [],
                "kind": "executor",
                "region": "backend_process",
            }
        ],
        "edges": [{"from": "snapshot", "to": "exec1", "label": "execute"}],
        "steps": [
            {
                "title": "Query execution",
                "narration": "The backend process executes the query plan.",
                "highlight_node_ids": ["exec1"],
                "highlight_edge_ids": [],
            }
        ],
        "legend": [],
        "recommendation": None,
    }


def _expert_prompt(sql, isolation_level, plan_summary, reference_summary, passages, previous_draft=None, feedback=None) -> str:
    checklist = build_region_checklist(reference_summary)
    min_nodes = max(6, round(0.6 * len(checklist)))
    # reference_summary's `regions` field would just duplicate `checklist`
    # below (same items, same source) — only steps/legend add information
    # the checklist doesn't already carry, so that's all that's sent. Fewer
    # redundant tokens per call matters here: this prompt is on the critical
    # path for both latency (MAX_CRITIQUE_ROUNDS) complaints and richness.
    shape_only = {"steps": reference_summary.get("steps", []), "legend": reference_summary.get("legend", [])}
    parts = [
        f"sql:\n{sql}",
        f"isolation_level: {isolation_level}",
        f"plan_summary:\n{plan_summary}",
        f"reference_steps_and_legend (for step titles and overall shape):\n{json.dumps(shape_only, indent=2)}",
        "reference_checklist (every content item from the matching reference diagram, "
        "each pre-tagged with the exact region key it belongs to — go through this list "
        "item by item, see REQUIRED WORKFLOW below):\n" + "\n".join(checklist),
        f"required_minimum_authored_nodes: {min_nodes}"
        f" (the checklist above has {len(checklist)} items; your `nodes` list, not counting"
        " client/postmaster/backend/snapshot, must have at least this many entries)",
        f"retrieved_passages:\n{json.dumps(passages, indent=2)}",
    ]
    if previous_draft is not None:
        parts.append(f"previous_draft:\n{json.dumps(previous_draft, indent=2)}")
        parts.append(f"feedback:\n{json.dumps(feedback, indent=2)}")
    return "\n\n".join(parts)


async def run_expert_critique_loop(
    sql: str, plan_summary: str, isolation_level: str, query_kind: str, on_status: StatusCallback | None = None
) -> dict:
    """Draft -> up to MAX_CRITIQUE_ROUNDS of (critique -> revise) -> return
    the final draft. Never raises: any LLM/parse failure at any point falls
    back to keeping the best draft so far (or a minimal safe one), so the
    pipeline is never blocked on this loop."""
    reference_summary = get_reference_summary(query_kind)
    passages = _fetch_grounding_passages(plan_summary)

    if on_status:
        await on_status("Building a visualization of what happened...")
    try:
        text = await _run_agent_turn(
            pg_expert_agent, _expert_prompt(sql, isolation_level, plan_summary, reference_summary, passages)
        )
        draft = _extract_json_object(text)
    except Exception as exc:
        print(f"run_expert_critique_loop: initial draft failed, using fallback ({exc})")
        return _fallback_draft(plan_summary)

    for i in range(MAX_CRITIQUE_ROUNDS):
        try:
            facts = compute_diagram_facts(draft, reference_summary)
            if on_status:
                await on_status("Double-checking the visualization for clarity...")
            # Only steps/legend, not the reference's raw region items —
            # `facts` (missing_regions_vs_reference, richness_ratio, etc.)
            # already distills the region/item-level comparison the critique
            # needs, so the full region text would just be redundant tokens.
            reference_shape = {"steps": reference_summary.get("steps", []), "legend": reference_summary.get("legend", [])}
            critique_text = await _run_agent_turn(
                critique_agent,
                f"draft:\n{json.dumps(draft, indent=2)}\n\nreference_steps_and_legend:\n{json.dumps(reference_shape, indent=2)}\n\ncomputed_facts:\n{json.dumps(facts, indent=2)}",
            )
            critique = _extract_json_object(critique_text)
            if critique.get("approved"):
                break
            if on_status:
                await on_status("Refining the visualization...")
            revise_text = await _run_agent_turn(
                pg_expert_agent,
                _expert_prompt(
                    sql, isolation_level, plan_summary, reference_summary, passages,
                    previous_draft=draft, feedback=critique.get("feedback", []),
                ),
            )
            draft = _extract_json_object(revise_text)
        except Exception as exc:
            print(f"run_expert_critique_loop: round {i} failed, keeping current draft ({exc})")
            break

    return draft


def materialize_diagram(draft: dict, backend_pid: int | None) -> tuple[dict, list[dict]]:
    """Turns the Expert's content-only draft into pixel-positioned diagram +
    steps JSON — the only place geometry is decided (never by the LLM)."""
    builder = RegionDiagramBuilder()
    seed_intro_nodes(builder, backend_pid)

    for region in draft.get("regions") or []:
        if region.get("key") and region.get("label"):
            builder.add_region(region["key"], region["label"])

    for node in draft.get("nodes") or []:
        if not node.get("id"):
            continue
        builder.add_node(
            node["id"],
            node.get("label", "?"),
            node.get("detail"),
            node.get("kind", "executor"),
            node.get("region", "backend_process"),
        )

    for edge in draft.get("edges") or []:
        if edge.get("from") in builder.nodes and edge.get("to") in builder.nodes:
            builder.add_edge(edge["from"], edge["to"], edge.get("label", ""))

    diagram = builder.to_dict()
    diagram["legend"] = draft.get("legend") or []

    steps = []
    for i, s in enumerate(draft.get("steps") or []):
        steps.append(
            {
                "index": i,
                "title": s.get("title", ""),
                "narration": s.get("narration", ""),
                "highlight_node_ids": [nid for nid in s.get("highlight_node_ids", []) if nid in builder.nodes],
                "highlight_edge_ids": s.get("highlight_edge_ids", []) or [],
                "stats": None,
            }
        )
    if not steps:
        steps = [
            {
                "index": 0,
                "title": "Result",
                "narration": "The query executed.",
                "highlight_node_ids": list(builder.nodes)[-1:],
                "highlight_edge_ids": [],
                "stats": None,
            }
        ]
    return diagram, steps


# ---- Orchestrator: per-request context + tool-bound agent ----

class OrchestratorContext:
    """One instance per /api/analyze request. Its methods are bound as the
    orchestrator agent's tools — plain state on `self` instead of a global,
    so concurrent requests never share sandbox/session state."""

    def __init__(self, sql: str, isolation_level: str, on_status: StatusCallback, user_supplied_ddl: bool):
        self.sql = sql
        self.isolation_level = isolation_level
        self.on_status = on_status
        self.user_supplied_ddl = user_supplied_ddl
        self.session_db: str | None = None
        self.plan: dict | None = None
        self.backend_pid: int | None = None
        self.stats: dict | None = None
        self.schema_note: str | None = None
        self.query_kind = classify_query_kind(sql)
        self.results: list[dict] = []
        self.pending_recommendation: dict | None = None
        self.terminal: dict | None = None

    async def _run_explain(self) -> dict:
        await self.on_status("Running your query against PostgreSQL...")
        result = et.run_explain_analyze(self.session_db, self.sql, self.isolation_level)
        if result["status"] != "success":
            return {"status": "error", "message": result.get("message")}
        self.plan = result["plan"]
        self.backend_pid = result["backend_pid"]
        self.stats = {
            "planning_time_ms": result["planning_time_ms"],
            "execution_time_ms": result["execution_time_ms"],
        }
        return {"status": "success"}

    async def _consult_pg_internals_expert(self) -> dict:
        plan_summary = opt.summarize_plan(self.plan)
        draft = await run_expert_critique_loop(
            self.sql, plan_summary, self.isolation_level, self.query_kind, self.on_status
        )
        diagram, steps = materialize_diagram(draft, self.backend_pid)
        recommendation = draft.get("recommendation")
        self.results.append(
            {"diagram": diagram, "steps": steps, "stats": dict(self.stats or {}), "recommendation": recommendation}
        )
        self.pending_recommendation = recommendation
        return {
            "status": "success",
            "has_recommendation": bool(recommendation),
            "recommendation_summary": (recommendation or {}).get("index_sql"),
        }

    async def analyze_query(self, ddl: str = "") -> dict:
        """Does everything needed to turn the SQL query into a visualization
        in one call: sets up the sandbox (first call only), applies `ddl` if
        given, checks whether the query resolves, and if so runs a real
        EXPLAIN and consults the PostgreSQL Internals Expert.

        Call this ONCE at the start with the user's DDL (or empty if none).
        If it returns status "needs_schema" and no DDL had been supplied yet,
        write your own CREATE TABLE + INSERT statements and call this AGAIN
        with your `ddl` — the sandbox and everything already resolved is
        kept, only the new schema is added.

        Returns: {"status": "needs_schema"|"error"|"success", "message": ...,
        "has_recommendation": bool, "recommendation_summary": str|None}.
        """
        if self.session_db is None:
            await self.on_status("Setting up a sandbox database for your query...")
            created = pgs.create_session_db()
            if created["status"] != "success":
                return {"status": "error", "message": created.get("message")}
            self.session_db = created["session_db"]

        if ddl:
            if not _validate_inferred_sql(ddl):
                return {"status": "error", "message": "generated schema contained a disallowed statement"}
            if not self.user_supplied_ddl:
                await self.on_status("Inferring a schema for your tables...")
            applied = pgs.apply_schema(self.session_db, ddl)
            if applied["status"] != "success":
                return {"status": "error", "message": applied.get("message")}
            if not self.user_supplied_ddl:
                self.schema_note = "Schema and sample data were auto-inferred from the query."

        resolved = pgs.check_query_resolves(self.session_db, self.sql)
        if resolved["status"] != "success":
            return resolved

        explained = await self._run_explain()
        if explained["status"] != "success":
            return explained

        return await self._consult_pg_internals_expert()

    async def apply_optimization_and_reanalyze(self) -> dict:
        """Applies the most recently recommended index, re-runs EXPLAIN for
        real, and consults the PG Internals Expert again — call this once,
        only if analyze_query said a recommendation exists."""
        if not self.pending_recommendation:
            return {"status": "error", "message": "no pending recommendation to apply"}
        await self.on_status("Applying the optimization and re-running your query...")
        index_sql = self.pending_recommendation.get("index_sql", "")
        applied = pgs.run_sql(self.session_db, index_sql)
        if applied["status"] != "success":
            return {"status": "error", "message": applied.get("message")}
        pgs.run_sql(self.session_db, "ANALYZE")
        self.pending_recommendation = None

        explained = await self._run_explain()
        if explained["status"] != "success":
            return explained
        return await self._consult_pg_internals_expert()

    def report_needs_schema(self, message: str) -> dict:
        """Call this and stop if the query still can't be resolved after
        your own schema guess."""
        self.terminal = {"status": "needs_schema", "message": message}
        return {"status": "acknowledged"}

    def report_error(self, message: str) -> dict:
        """Call this if something fails unexpectedly."""
        self.terminal = {"status": "error", "message": message}
        return {"status": "acknowledged"}


async def run_orchestrator(sql: str, ddl: str | None, isolation_level: str, on_status: StatusCallback) -> dict:
    """Builds a fresh orchestrator agent bound to a new per-request context,
    runs it, and returns the final API response shape. The orchestrator's
    own final text reply is never trusted for data — only the tool-call
    results captured on `ctx` are used, so a model that says something
    unexpected in its closing sentence can't corrupt the response."""
    ctx = OrchestratorContext(sql, isolation_level, on_status, user_supplied_ddl=bool(ddl))
    orchestrator_agent = Agent(
        name="orchestrator",
        model=LiteLlm(model=f"ollama_chat/{_OLLAMA_MODEL}", api_base=_OLLAMA_API_BASE),
        generate_content_config=types.GenerateContentConfig(temperature=0.0),
        instruction=ORCHESTRATOR_INSTRUCTION,
        tools=[
            ctx.analyze_query,
            ctx.apply_optimization_and_reanalyze,
            ctx.report_needs_schema,
            ctx.report_error,
        ],
    )

    prompt = f"SQL query:\n{sql}\n"
    prompt += f"\nUser-supplied schema (DDL):\n{ddl}\n" if ddl else "\n(No DDL supplied — infer one yourself if needed.)\n"

    try:
        try:
            await _run_agent_turn(orchestrator_agent, prompt)
        except Exception as exc:
            print(f"run_orchestrator: agent turn failed ({exc})")
            if not ctx.terminal and not ctx.results:
                ctx.terminal = {"status": "error", "message": "Something went wrong while analyzing your query."}
    finally:
        # Runs even on asyncio.CancelledError (e.g. the client disconnected
        # mid-analysis) — `except Exception` above doesn't catch that, since
        # CancelledError is a BaseException, so without this `finally` a
        # cancelled request would leak its sandbox database forever.
        if ctx.session_db:
            pgs.drop_session_db(ctx.session_db)

    if ctx.terminal:
        return ctx.terminal
    if not ctx.results:
        return {"status": "error", "message": "Analysis didn't produce a visualization — please try again."}

    before = ctx.results[0]
    after = ctx.results[1] if len(ctx.results) > 1 else None
    recommendations = []
    if before.get("recommendation"):
        rec = before["recommendation"]
        recommendations.append({"index_sql": rec.get("index_sql", ""), "rationale": rec.get("rationale", "")})

    return {
        "status": "success",
        "schema_note": ctx.schema_note,
        "before_diagram": before["diagram"],
        "before_steps": before["steps"],
        "before_stats": before["stats"],
        "after_diagram": after["diagram"] if after else None,
        "after_steps": after["steps"] if after else None,
        "after_stats": after["stats"] if after else None,
        "recommendations": recommendations,
    }
