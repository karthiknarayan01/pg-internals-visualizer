"""Run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) against the sandbox and return
the real plan tree, plus the backend PID actually used to serve it."""
import psycopg

from agent.tools.postgres_sandbox import _session_conninfo

# SET doesn't support query parameters, so the value gets string-interpolated
# below — only ever allow these three exact, hardcoded literals through.
_ISOLATION_LEVELS = {
    "read committed": "READ COMMITTED",
    "repeatable read": "REPEATABLE READ",
    "serializable": "SERIALIZABLE",
}


def run_explain_analyze(session_db: str, sql: str, isolation_level: str = "read committed") -> dict:
    """Execute the query for real (ANALYZE) and return its JSON plan.

    Returns {"status": "success", "plan": <dict>, "backend_pid": <int>} or
    {"status": "error", "message": ...}. The query is actually executed
    (side effects included) since ANALYZE requires real execution, under the
    given transaction isolation level (so the MVCC narrative reflects real
    behavior, not a hypothetical).
    """
    level_sql = _ISOLATION_LEVELS.get(isolation_level.lower(), "READ COMMITTED")
    try:
        with psycopg.connect(_session_conninfo(session_db), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET default_transaction_isolation = '{level_sql}'")
                cur.execute("SELECT pg_backend_pid()")
                backend_pid = cur.fetchone()[0]
                cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}")
                (plan_json,) = cur.fetchone()
        return {
            "status": "success",
            "plan": plan_json[0]["Plan"],
            "planning_time_ms": plan_json[0].get("Planning Time"),
            "execution_time_ms": plan_json[0].get("Execution Time"),
            "backend_pid": backend_pid,
        }
    except psycopg.Error as exc:
        return {"status": "error", "message": str(exc)}
