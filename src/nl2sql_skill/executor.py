"""Read-only MySQL executor backed by the registered NL2SQL datasources.

The pipeline already applies the sqlglot AST guardrails and LIMIT normalization
before this executor runs. It only needs to connect to the target datasource and
return JSON-safe rows, matching the ``QueryExecutor`` protocol used by
``pipeline._execute``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .config import DataSourceConfig
from .types import ExecutorQuery


@dataclass(slots=True)
class ExecutorResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    truncated: bool = False


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


class DataSourceMySQLExecutor:
    """Execute already-guarded read-only SQL against a registered datasource."""

    def __init__(self, datasources: dict[str, DataSourceConfig]):
        self._datasources = datasources

    async def aquery(self, request: ExecutorQuery) -> ExecutorResult:
        return await asyncio.to_thread(self._query_sync, request)

    def _query_sync(self, request: ExecutorQuery) -> ExecutorResult:
        ds = self._datasources.get((request.db_id or "").lower())
        if ds is None:
            return ExecutorResult(error=f"数据源未注册: {request.db_id}")
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - environment dependency
            return ExecutorResult(
                error="需要安装 pymysql: pip install 'nl2sql-skill[sql]'"
            )

        conn_info = ds.parsed
        try:
            conn = pymysql.connect(
                host=str(conn_info["host"]),
                port=int(conn_info["port"]),  # type: ignore[arg-type]
                user=str(conn_info["user"]),
                password=str(conn_info["password"]),
                database=str(conn_info["database"]),
                charset="utf8mb4",
                connect_timeout=5,
                read_timeout=30,
                cursorclass=pymysql.cursors.DictCursor,
            )
        except Exception as exc:
            return ExecutorResult(error=f"{request.db_id} 连接失败: {exc}")

        try:
            with conn.cursor() as cur:
                cur.execute(request.sql)
                rows = list(cur.fetchall())
        except Exception as exc:
            return ExecutorResult(error=f"{request.db_id} 查询失败: {exc}")
        finally:
            conn.close()

        safe_rows = _json_safe(rows)
        if not isinstance(safe_rows, list):
            safe_rows = []
        max_rows = request.max_rows
        if max_rows is not None:
            limited = safe_rows[:max_rows]
            truncated = len(safe_rows) > max_rows
        else:
            limited, truncated = safe_rows, False
        return ExecutorResult(rows=limited, truncated=truncated)