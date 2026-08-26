"""EXPLAIN 成本闸门单测（PRD FR-5.3）：阈值拦截、fail_policy 降级、rows 解析。"""

import asyncio

import pytest

from nl2sql_skill.config import DataSourceConfig
from nl2sql_skill.cost_guard import MySQLExplainCostGuard, _extract_estimated_rows

DS = {
    "erp": DataSourceConfig(db_id="erp", dsn="mysql://ro:pw@127.0.0.1:3306/erp"),
}


def _boom(db_id: str, sql: str):
    raise RuntimeError("connection refused")


def test_rows_below_threshold_allowed():
    guard = MySQLExplainCostGuard(DS, threshold=100, explain_runner=lambda db, sql: [{"rows": 90}])
    decision = asyncio.run(guard.evaluate("erp", "SELECT 1"))
    assert decision.allowed and decision.estimated_rows == 90


def test_rows_over_threshold_denied_with_max_of_steps():
    guard = MySQLExplainCostGuard(
        DS, threshold=100, explain_runner=lambda db, sql: [{"rows": 50}, {"rows": 200}]
    )
    decision = asyncio.run(guard.evaluate("erp", "SELECT 1"))
    assert not decision.allowed
    assert decision.estimated_rows == 200  # 多步骤取 max
    assert "超过成本上限" in decision.reason


def test_explain_failure_denies_by_default():
    guard = MySQLExplainCostGuard(DS, explain_runner=_boom)
    decision = asyncio.run(guard.evaluate("erp", "SELECT 1"))
    assert not decision.allowed and "fail_policy=deny" in decision.reason


def test_explain_failure_allow_policy_passes():
    guard = MySQLExplainCostGuard(DS, fail_policy="allow", explain_runner=_boom)
    decision = asyncio.run(guard.evaluate("erp", "SELECT 1"))
    assert decision.allowed and "fail_policy=allow" in decision.reason


def test_missing_rows_field_denied_by_default():
    guard = MySQLExplainCostGuard(DS, explain_runner=lambda db, sql: [{"id": 1}])
    decision = asyncio.run(guard.evaluate("erp", "SELECT 1"))
    assert not decision.allowed and "缺少 rows" in decision.reason


def test_unknown_datasource_denied():
    guard = MySQLExplainCostGuard(DS, explain_runner=lambda db, sql: [])
    decision = asyncio.run(guard.evaluate("nope", "SELECT 1"))
    assert not decision.allowed


def test_invalid_fail_policy_raises():
    with pytest.raises(ValueError):
        MySQLExplainCostGuard(DS, fail_policy="maybe")


def test_extract_estimated_rows_parses_and_ignores_garbage():
    rows = [{"rows": "100"}, {"rows": None}, {"rows": "abc"}, {"rows": 250}]
    assert _extract_estimated_rows(rows) == 250
    assert _extract_estimated_rows([{}, {"rows": None}]) is None