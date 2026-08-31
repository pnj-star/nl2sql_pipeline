"""公共类型定义：状态码、请求/结果 dataclass 与执行器协议。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

# ------------------------------状态码--------------------------------------
# 与 PRD 5.1 的 status 枚举一一对应；调用方（上层 LangGraph 节点）据此路由。
GENERATED = "generated"                    # 生成并通过全部校验（未执行）
GENERATED_CACHE = "generated_cache"        # 未执行且命中 SQL 缓存
EXECUTED = "executed"                      # execute=true 且执行成功（含缓存命中后执行）
NEED_CLARIFICATION = "need_clarification"  # 置信不足/缺槽位/缺字段，需向用户澄清
NO_SCHEMA = "no_schema"                    # 数据源未注册或 schema 召回为空
REJECTED_GUARDRAIL = "rejected_guardrail"  # 护栏拦截（写操作/越权/INTO OUTFILE 等）
ERROR = "error"                            # 内部异常（LLM 失败/执行失败/元数据失败）


@dataclass(slots=True)
class NL2SQLRequest:
    """一次 NL2SQL 调用的入参。

    属性:
        query: 用户自然语言问题，必填。
        tenant_id: 租户 ID，缓存与审计隔离边界，必填。
        db_id: 目标数据源 ID（对应配置里的 NL2SQL_DB_<ID>_DSN），必填。
        request_id: 幂等/追踪 ID，必填。
        session_id: 会话 ID，用于多轮上下文归组，可选。
        user_id: 用户 ID，审计归属，可选。
        history: 多轮历史列表；每项支持 {"role","content"} 或 {"question","sql"}。
        execute: True 时生成后委托 executor 执行并返回 rows。
        max_rows: 本次行数上限覆盖；None 时用配置默认值。
        clarify_threshold: 本次置信阈值覆盖；None 时用配置默认值。
        top_k_tables: schema 召回候选表数量覆盖；None 时用配置默认值。
    """

    query: str
    tenant_id: str
    db_id: str
    request_id: str
    session_id: str = ""
    user_id: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    execute: bool = False
    max_rows: int | None = None
    clarify_threshold: float | None = None
    top_k_tables: int | None = None


@dataclass(slots=True)
class NL2SQLResult:
    """NL2SQL 调用的统一返回结构（字段与 PRD §5.1 响应 schema 对齐）。

    属性:
        status: 机器可读状态码（本模块顶部常量之一），调用方据此分支。
        ok: 是否无内部异常，等价于 status != "error"。
        query: 回显的原始问题。
        sql: 通过校验的最终 SQL；澄清/拦截/异常场景为空串。
        confidence: LLM 自评置信度 [0,1]；注意存在虚高风险，仅作参考信号。
        used_tables: SQL 实际引用的物理表（已排除 CTE 别名）。
        assumptions: LLM 声明采用的口径假设（如时间字段选择），供上层展示核对。
        clarify_questions: 需要向用户追问的问题列表；need_clarification 时非空。
        rows / columns / truncated: execute=true 时的结果集、列名、截断标记。
        digest: execute=true 时的结果集摘要（行数 + 数值列 sum/min/max/avg）。
        cache_hit: 是否命中 SQL 缓存（executed + cache_hit=true 表示缓存 SQL 被执行）。
        tenant_id / db_id / request_id / user_id: 本次生效上下文回显。
        trace_id: 链路追踪 ID；未传时由 pipeline 自动生成。
        message: 状态的人类可读说明（拦截原因/澄清提示/异常信息）。
        audit: 审计附加信息（候选表全量、被截断表、护栏原因等），供日志排障。
    """

    status: str = ERROR
    ok: bool = False
    query: str = ""
    sql: str = ""
    confidence: float = 0.0
    used_tables: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    clarify_questions: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    digest: dict[str, Any] | None = None
    columns: list[str] = field(default_factory=list)
    truncated: bool = False
    cache_hit: bool = False
    tenant_id: str = ""
    db_id: str = ""
    request_id: str = ""
    user_id: str = ""
    trace_id: str = ""
    message: str = ""
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典（MCP/HTTP 层直接返回）。"""
        return asdict(self)


@dataclass(slots=True)
class ExecutorQuery:
    """透传给执行器的查询请求。

    字段名刻意与 structured_query_skill.StructuredQueryRequest 保持一致，
    使其 aquery() 可以直接消费本对象（鸭子类型兼容 dataclasses.replace）。

    属性:
        query: 兼容字段，恒为空串。
        sql: 待执行的只读 SQL（已经过 nl2sql 护栏校验）。
        params: 绑定参数；生成式 SQL 固定为空元组。
        cache_scope: 执行侧缓存命名空间。
        cache_ttl: 执行侧缓存 TTL；None 表示用执行器自身策略。
        max_rows: 行数上限。
        db_id: 目标数据源 ID。
    """

    query: str = ""
    sql: str = ""
    params: Any = field(default_factory=tuple)
    cache_scope: str = "structured_query"
    cache_ttl: int | None = None
    max_rows: int | None = None
    db_id: str = ""


@runtime_checkable
class QueryExecutor(Protocol):
    """执行器协议：与 structured_query_skill.StructuredQueryPipeline 鸭子类型兼容。

    实现方需提供异步方法 aquery(request) 并返回带
    rows / error / degraded / truncated 属性的结果对象。
    """

    async def aquery(self, request: Any) -> Any: ...
