from pydantic import BaseModel


class AnalyzeRequest(BaseModel):
    sql: str
    ddl: str | None = None
    isolation_level: str = "read committed"  # "read committed" | "repeatable read" | "serializable"
