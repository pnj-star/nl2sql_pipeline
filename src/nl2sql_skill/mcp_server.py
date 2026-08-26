"""构建只暴露 ``nl2sql_generate`` 的 FastMCP 服务器。

把 MCP 服务器（工具 + 鉴权 + trace 接线 + 指标）与命令行入口拆开：
本模块只负责 ``create_mcp_server()``；入口 ``mcp.py`` 负责解析参数并启动。
业务主链路不依赖 MCP 存在：core pipeline 仍走 ``builder.build_nl2sql_pipeline()``，
MCP 只是对外暴露的可选薄外壳（PRD §5.1 架构约束）。

鉴权沿用仓库统一约定（与 ``retrieve_skill`` / ``structured_query_skill`` 同规则）：
- 每次工具调用必传 ``tenant_id`` / ``db_id`` / ``request_id``；
- ``AUTH_MODE=jwt`` 时还需携带 JWT（``auth_token`` 参数或 HTTP Bearer 头），
  其中 ``tenant_id`` / ``kb_id`` claims 必须与请求作用域一致，``kb_id`` 位置承载
  ``db_id`` 数据源授权；
- ``AUTH_MODE=disabled`` 时守卫仍强制要求作用域参数齐全，保证下游缓存、
  指标与审计始终拥有完整上下文。
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from common_core import telemetry
from common_core.auth import AuthConfig
from common_core.instrumentation import current_trace_id, trace_node
from common_core.mcp_auth import ToolContextGuard, build_mcp_auth
from common_core.observability import Observability

from .builder import build_nl2sql_pipeline
from .config import NL2SQLConfig
from .pipeline import NL2SQLPipeline
from .types import ERROR, NL2SQLRequest, NL2SQLResult

logger = logging.getLogger(__name__)


def _masked_config_view(config: NL2SQLConfig) -> dict[str, Any]:
    """构造健康检查用的脱敏配置视图（不暴露任何连接串/密钥）。"""
    return {
        "datasources": sorted(config.datasources.keys()),
        "semantic_dir": config.semantic_dir or "",
        "clarify_threshold": config.clarify_threshold,
        "max_rows": config.max_rows,
        "cache_enabled": config.sql_cache_enabled,
        "llm_base_url": "<redacted>",
        "llm_model": "<redacted>",
    }


def _health_payload(
    name: str,
    config: NL2SQLConfig | None,
    config_source: str | None,
) -> dict[str, Any]:
    """构造 ``/health`` 响应：报告配置完整性、指纹与脱敏视图。

    配置不完整时仍返回 ``status=ok`` 便于运维继续排查，进程级别的
    fail-fast 由 ``mcp.main()`` 在启动阶段完成（health 只是第二道确认）。
    """
    if config is None:
        return {
            "status": "ok",
            "service": name,
            "tools": ["nl2sql_generate"],
            "config": {
                "complete": False,
                "fingerprint": None,
                "source": config_source or "unknown",
                "masked": {},
            },
        }
    complete = bool(config.datasources)
    raw = "\x1f".join(
        [
            ",".join(sorted(config.datasources.keys())),
            str(config.clarify_threshold),
            str(config.max_rows),
        ]
    )
    fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return {
        "status": "ok",
        "service": name,
        "tools": ["nl2sql_generate"],
        "config": {
            "complete": complete,
            "fingerprint": fingerprint,
            "source": config_source or "process-env",
            "masked": _masked_config_view(config),
        },
    }


def _load_pipeline(pipeline: NL2SQLPipeline | None, metrics: Any = None) -> NL2SQLPipeline:
    """返回显式传入的管线，否则从环境构建一个默认管线。"""
    if pipeline is not None:
        return pipeline
    return build_nl2sql_pipeline(metrics=metrics)


def _transport_auth_token() -> str | None:
    """HTTP 传输层校验通过后，从 FastMCP 上下文取回原始 JWT。"""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        access_token = get_access_token()
    except Exception:  # noqa: BLE001 - 非 HTTP 传输/未启用鉴权时优雅降级
        return None
    return access_token.token if access_token is not None else None


def _attach_trace(traceparent: str | None):
    """解析并挂载上游 W3C traceparent 为当前 OTel 上下文。

    返回恢复上下文所需的 token；解析失败或未启用追踪时返回 None（no-op）。
    """
    return telemetry.set_current_context(telemetry.parse_traceparent(traceparent))


def create_mcp_server(
    pipeline: NL2SQLPipeline | None = None,
    *,
    auth: AuthConfig | None = None,
    metrics: Observability | None = None,
    config: NL2SQLConfig | None = None,
    config_source: str | None = None,
    name: str = "nl2sql-skill",
    host: str | None = None,
    port: int | None = None,
    streamable_path: str = "/streamable",
    sse_path: str = "/sse",
    log_level: str = "INFO",
    debug: bool = False,
    instructions: str = (
        "面向 agent 的 MySQL 自然语言转 SQL 工具。只做 NL\u2192SQL 生成与静态校验、"
        "不做回答生成：传入 query + tenant_id + db_id + request_id，返回已通过 "
        "sqlglot AST 只读护栏校验的 SQL（execute=true 时可选附带执行结果）。"
        "若 AUTH_MODE=jwt 开启，HTTP 调用通过 Authorization Bearer 头传递 JWT，"
        "也可在工具参数里传 auth_token；其中 tenant_id / kb_id claims 需与请求 "
        "参数一致（kb_id 位置承载 db_id 数据源授权）。"
        "可选参数支持多轮上下文（history）、执行（execute）、行数上限（max_rows）、"
        "置信阈值（clarify_threshold）与候选表数（top_k_tables）。"
        "可选传入上游 W3C traceparent 以串联分布式调用链路，"
        "响应会携带 trace_id 供日志与链路追踪关联排查。"
    ),
) -> Any:
    """构建只暴露 ``nl2sql_generate`` 的 FastMCP 服务器。"""
    from mcp.server.fastmcp import FastMCP

    pipeline = _load_pipeline(pipeline, metrics)
    auth_config = auth or AuthConfig.from_env()
    # nl2sql 的作用域是 tenant_id + db_id（无 kb 概念）；kb_id 位置承载 db_id，
    # 因此关闭 require_kb_id，改在工具参数层强制校验 db_id 非空。
    guard = ToolContextGuard(
        config=auth_config,
        require_tenant_id=True,
        require_kb_id=False,
        require_request_id=True,
    )
    token_verifier, auth_settings = build_mcp_auth(auth_config)
    mcp_kwargs: dict[str, Any] = {
        "streamable_http_path": streamable_path,
        "sse_path": sse_path,
        "log_level": log_level,
        "debug": debug,
    }
    if auth_settings is not None:
        mcp_kwargs["token_verifier"] = token_verifier
        mcp_kwargs["auth"] = auth_settings
    if host is not None:
        mcp_kwargs["host"] = host
    if port is not None:
        mcp_kwargs["port"] = port
    server = FastMCP(name=name, instructions=instructions, **mcp_kwargs)

    @server.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        """K8s readiness / 运维探活：暴露脱敏配置状态与指纹。"""
        return JSONResponse(_health_payload(name, config, config_source))

    @server.tool()
    async def nl2sql_generate(
        query: str,
        tenant_id: str,
        db_id: str,
        request_id: str,
        auth_token: str | None = None,
        traceparent: str | None = None,
        session_id: str = "",
        user_id: str = "",
        history: list[dict[str, Any]] | None = None,
        execute: bool = False,
        max_rows: int | None = None,
        clarify_threshold: float | None = None,
        top_k_tables: int | None = None,
    ) -> dict[str, Any]:
        """把自然语言问题转换为通过安全护栏的只读 SQL（可选执行）。

        Args:
            query: 用户自然语言问题。
            tenant_id: 租户 ID，用于租户隔离与鉴权校验。
            db_id: 目标数据源 ID（对应 NL2SQL_DB_<ID>_DSN 注册表）。
            request_id: 本次请求唯一标识，供日志追踪。
            auth_token: JWT 令牌；HTTP 调用也可通过 Authorization Bearer 头传递。
            traceparent: 上游 W3C traceparent 头，串联分布式调用链路。
            session_id: 会话 ID，多轮上下文归组。
            user_id: 用户 ID，审计归属。
            history: 多轮历史列表；每项支持 {"role","content"} 或
                {"question","sql"}。
            execute: 是否委托执行器运行生成的 SQL 并返回结果集。
            max_rows: 本次行数上限覆盖；不传用服务端默认值。
            clarify_threshold: 本次置信阈值覆盖；不传用服务端默认值。
            top_k_tables: schema 召回候选表数量覆盖；不传用服务端默认值。

        Returns:
            结构化字典（契约见 SKILL.md）：status 是权威信号
            （generated / generated_cache / executed / need_clarification /
            no_schema / rejected_guardrail / error），ok 表示无内部异常，
            sql 为通过全部校验的只读 SQL，audit 提供召回与耗时诊断。
        """
        token = _transport_auth_token() or auth_token
        # kb_id 参数位承载 db_id 作用域，token 中 kb_id claim 即数据源授权。
        context = guard.resolve(
            tenant_id=tenant_id,
            kb_id=db_id,
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            auth_token=token,
        )
        trace_token = _attach_trace(traceparent)
        trace_id_value: str | None = None
        t_start = time.perf_counter()
        try:
            with trace_node(
                "nl2sql_generate",
                tenant_id=context.tenant_id,
                kb_id=context.kb_id,
                request_id=context.request_id,
            ):
                trace_id_value = current_trace_id()
                result: NL2SQLResult = await pipeline.generate(
                    NL2SQLRequest(
                        query=query,
                        tenant_id=context.tenant_id,
                        db_id=context.kb_id or db_id,
                        request_id=context.request_id,
                        session_id=context.session_id,
                        user_id=context.user_id,
                        history=history or [],
                        execute=execute,
                        max_rows=max_rows,
                        clarify_threshold=clarify_threshold,
                        top_k_tables=top_k_tables,
                    )
                )
        except Exception as exc:  # 管线/鉴权异常转成稳定结构化契约
            logger.exception(
                "nl2sql_generate failed tenant=%s db=%s request=%s",
                context.tenant_id,
                db_id,
                context.request_id,
            )
            record_error = getattr(metrics, "record_node_error", None)
            if record_error is not None:
                record_error(
                    "nl2sql_generate",
                    tenant_id=context.tenant_id,
                    kb_id=db_id,
                )
            return {
                "ok": False,
                "status": ERROR,
                "query": query,
                "sql": "",
                "confidence": 0.0,
                "used_tables": [],
                "assumptions": [],
                "clarify_questions": [],
                "rows": [],
                "columns": [],
                "truncated": False,
                "cache_hit": False,
                "tenant_id": context.tenant_id,
                "db_id": db_id,
                "request_id": context.request_id,
                "user_id": context.user_id,
                "trace_id": trace_id_value,
                "message": f"{type(exc).__name__}: {exc}",
                "audit": {},
            }
        finally:
            telemetry.reset_context(trace_token)

        metrics_record = getattr(metrics, "record_node_duration", None)
        if metrics_record is not None:
            metrics_record(
                "nl2sql_generate",
                time.perf_counter() - t_start,
                tenant_id=context.tenant_id,
                kb_id=db_id,
            )
        metrics_run = getattr(metrics, "record_run", None)
        if metrics_run is not None:
            metrics_run(
                result.status,
                tenant_id=context.tenant_id,
                kb_id=db_id,
            )
        metrics_cache = getattr(metrics, "record_cache", None)
        if metrics_cache is not None:
            metrics_cache(
                "hit" if result.cache_hit else "miss",
                tenant_id=context.tenant_id,
                kb_id=db_id,
            )

        payload = result.to_dict()
        # 以鉴权确认后的作用域为准回显，避免调用方参数与 token 不一致时误导审计。
        payload["tenant_id"] = context.tenant_id
        payload["db_id"] = context.kb_id or db_id
        payload["request_id"] = context.request_id
        payload["user_id"] = context.user_id
        if trace_id_value:
            payload["trace_id"] = trace_id_value
        return payload

    return server


__all__ = ["create_mcp_server"]