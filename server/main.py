"""FastAPI backend for pg-explain-visualizer.

POST /api/analyze streams progress over SSE while the PG Internals Expert
(agent/agent.py::run_direct_expert) does its work — no orchestrator, no
docker/sandbox, no real EXPLAIN execution; the Expert reasons out its own
plan and draws the diagram in one call (see PART 0 of
agent/prompts.py::PG_EXPERT_INSTRUCTION). A final "result" event carries
the full response payload.
"""
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request

from agent.agent import run_direct_expert
from server.schemas import AnalyzeRequest

app = FastAPI(title="pg-explain-visualizer")

WEB_DIR = Path(__file__).parent.parent / "web"


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    # This is a local, actively-iterated-on tool — a stale cached copy of
    # app.js silently running against a newer server has already caused a
    # confusing "old code, new backend" mismatch once. Never cache anything
    # so a plain reload always picks up whatever's on disk right now.
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


async def _stream_analysis(req: AnalyzeRequest):
    queue: asyncio.Queue = asyncio.Queue()

    async def on_status(message: str) -> None:
        await queue.put({"event": "status", "data": json.dumps({"message": message})})

    async def run() -> None:
        try:
            result = await run_direct_expert(req.sql, req.ddl, req.isolation_level, on_status)
        except Exception as exc:  # never let an unexpected error hang the stream
            result = {"status": "error", "message": f"Unexpected error: {exc}"}
        await queue.put({"event": "result", "data": json.dumps(result)})
        await queue.put(None)

    task = asyncio.create_task(run())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    return EventSourceResponse(_stream_analysis(req))


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
