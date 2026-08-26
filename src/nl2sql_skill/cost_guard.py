"""EXPLAIN 成本闸门（PRD FR-5.3）：execute=true 路径上评估 SQL 预估扫描行数。

- estimated_rows 超过 cost_threshold 时拒绝执行，映射为 rejected_guardrail；
- EXPLAIN 自身失败（连接/权限/语法）时按 fail_policy 降级：
  ``deny``（默认）拒绝执行并告警，``allow`` 放行并告警；
- 闸门只对 execute=true 生效，且 SQL 必须已通过 AST 护栏；
- EXPLAIN 使用与元数据采集相同的只读连接配置，不依赖外部执行器，
  保持 skill 内部可独立复用。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .config import DataSourceConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CostDecision:
    """一次成本评估的结果。

    属性:
        allowed: 是否允许执行（未超阈值 / fail_policy=allow 降级放行）。
        estimated_rows: EXPLAIN 解析出的预估扫描行数；无法解析时为 None。
        reason: 决策说明（拒绝原因或降级说明），可直接透出给调用方。
    """

    allowed: bool
    estimated_rows: int | None = None
    reason: str = ""


class CostGuard(Protocol):
    """成本闸门协议：调用方可以注入任何实现了该协议的评估器。"""

    async def evaluate(self, db_id: str, sql: str) -> CostDecision: ...


def _extract_estimated_rows(rows: list[dict[str, Any]]) -> int | None:
    """从 EXPLAIN 结果里取预估扫描行数；多步骤时取各步骤最大 rows。"""
    values: list[int] = []
    for row in rows:
        raw = row.get("rows")
        if raw is None:
            continue
        try:
            values.append(int(raw))
        except (TypeError, ValueError):
            continue
    return max(values) if values else None


class MySQLExplainCostGuard:
    """基于 MySQL EXPLAIN 的成本闸门实现。

    参数:
        datasources: db_id → DataSourceConfig 注册表（复用只读 DSN 连接）。
        threshold: 预估行数上限；超过即拒绝执行。
        fail_policy: EXPLAIN 失败时的策略，"deny"（默认，拒绝执行）或 "allow"（放行）。
        connect_timeout: EXPLAIN 连接超时（秒）。
        explain_runner: 可选注入的同步执行函数 (db_id, sql) -> 行字典列表；
            None 时使用内置 pymysql 实现；测试注入假 runner。
    """

    def __init__(
        self,
        datasources: dict[str, DataSourceConfig],
        *,
        threshold: int = 5_000_000,
        fail_policy: str = "deny",
        connect_timeout: int = 5,
        explain_runner: Callable[[str, str], list[dict[str, Any]]] | None = None,
    ) -> None:
        if fail_policy not in ("deny", "allow"):
            raise ValueError(f"fail_policy 必须是 deny/allow，收到 {fail_policy!r}")
        self._datasources = datasources
        self.threshold = threshold
        self.fail_policy = fail_policy
        self._connect_timeout = connect_timeout
        self._explain_runner = explain_runner

    async def evaluate(self, db_id: str, sql: str) -> CostDecision:
        """执行 EXPLAIN 并给出成本决策；失败按 fail_policy 降级。"""
        runner = self._explain_runner or self._run_explain
        try:
            rows = await asyncio.to_thread(runner, db_id, sql)
        except Exception as exc:  # noqa: BLE001 - 连接/权限/语法错误统一按策略降级
            logger.warning("EXPLAIN failed db=%s: %s", db_id, exc)
            if self.fail_policy == "deny":
                return CostDecision(allowed=False, reason=f"EXPLAIN 失败且 fail_policy=deny: {exc}")
            return CostDecision(allowed=True, reason=f"EXPLAIN 失败已按 fail_policy=allow 放行: {exc}")

        estimated = _extract_estimated_rows(rows)
        if estimated is None:
            logger.warning("EXPLAIN 结果缺少 rows 预估 db=%s", db_id)
            if self.fail_policy == "deny":
                return CostDecision(allowed=False, reason="EXPLAIN 结果缺少 rows 预估且 fail_policy=deny")
            return CostDecision(allowed=True, reason="EXPLAIN 结果缺少 rows 预估，已按 fail_policy=allow 放行")
        if estimated > self.threshold:
            return CostDecision(
                allowed=False,
                estimated_rows=estimated,
                reason=f"预估扫描 {estimated} 行超过成本上限 {self.threshold}",
            )
        return CostDecision(allowed=True, estimated_rows=estimated)

    def _run_explain(self, db_id: str, sql: str) -> list[dict[str, Any]]:
        """用只读连接执行 EXPLAIN 并返回行字典列表（同步，在线程池中运行）。"""
        ds = self._datasources.get(db_id)
        if ds is None:
            raise ValueError(f"数据源未注册: {db_id}")
        import pymysql

        conn_info = ds.parsed
        conn = pymysql.connect(
            host=str(conn_info["host"]),
            port=int(conn_info["port"]),
            user=str(conn_info["user"]),
            password=str(conn_info["password"]),
            database=str(conn_info["database"]),
            charset="utf8mb4",
            connect_timeout=self._connect_timeout,
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("EXPLAIN " + sql)
                return list(cur.fetchall())
        finally:
            conn.close()


__all__ = ["CostDecision", "CostGuard", "MySQLExplainCostGuard", "_extract_estimated_rows"]