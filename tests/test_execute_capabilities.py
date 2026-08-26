"""执行侧能力测试：结果集 digest（FR-6.3）、成本闸门集成（FR-5.3）、builder 接线。"""

import asyncio

from nl2sql_skill.builder import build_nl2sql_pipeline
from nl2sql_skill.config import DataSourceConfig, NL2SQLConfig
from nl2sql_skill.cost_guard import CostDecision
from nl2sql_skill.linking import SchemaLinker
from nl2sql_skill.generator import SQLGenerator
from nl2sql_skill.guardrails import GuardrailValidator
from nl2sql_skill.metadata import ColumnMeta, SchemaSnapshot, StaticSchemaProvider, TableMeta
from nl2sql_skill.pipeline import NL2SQLPipeline, _build_result_digest
from nl2sql_skill.semantic import SemanticLayer
from nl2sql_skill.types import NL2SQLRequest

from conftest import FakeCache, FakeLLM

GOOD_SQL = "SELECT id FROM orders WHERE status = 1"


class FakeCostGuard:
    """可编程成本闸门：stub evaluate 并记录调用。"""

    def __init__(self, decision: CostDecision) -> None:
        self.decision = decision
        self.evaluated: list[tuple[str, str]] = []

    async def evaluate(self, db_id: str, sql: str) -> CostDecision:
        self.evaluated.append((db_id, sql))
        return self.decision


class FakeExecutor:
    def __init__(self, rows=None) -> None:
        self.rows = rows if rows is not None else []
        self.calls = 0

    async def aquery(self, request):
        self.calls += 1

        class R:
            pass

        r = R()
        r.rows = self.rows
        r.error = None
        r.truncated = False
        return r


def _config(**overrides) -> NL2SQLConfig:
    ds = DataSourceConfig(
        db_id="erp",
        dsn="mysql://ro:pw@127.0.0.1:3306/erp",
        visible_tables=frozenset({"orders"}),
    )
    return NL2SQLConfig(datasources={"erp": ds}, **overrides)


def _pipeline(llm_responses: list[dict], *, executor=None, cost_guard=None) -> NL2SQLPipeline:
    orders = TableMeta(name="orders", comment="订单表", columns=[ColumnMeta(name="id")])
    snapshot = SchemaSnapshot.build("erp", {"orders": orders})
    return NL2SQLPipeline(
        config=_config(),
        metadata_provider=StaticSchemaProvider({"erp": snapshot}),
        linker=SchemaLinker(top_k_tables=5, schema_context_max_tokens=4000),
        generator=SQLGenerator(FakeLLM(llm_responses), model_tag="tag-1"),
        guardrails=GuardrailValidator(),
        semantic=SemanticLayer(version="sv1"),
        executor=executor,
        cost_guard=cost_guard,
    )


def _request(**kw) -> NL2SQLRequest:
    base = dict(query="有效订单 id 有哪些", tenant_id="t1", db_id="erp", request_id="r1")
    base.update(kw)
    return NL2SQLRequest(**base)


# ---------------------------------------------------------------- digest ----

def test_build_result_digest_aggregates_numeric_columns():
    rows = [
        {"id": 1, "name": "a", "active": True},
        {"id": 2, "name": "b", "active": False},
    ]
    digest = _build_result_digest(rows, ["id", "name", "active"])
    assert digest["row_count"] == 2
    assert digest["numeric"]["id"] == {"sum": 3.0, "min": 1.0, "max": 2.0, "avg": 1.5}
    assert "name" not in digest["numeric"] and "active" not in digest["numeric"]


def test_build_result_digest_empty_rows():
    assert _build_result_digest([], []) == {"row_count": 0}


def test_execute_populates_digest():
    ex = FakeExecutor(rows=[{"id": 1, "amount": 10.5}, {"id": 2, "amount": 20.5}])
    pipe = _pipeline([{"sql": GOOD_SQL, "confidence": 0.95}], executor=ex)
    result = asyncio.run(pipe.generate(_request(execute=True)))
    assert result.status == "executed"
    assert result.digest["row_count"] == 2
    assert result.digest["numeric"]["amount"] == {"sum": 31.0, "min": 10.5, "max": 20.5, "avg": 15.5}


# ---------------------------------------------------------------- cost gate ----

def test_cost_gate_blocks_over_threshold_execution():
    guard = FakeCostGuard(CostDecision(allowed=False, estimated_rows=9_000_000, reason="预估扫描 9000000 行超过成本上限"))
    ex = FakeExecutor(rows=[{"id": 1}])
    pipe = _pipeline([{"sql": GOOD_SQL, "confidence": 0.95}], executor=ex, cost_guard=guard)
    result = asyncio.run(pipe.generate(_request(execute=True)))
    assert result.status == "rejected_guardrail"
    assert "成本闸门" in result.message
    assert result.audit["cost_gate"] == "blocked"
    assert result.audit["cost_estimated_rows"] == 9_000_000
    assert ex.calls == 0  # 被拦截后绝不执行
    assert len(guard.evaluated) == 1


def test_cost_gate_allows_within_threshold():
    guard = FakeCostGuard(CostDecision(allowed=True, estimated_rows=1_000))
    ex = FakeExecutor(rows=[{"id": 1}])
    pipe = _pipeline([{"sql": GOOD_SQL, "confidence": 0.95}], executor=ex, cost_guard=guard)
    result = asyncio.run(pipe.generate(_request(execute=True)))
    assert result.status == "executed"
    assert result.audit["cost_gate"] == "allowed"
    assert result.audit["cost_estimated_rows"] == 1_000
    assert ex.calls == 1


def test_cost_gate_skipped_without_execute():
    guard = FakeCostGuard(CostDecision(allowed=False, reason="n/a"))
    pipe = _pipeline([{"sql": GOOD_SQL, "confidence": 0.95}], cost_guard=guard)
    result = asyncio.run(pipe.generate(_request(execute=False)))
    assert result.status == "generated"
    assert guard.evaluated == []


def test_cost_gate_not_configured_always_allows():
    ex = FakeExecutor(rows=[{"id": 1}])
    pipe = _pipeline([{"sql": GOOD_SQL, "confidence": 0.95}], executor=ex)
    result = asyncio.run(pipe.generate(_request(execute=True)))
    assert result.status == "executed" and ex.calls == 1


# ---------------------------------------------------------------- builder ----

def test_builder_wires_cost_guard_when_enabled():
    pipe = build_nl2sql_pipeline(config=_config(cost_guard_enabled=True), llm=FakeLLM([]), cache=None)
    assert pipe.cost_guard is not None


def test_builder_leaves_cost_guard_none_when_disabled():
    pipe = build_nl2sql_pipeline(config=_config(), llm=FakeLLM([]), cache=None)
    assert pipe.cost_guard is None


def test_builder_wires_example_store_from_config(tmp_path):
    store_path = tmp_path / "ex.jsonl"
    store_path.write_text(
        '{"question": "本月订单总额", "sql": "SELECT SUM(amount) FROM orders", "db_id": "erp"}\n',
        encoding="utf-8",
    )
    pipe = build_nl2sql_pipeline(
        config=_config(example_store_path=str(store_path)),
        llm=FakeLLM([]),
        cache=None,
    )
    assert pipe.few_shot_provider is not None
    assert pipe._provider_accepts_tenant
    hits = pipe.few_shot_provider("本月订单总额", "erp", 0.5, tenant_id="t1")
    assert hits and hits[0]["sql"] == "SELECT SUM(amount) FROM orders"