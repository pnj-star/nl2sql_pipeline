"""编排层单测：状态流转、缓存命中重校验、执行映射（PRD §5.1 / FR-6 / FR-8）。"""

import asyncio
import json

from nl2sql_skill.builder import build_nl2sql_pipeline
from nl2sql_skill.config import DataSourceConfig, NL2SQLConfig
from nl2sql_skill.linking import SchemaLinker
from nl2sql_skill.generator import SQLGenerator
from nl2sql_skill.guardrails import GuardrailValidator
from nl2sql_skill.metadata import ColumnMeta, SchemaSnapshot, StaticSchemaProvider, TableMeta
from nl2sql_skill.pipeline import NL2SQLPipeline
from nl2sql_skill.semantic import SemanticLayer, SemanticTable
from nl2sql_skill.types import NL2SQLRequest

from conftest import FakeCache, FakeLLM


class RaisingLLM:
    """模拟传输层故障的 LLM：chat_json 直接抛异常。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_json(self, messages, *, system_prompt=""):
        self.calls += 1
        raise ConnectionError("network down")


GOOD_SQL = "SELECT id FROM orders WHERE status = 1"


def _config(**overrides) -> NL2SQLConfig:
    ds = DataSourceConfig(
        db_id="erp",
        dsn="mysql://ro:pw@127.0.0.1:3306/erp",
        visible_tables=frozenset({"orders"}),
    )
    return NL2SQLConfig(datasources={"erp": ds}, **overrides)


def _pipeline(llm_responses: list[dict], *, cache=None, executor=None) -> NL2SQLPipeline:
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
        cache=cache,
    )


def _request(**kw) -> NL2SQLRequest:
    base = dict(query="有效订单 id 有哪些", tenant_id="t1", db_id="erp", request_id="r1")
    base.update(kw)
    return NL2SQLRequest(**base)


def test_generated_flow_writes_cache():
    pipe = _pipeline([{"sql": GOOD_SQL, "confidence": 0.9}], cache=FakeCache())
    result = asyncio.run(pipe.generate(_request()))
    assert result.status == "generated" and "LIMIT" in result.sql and result.used_tables == ["orders"]
    assert pipe.cache.store  # 成功结果已写缓存


def test_cache_hit_skips_llm_and_revalidates():
    llm = [{"sql": GOOD_SQL, "confidence": 0.9}]
    pipe = _pipeline(list(llm), cache=FakeCache())
    first = asyncio.run(pipe.generate(_request()))
    calls_after_first = pipe.generator.llm.calls
    second = asyncio.run(pipe.generate(_request()))
    assert first.status == "generated"
    assert second.status == "generated_cache" and second.cache_hit
    assert pipe.generator.llm.calls == calls_after_first  # 命中后不再调 LLM


def test_tampered_cache_falls_through_to_regenerate():
    pipe = _pipeline([{"sql": GOOD_SQL, "confidence": 0.9}], cache=FakeCache())
    asyncio.run(pipe.generate(_request()))
    # 向缓存注入一条越权 SQL，模拟"缓存写入后被篡改/规则升级前写入"。
    for key in list(pipe.cache.store):
        pipe.cache.store[key] = json.dumps({"sql": "SELECT * FROM secret", "confidence": 0.99})
    llm_backup = list(pipe.generator.llm.responses)
    pipe.generator.llm.responses = llm_backup or [{"sql": GOOD_SQL, "confidence": 0.9}]
    result = asyncio.run(pipe.generate(_request()))
    assert result.status in ("generated", "generated_cache") and "secret" not in result.sql.lower()


class FakeExecutor:
    """假执行器：返回 rows 或 error，接口与 structured_query 兼容。"""

    def __init__(self, rows=None, error=None) -> None:
        self.rows = rows if rows is not None else []
        self.error = error
        self.last_request = None

    async def aquery(self, request):
        self.last_request = request

        class R:
            pass

        r = R()
        r.rows = self.rows
        r.error = self.error
        r.truncated = False
        return r


def test_execute_success_maps_to_executed():
    ex = FakeExecutor(rows=[{"id": 1}, {"id": 2}])
    pipe = _pipeline([{"sql": GOOD_SQL, "confidence": 0.95}], executor=ex)
    result = asyncio.run(pipe.generate(_request(execute=True)))
    assert result.status == "executed" and result.rows == [{"id": 1}, {"id": 2}]
    assert result.columns == ["id"] and ex.last_request.max_rows == 200


def test_executor_error_maps_to_error_status():
    pipe = _pipeline(
        [{"sql": GOOD_SQL, "confidence": 0.95}],
        executor=FakeExecutor(error="timeout"),
    )
    result = asyncio.run(pipe.generate(_request(execute=True)))
    assert result.status == "error" and "执行失败" in result.message


def test_guardrail_rejection_not_cached_and_no_retry():
    pipe = _pipeline([{"sql": "DELETE FROM orders", "confidence": 0.99}], cache=FakeCache())
    result = asyncio.run(pipe.generate(_request()))
    assert result.status == "rejected_guardrail"
    assert not pipe.cache.store  # 拦截结果绝不写缓存
    assert pipe.generator.llm.calls == 1  # 校验失败不触发内部重试


def test_low_confidence_triggers_clarification():
    pipe = _pipeline([{"sql": "", "confidence": 0.2, "clarify_questions": ["统计哪个门店？"]}])
    result = asyncio.run(pipe.generate(_request()))
    assert result.status == "need_clarification" and result.clarify_questions


def test_unknown_db_returns_no_schema():
    pipe = _pipeline([])
    result = asyncio.run(pipe.generate(_request(db_id="nope")))
    assert result.status == "no_schema" and "未注册" in result.message


def test_llm_transport_error_reported_without_repair_retry():
    llm = RaisingLLM()
    pipe = NL2SQLPipeline(
        config=_config(),
        metadata_provider=StaticSchemaProvider(
            {
                "erp": SchemaSnapshot.build(
                    "erp",
                    {"orders": TableMeta(name="orders", columns=[ColumnMeta(name="id")])},
                )
            }
        ),
        linker=SchemaLinker(top_k_tables=5),
        generator=SQLGenerator(llm, model_tag="tag-x"),
        guardrails=GuardrailValidator(),
        semantic=SemanticLayer(version="sv"),
    )
    result = asyncio.run(pipe.generate(_request()))
    # A3：网络异常必须明确报"调用失败"，且不做 JSON 修复重试。
    assert result.status == "error" and "调用失败" in result.message and llm.calls == 1


def test_trace_id_always_populated():
    pipe = _pipeline([])
    ok = asyncio.run(pipe.generate(_request()))
    bad = asyncio.run(pipe.generate(_request(db_id="nope")))
    assert ok.trace_id and bad.trace_id and ok.trace_id != bad.trace_id


def test_history_not_in_cache_key_by_default():
    pipe = _pipeline([])
    m1 = pipe._cache_material(query="q", db_id="erp", history_hash="")
    m2 = pipe._cache_material(query="q", db_id="erp", history_hash="abc123")
    # C4：默认关闭 history 参与 key，两者必须相等。
    assert m1 == m2


def test_builder_assembles_with_injected_fakes():
    pipe = build_nl2sql_pipeline(config=_config(), llm=FakeLLM([]), cache=None)
    assert isinstance(pipe, NL2SQLPipeline)
