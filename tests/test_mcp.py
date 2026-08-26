"""MCP 入口契约测试：验证 nl2sql_generate 工具的入参透传、返回契约、
鉴权接线、health 路由与 metrics 记录（对标 retrieve_skill/test_rag_mcp.py）。"""

import asyncio

import httpx
import pytest

pytest.importorskip("mcp")

from common_core.auth import AuthConfig
from common_core.mcp_auth import ToolAuthError, ToolContextGuard

from nl2sql_skill.mcp import create_mcp_server
from nl2sql_skill.types import GENERATED, NL2SQLRequest, NL2SQLResult


class FakePipeline:
    """返回稳定 generated 结果的假管线；记录入参供断言。"""

    def __init__(self) -> None:
        self.calls: list[NL2SQLRequest] = []

    async def generate(self, request: NL2SQLRequest) -> NL2SQLResult:
        self.calls.append(request)
        return NL2SQLResult(
            status=GENERATED,
            ok=True,
            query=request.query,
            sql="SELECT id FROM orders LIMIT 200",
            confidence=0.9,
            used_tables=["orders"],
            assumptions=["时间范围取订单创建时间"],
            tenant_id=request.tenant_id,
            db_id=request.db_id,
            request_id=request.request_id,
            user_id=request.user_id,
            trace_id="trc-test",
            message="ok",
            audit={"candidate_tables_all": ["orders"]},
        )


class FailingPipeline(FakePipeline):
    async def generate(self, request: NL2SQLRequest) -> NL2SQLResult:
        raise RuntimeError("metadata down")


class RecordingMetrics:
    def __init__(self) -> None:
        self.errors: list[dict] = []

    def record_node_error(self, node: str, tenant_id: str = "", kb_id: str = "") -> None:
        self.errors.append({"node": node, "tenant_id": tenant_id, "kb_id": kb_id})


def _payload(result) -> dict:
    return result[1] if isinstance(result, tuple) else result


def _run(coro):
    return asyncio.run(coro)


def _disabled_auth() -> AuthConfig:
    return AuthConfig(mode="disabled")


def test_mcp_server_exposes_nl2sql_tools() -> None:
    server = create_mcp_server(pipeline=FakePipeline(), auth=_disabled_auth())
    names = {tool.name for tool in _run(server.list_tools())}
    assert "nl2sql_generate" in names


def test_health_route_reports_config() -> None:
    server = create_mcp_server(pipeline=FakePipeline(), auth=_disabled_auth())

    async def request_health() -> dict:
        app = server.streamable_http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
        assert response.status_code == 200
        return response.json()

    payload = _run(request_health())
    assert payload["status"] == "ok"
    assert payload["service"] == "nl2sql-skill"
    assert payload["tools"] == ["nl2sql_generate"]


def test_health_route_stays_anonymous_with_jwt_auth() -> None:
    server = create_mcp_server(
        pipeline=FakePipeline(),
        auth=AuthConfig(mode="jwt", jwt_secret="test-secret-key-0123456789abcdef"),
    )

    async def request_health() -> dict:
        app = server.streamable_http_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
        assert response.status_code == 200
        return response.json()

    payload = _run(request_health())
    assert payload["status"] == "ok"


def test_nl2sql_generate_returns_contract() -> None:
    pipeline = FakePipeline()
    server = create_mcp_server(pipeline=pipeline, auth=_disabled_auth())
    result = _run(
        server.call_tool(
            "nl2sql_generate",
            {
                "query": "上月华东区销售额前十的门店",
                "tenant_id": "t1",
                "db_id": "erp",
                "request_id": "r1",
                "session_id": "s1",
                "user_id": "u1",
            },
        )
    )
    payload = _payload(result)
    assert payload["status"] == GENERATED
    assert payload["ok"] is True
    assert payload["sql"] == "SELECT id FROM orders LIMIT 200"
    assert payload["tenant_id"] == "t1"
    assert payload["db_id"] == "erp"
    assert payload["request_id"] == "r1"
    assert payload["user_id"] == "u1"
    assert payload["query"] == "上月华东区销售额前十的门店"
    assert payload["cache_hit"] is False
    assert payload["trace_id"] == "trc-test"
    assert payload["audit"]["candidate_tables_all"] == ["orders"]
    # 入参必须完整透传到 pipeline（含 session_id）。
    request = pipeline.calls[0]
    assert request.session_id == "s1" and request.history == []


def test_nl2sql_generate_passes_history_and_overrides() -> None:
    pipeline = FakePipeline()
    server = create_mcp_server(pipeline=pipeline, auth=_disabled_auth())
    _run(
        server.call_tool(
            "nl2sql_generate",
            {
                "query": "只看杭州的",
                "tenant_id": "t1",
                "db_id": "erp",
                "request_id": "r2",
                "history": [{"question": "各区域销量", "sql": "SELECT region, SUM(qty) FROM orders"}],
                "max_rows": 50,
                "clarify_threshold": 0.8,
                "top_k_tables": 5,
            },
        )
    )
    request = pipeline.calls[0]
    assert request.history and request.max_rows == 50
    assert request.clarify_threshold == 0.8 and request.top_k_tables == 5


def test_structured_error_on_pipeline_exception() -> None:
    metrics = RecordingMetrics()
    server = create_mcp_server(
        pipeline=FailingPipeline(),
        auth=_disabled_auth(),
        metrics=metrics,
    )
    result = _run(
        server.call_tool(
            "nl2sql_generate",
            {
                "query": "q",
                "tenant_id": "t9",
                "db_id": "erp9",
                "request_id": "r9",
            },
        )
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["sql"] == ""
    assert "metadata down" in payload["message"]
    assert payload["tenant_id"] == "t9"
    assert payload["db_id"] == "erp9"
    assert metrics.errors == [
        {"node": "nl2sql_generate", "tenant_id": "t9", "kb_id": "erp9"}
    ]


def test_auth_guard_requires_scope_parameters() -> None:
    # MCP 参数层：缺少必填 tenant_id 直接拒绝（pydantic 校验错误包装为 ToolError）。
    from mcp.server.fastmcp.exceptions import ToolError

    server = create_mcp_server(pipeline=FakePipeline(), auth=_disabled_auth())
    with pytest.raises(ToolError, match="tenant_id"):
        _run(
            server.call_tool(
                "nl2sql_generate",
                {"query": "q", "db_id": "erp", "request_id": "r1"},
            )
        )
    # guard 层：disabled 模式仍强制要求作用域参数齐全。
    guard = ToolContextGuard(
        config=_disabled_auth(),
        require_tenant_id=True,
        require_kb_id=False,
        require_request_id=True,
    )
    with pytest.raises(ToolAuthError):
        guard.resolve(tenant_id="", kb_id="erp", request_id="r1")


def test_jwt_scope_mismatch_rejected_and_match_accepted() -> None:
    import time

    import jwt

    secret = "test-secret-key-0123456789abcdef"
    token = jwt.encode(
        {
            "exp": int(time.time()) + 3600,
            "sub": "user-x",
            "tenant_id": "t1",
            "kb_id": "erp",
        },
        secret,
        algorithm="HS256",
    )
    server = create_mcp_server(
        pipeline=FakePipeline(),
        auth=AuthConfig(mode="jwt", jwt_secret=secret),
    )
    # token 的 db_id(kb_id) 与请求参数不一致 → 拒绝（FastMCP 包装为 ToolError）。
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="kb_id does not match"):
        _run(
            server.call_tool(
                "nl2sql_generate",
                {
                    "query": "q",
                    "tenant_id": "t1",
                    "db_id": "other_db",
                    "request_id": "r1",
                    "auth_token": token,
                },
            )
        )
    # token 的 tenant/kb 与请求一致 → 放行，user_id 以 token 为准。
    pipeline = FakePipeline()
    server2 = create_mcp_server(
        pipeline=pipeline,
        auth=AuthConfig(mode="jwt", jwt_secret=secret),
    )
    result = _run(
        server2.call_tool(
            "nl2sql_generate",
            {
                "query": "q",
                "tenant_id": "t1",
                "db_id": "erp",
                "request_id": "r2",
                "auth_token": token,
            },
        )
    )
    payload = _payload(result)
    assert payload["status"] == GENERATED
    assert payload["user_id"] == "user-x"
    assert pipeline.calls[0].tenant_id == "t1"
    assert pipeline.calls[0].db_id == "erp"