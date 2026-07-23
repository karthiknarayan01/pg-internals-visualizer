"""Manage the disposable sandbox Postgres instance used for EXPLAIN ANALYZE runs.

Each analysis request gets its own database inside the shared sandbox
container (created fresh, dropped afterwards) so schemas from different
sessions never collide and every run starts from a clean slate.
"""
import os
import re
import uuid

import psycopg

PG_SANDBOX_HOST = os.environ.get("PG_SANDBOX_HOST", "localhost")
PG_SANDBOX_PORT = os.environ.get("PG_SANDBOX_PORT", "5433")
PG_SANDBOX_ADMIN_DB = os.environ.get("PG_SANDBOX_DB", "sandbox")
PG_SANDBOX_USER = os.environ.get("PG_SANDBOX_USER", "sandbox")
PG_SANDBOX_PASSWORD = os.environ.get("PG_SANDBOX_PASSWORD", "sandbox")

_VALID_DB_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _admin_conninfo() -> str:
    return (
        f"host={PG_SANDBOX_HOST} port={PG_SANDBOX_PORT} "
        f"dbname={PG_SANDBOX_ADMIN_DB} user={PG_SANDBOX_USER} password={PG_SANDBOX_PASSWORD}"
    )


def _session_conninfo(session_db: str) -> str:
    return (
        f"host={PG_SANDBOX_HOST} port={PG_SANDBOX_PORT} "
        f"dbname={session_db} user={PG_SANDBOX_USER} password={PG_SANDBOX_PASSWORD}"
    )


def create_session_db() -> dict:
    """Create a fresh, empty database for one analysis session.

    Returns {"status": "success", "session_db": <name>} or {"status": "error", "message": ...}.
    """
    session_db = f"session_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(_admin_conninfo(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{session_db}"')
        return {"status": "success", "session_db": session_db}
    except psycopg.Error as exc:
        return {"status": "error", "message": str(exc)}


def drop_session_db(session_db: str) -> dict:
    """Drop a session database created by create_session_db. Best-effort cleanup."""
    if not _VALID_DB_NAME.match(session_db):
        return {"status": "error", "message": "invalid session_db name"}
    try:
        with psycopg.connect(_admin_conninfo(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (session_db,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{session_db}"')
        return {"status": "success"}
    except psycopg.Error as exc:
        return {"status": "error", "message": str(exc)}


def apply_schema(session_db: str, ddl: str) -> dict:
    """Run user-supplied DDL (CREATE TABLE etc.) against the session database,
    then ANALYZE so the planner has real statistics for the loaded data
    (EXPLAIN's ANALYZE option times real execution but does NOT refresh
    pg_class/pg_statistic — a plain ANALYZE is a separate, necessary step)."""
    try:
        with psycopg.connect(_session_conninfo(session_db), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute("ANALYZE")
        return {"status": "success"}
    except psycopg.Error as exc:
        return {"status": "error", "message": str(exc)}


def check_query_resolves(session_db: str, sql: str) -> dict:
    """Try to PREPARE the query (parses + plans, no execution) to detect missing
    schema. Returns {"status": "success"} if it resolves, or
    {"status": "needs_schema", "message": <postgres error>} if tables/columns
    are unresolved, or {"status": "error", ...} for other failures.
    """
    try:
        with psycopg.connect(_session_conninfo(session_db), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f"PREPARE _check_stmt AS {sql}")
                cur.execute("DEALLOCATE _check_stmt")
        return {"status": "success"}
    except psycopg.errors.UndefinedTable as exc:
        return {"status": "needs_schema", "message": str(exc)}
    except psycopg.errors.UndefinedColumn as exc:
        return {"status": "needs_schema", "message": str(exc)}
    except psycopg.Error as exc:
        return {"status": "error", "message": str(exc)}


def run_sql(session_db: str, sql: str) -> dict:
    """Run an arbitrary statement (e.g. CREATE INDEX) against the session database."""
    try:
        with psycopg.connect(_session_conninfo(session_db), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        return {"status": "success"}
    except psycopg.Error as exc:
        return {"status": "error", "message": str(exc)}
