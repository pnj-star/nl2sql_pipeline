"""NL2SQL 总编排：串联元数据 → 召回 → 生成 → 护栏 →（可选）执行。

状态流转（与 PRD §5.1 status 表一致）：
    数据源未注册/召回为空        → no_schema
    LLM 澄清 / 置信不足 / 缺槽位 → need_clarification
    护栏拦截                     → rejected_guardrail（不缓存、不重试）
    校验通过未执行               → generated
    校验通过且执行成功           → executed（cache_hit 标识 SQL 是否来自缓存）
    内部异常                     → error
缓存规则：仅最终通过校验的 SQL 写入；命中后仍完整重跑护栏再返回（PRD P0 决议）。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
import time
import uuid
from typing import Any, Callable

from common_core.providers import RedisCache

from .config import NL2SQLConfig
from .generator import GenerationOutcome, SQLGenerator
from .guardrails import GuardrailValidator
from .linking import LinkedContext, SchemaLinker, estimate_tokens
from .metadata import MetadataProvider, SchemaSnapshot
from .semantic import SemanticLayer
from .types import (
    ERROR,
    EXECUTED,
    GENERATED,
    GENERATED_CACHE,
    NEED_CLARIFICATION,
    NO_SCHEMA,
    REJECTED_GUARDRAIL,
    ExecutorQuery,
    NL2SQLRequest,
    NL2SQLResult,
)

logger = logging.getLogger(__name__)

# 相对时间词 + 绝对日期模式：用于"缺时间范围"槽位校验（PRD FR-4.5 最小实现）。
_TIME_WORDS = ("今天", "昨天", "本周", "上周", "本月", "上月", "上个月", "最近", "今年", "去年")
_DATE_PATTERN = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]")



def _build_result_digest(rows: list[dict[str, Any]], columns: list[str]) -> dict[str, Any]:
    """FR-6.3：对结果集做轻量数值摘要，供上层直接展示概览。

    返回 {"row_count", "numeric": {列名: {sum,min,max,avg}}}；
    仅统计数值列（排除 bool），聚合值四舍五入到 4 位小数，保证 JSON 可序列化。
    """
    digest: dict[str, Any] = {"row_count": len(rows)}
    numeric: dict[str, dict[str, float]] = {}
    for col in columns:
        values: list[float] = []
        for row in rows:
            value = row.get(col)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                values.append(float(value))
        if values:
            numeric[col] = {
                "sum": round(sum(values), 4),
                "min": round(min(values), 4),
                "max": round(max(values), 4),
                "avg": round(sum(values) / len(values), 4),
            }
    if numeric:
        digest["numeric"] = numeric
    return digest


def _callable_accepts_kwarg(fn: Callable[..., Any] | None, name: str) -> bool:
    """探测可调用对象是否接受指定关键字参数（用于可选能力透传）。"""
    if fn is None:
        return False
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.name == name:
            return True
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False

class NL2SQLPipeline:
    """可复用的 NL2SQL pipeline；依赖全部通过构造注入，便于测试替换。"""

    def __init__(
        self,
        *,
        config: NL2SQLConfig,
        metadata_provider: MetadataProvider,
        linker: SchemaLinker,
        generator: SQLGenerator,
        guardrails: GuardrailValidator,
        semantic: SemanticLayer | None = None,
        executor: Any = None,
        cache: RedisCache | None = None,
        cost_guard: Any | None = None,
        few_shot_provider: Callable[[str, str, float], list[dict[str, str]]] | None = None,
    ) -> None:
        """初始化 pipeline。

        参数:
            config: 行为配置。
            metadata_provider: 元数据快照来源。
            linker: schema 召回器。
            generator: LLM 生成器。
            guardrails: 护栏校验器。
            semantic: 语义层；None 时用空语义层。
            executor: 可选执行器（structured_query_skill 兼容协议）；execute=true 必需。
            cache: 可选 Redis 缓存；None 时禁用 SQL 缓存。
            few_shot_provider: 可选示例库召回函数
                (question, db_id, min_similarity) -> [{"question","sql"}]，
                对接 FR-7 示例自学习库；第三个参数为最小相似度阈值，
                由提供方负责按阈值过滤后返回；None 表示不注入 few-shot。
        """
        self.config = config
        self.metadata_provider = metadata_provider
        self.linker = linker
        self.generator = generator
        self.guardrails = guardrails
        self.semantic = semantic or SemanticLayer(version="empty")
        self.executor = executor
        self.cache = cache
        self.cost_guard = cost_guard
        self._provider_accepts_tenant = _callable_accepts_kwarg(
            few_shot_provider, "tenant_id"
        )
        self.few_shot_provider = few_shot_provider

    # ------------------------------------------------------------------ 入口 --
    async def generate(self, request: NL2SQLRequest) -> NL2SQLResult:
        """执行一次完整的 NL2SQL 流程。

        处理流程:
        1. 生成本次 trace_id，保证即使没有接入追踪后端也能关联日志；
        2. 进入 _generate_inner() 执行元数据召回、schema 召回、LLM 生成、
           护栏校验和可选执行；
        3. 任何未预期异常都在这里收敛为 ``error``，不向 MCP/LangGraph 调用方抛出。

        参数:
            request: 调用入参（见 types.NL2SQLRequest）。
        返回:
            NL2SQLResult，status 字段是调用方唯一需要判断的权威信号。

        状态约定:
            - generated/generated_cache/executed 属于成功路径；
            - need_clarification/no_schema/rejected_guardrail 属于业务终止路径；
            - error 表示内部异常，调用方不应把 message 直接当作 SQL 结果解释。
        """
        result = NL2SQLResult(
            query=request.query,
            tenant_id=request.tenant_id,
            db_id=request.db_id,
            request_id=request.request_id,
            user_id=request.user_id,
        )
        # trace_id 由本层生成（PRD FR-9.3）：调用方未接追踪后端时也有可关联 ID。
        result.trace_id = uuid.uuid4().hex
        t_total = time.perf_counter()
        try:
            final = await self._generate_inner(request, result)
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，任何异常都不得逃逸
            logger.exception("nl2sql internal error request_id=%s", request.request_id)
            result.status = ERROR
            result.ok = False
            result.message = f"内部异常: {exc}"
            final = result
        elapsed = round(time.perf_counter() - t_total, 4)
        if not isinstance(final.audit, dict):
            final.audit = {}
        final.audit["total_s"] = elapsed
        logger.info(
            "nl2sql done req=%s status=%s cache_hit=%s total_s=%.4f "
            "metadata=%.4f linking=%.4f llm=%.4f guardrail=%.4f",
            request.request_id, final.status, final.cache_hit, elapsed,
            final.audit.get("metadata_fetch_s", 0),
            final.audit.get("schema_linking_s", 0),
            final.audit.get("llm_generate_s", 0),
            final.audit.get("guardrail_validate_s", 0),
        )
        return final

    async def _generate_inner(self, request: NL2SQLRequest, result: NL2SQLResult) -> NL2SQLResult:
        """主流程实现（异常由 generate 外层统一兜底）。

        完整流程:
        1. 校验 db_id 是否已注册，并加载元数据快照；
        2. 规范化多轮历史，执行 schema linking，得到候选表和 prompt 上下文；
        3. 计算确定性槽位提示（例如时间敏感表缺少时间范围）；
        4. 查询 SQL 缓存。命中后必须重跑护栏：即使缓存生成时合法，
           护栏规则升级或表白名单变化后旧 SQL 也可能不再允许；
        5. 缓存未命中时注入 few-shot 并调用 LLM；
        6. 判断澄清路径，再执行 sqlglot 护栏校验；
        7. 只有最终通过校验的 SQL 才写缓存；execute=true 时继续委托执行器。

        参数:
            request: 原始请求对象。
            result: generate() 预填充上下文的返回对象，本方法原地补齐状态和结果。

        返回:
            已设置 status/sql/message/audit 等字段的同一个 result 对象。
        """
        t_start = time.perf_counter()
        timings: dict[str, float] = {}
        datasource = self.config.datasources.get(request.db_id.lower())
        if datasource is None:
            return self._finish(result, NO_SCHEMA, message=f"数据源未注册: {request.db_id}")

        t0 = time.perf_counter()
        snapshot = await self.metadata_provider.get_snapshot(request.db_id)
        timings["metadata_fetch_s"] = round(time.perf_counter() - t0, 4)
        history_text, history_hash = self._build_history(request.history)

        t0 = time.perf_counter()
        linked = self.linker.link(request.query, snapshot, self.semantic)
        timings["schema_linking_s"] = round(time.perf_counter() - t0, 4)
        audit = {
            "candidate_tables_all": linked.candidate_names,
            "truncated_tables": linked.truncated_tables,
            "schema_fingerprint": snapshot.fingerprint,
            "semantic_version": self.semantic.version,
            **timings,
        }
        stale_probe = getattr(self.metadata_provider, "is_stale", None)
        if callable(stale_probe):
            audit["schema_stale"] = bool(stale_probe(request.db_id))

        if not linked.candidates:
            return self._finish(result, NO_SCHEMA, message="没有召回到与问题相关的表", audit=audit)

        allowed_tables = self._allowed_tables(snapshot, datasource.visible_tables)
        max_rows = request.max_rows or self.config.max_rows
        threshold = (
            request.clarify_threshold
            if request.clarify_threshold is not None
            else self.config.clarify_threshold
        )
        slot_hints = self._missing_slots(request.query, linked, self.semantic)

        material = self._cache_material(
            query=request.query,
            db_id=request.db_id,
            history_hash=history_hash,
            schema_fingerprint=snapshot.fingerprint,
        )
        t0 = time.perf_counter()
        cached_payload = self._cache_get(material, request.tenant_id)
        timings["cache_lookup_s"] = round(time.perf_counter() - t0, 4)
        audit["cache_lookup_s"] = timings["cache_lookup_s"]
        if cached_payload is not None:
            outcome = self._outcome_from_cache(cached_payload)
            if outcome is not None:
                check = self.guardrails.validate(
                    outcome.sql, allowed_tables=allowed_tables, max_rows=max_rows
                )
                if check.ok:
                    result.cache_hit = True
                    audit["cache_hit"] = True
                    if request.execute:
                        blocked = await self._cost_gate(result, check.sql, max_rows, request, audit)
                        if blocked is not None:
                            return blocked
                        return await self._execute(result, check.sql, max_rows, request, audit)
                    return self._fill_success(result, outcome, check, status=GENERATED_CACHE, audit=audit)
                logger.warning(
                    "cached sql failed guardrail (rules upgraded?) db=%s req=%s reason=%s",
                    request.db_id, request.request_id, check.reason,
                )

        few_shots: list[dict[str, str]] = []
        if self.few_shot_provider is not None:
            # 相似度阈值由配置下发（NL2SQL_EXAMPLE_MIN_SIMILARITY），
            # 提供方实现向量召回并自行完成阈值过滤，低于阈值的示例不得注入。
            if self._provider_accepts_tenant:
                few_shots = self.few_shot_provider(
                    request.query,
                    request.db_id,
                    self.config.example_min_similarity,
                    tenant_id=request.tenant_id,
                )
            else:
                few_shots = self.few_shot_provider(
                    request.query, request.db_id, self.config.example_min_similarity
                )

        top_k = request.top_k_tables or self.config.top_k_tables
        t0 = time.perf_counter()
        try:
            outcome = await self.generator.generate(
                request.query,
                schema_blocks=linked.blocks_text,
                history_text=history_text,
                few_shots=few_shots[:top_k],
                extra_clarifications=slot_hints,
                server_version=datasource.server_version,
            )
        finally:
            timings["llm_generate_s"] = round(time.perf_counter() - t0, 4)
            audit["llm_generate_s"] = timings["llm_generate_s"]
        if outcome.error:
            return self._finish(result, ERROR, message=outcome.error, audit=audit)

        questions = list(outcome.clarify_questions)
        if outcome.is_clarification or outcome.confidence < threshold or slot_hints and not outcome.sql.strip():
            merged = list(dict.fromkeys(questions + slot_hints)) or ["问题信息不足，请补充更多条件"]
            return self._finish(
                result,
                NEED_CLARIFICATION,
                message="需要澄清",
                clarify_questions=merged,
                assumptions=outcome.assumptions,
                confidence=outcome.confidence,
                audit={**audit, "used_tables": outcome.used_tables},
            )

        t0 = time.perf_counter()
        check = self.guardrails.validate(
            outcome.sql, allowed_tables=allowed_tables, max_rows=max_rows
        )
        timings["guardrail_validate_s"] = round(time.perf_counter() - t0, 4)
        audit["guardrail_validate_s"] = timings["guardrail_validate_s"]
        if not check.ok:
            return self._finish(
                result,
                REJECTED_GUARDRAIL,
                message=check.reason,
                confidence=outcome.confidence,
                assumptions=outcome.assumptions,
                audit={**audit, "used_tables_declared": outcome.used_tables, "guardrail_reason": check.reason},
            )

        if request.execute:
            blocked = await self._cost_gate(result, check.sql, max_rows, request, audit)
            if blocked is not None:
                # 成本拦截属于护栏类结果：一律不写缓存（PRD FR-8 缓存规范）。
                return blocked

        self._cache_put(
            material,
            {
                "sql": check.sql,
                "confidence": outcome.confidence,
                "used_tables": check.used_tables,
                "assumptions": outcome.assumptions,
            },
            tenant_id=request.tenant_id,
        )
        audit["used_tables"] = check.used_tables
        audit["repair_used"] = outcome.repair_used

        if request.execute:
            return await self._execute(result, check.sql, max_rows, request, audit)
        timings["total_s"] = round(time.perf_counter() - t_start, 4)
        audit["total_s"] = timings["total_s"]
        return self._fill_success(result, outcome, check, status=GENERATED, audit=audit)

    async def _cost_gate(
        self,
        result: NL2SQLResult,
        sql: str,
        max_rows: int,
        request: NL2SQLRequest,
        audit: dict[str, Any],
    ) -> NL2SQLResult | None:
        """EXPLAIN 成本闸门（FR-5.3）：execute=true 前置检查。

        返回 None 表示放行；被拦截时返回已收口的 rejected_guardrail 结果。
        未配置 cost_guard 时恒放行，保持零成本兼容。
        """
        if self.cost_guard is None:
            return None
        t0 = time.perf_counter()
        decision = await self.cost_guard.evaluate(request.db_id, sql)
        audit["cost_gate_s"] = round(time.perf_counter() - t0, 4)
        audit["cost_gate"] = "allowed" if decision.allowed else "blocked"
        audit["cost_estimated_rows"] = decision.estimated_rows
        if decision.allowed:
            return None
        return self._finish(
            result,
            REJECTED_GUARDRAIL,
            message=f"成本闸门拒绝执行: {decision.reason}",
            sql=sql,
            audit=audit,
        )
    # ---------------------------------------------------------------- 执行侧 --
    async def _execute(
        self,
        result: NL2SQLResult,
        sql: str,
        max_rows: int,
        request: NL2SQLRequest,
        audit: dict[str, Any],
    ) -> NL2SQLResult:
        """委托执行器运行 SQL 并按 FR-6.4 做状态映射。

        参数:
            result: 待补齐执行结果的返回对象。
            sql: 已经通过护栏的只读 SQL。
            max_rows: 本次生效的行数上限；护栏阶段已用它规范化 LIMIT。
            request: 原始请求，用于保留审计上下文。
            audit: 当前累计的诊断信息，会原样写入 result.audit。

        返回:
            执行成功时填充 EXECUTED、rows、columns 和 truncated；
            执行器缺失、下游异常或下游返回错误时映射为 ERROR，
            并在安全范围内保留待执行 SQL 便于排查。
        """
        if self.executor is None:
            return self._finish(result, ERROR, message="执行器未配置，无法 execute=true", audit=audit)
        exec_request = ExecutorQuery(sql=sql, max_rows=max_rows, db_id=request.db_id)
        try:
            raw = await self.executor.aquery(exec_request)
        except Exception as exc:  # noqa: BLE001 - 下游任何异常都归一为 error
            return self._finish(
                result, ERROR, message=f"执行阶段异常: {exc}", sql=sql, audit=audit
            )
        error = getattr(raw, "error", None)
        if error:
            return self._finish(
                result, ERROR, message=f"执行失败: {error}", sql=sql, audit=audit
            )
        rows = [dict(row) for row in getattr(raw, "rows", [])]
        columns = list(rows[0].keys()) if rows else []
        result.status = EXECUTED
        result.ok = True
        result.sql = sql
        result.rows = rows
        result.columns = columns
        result.truncated = bool(getattr(raw, "truncated", False))
        result.digest = _build_result_digest(rows, columns)
        result.audit = audit
        return result

    # -------------------------------------------------------------- 工具方法 --
    @staticmethod
    def _allowed_tables(snapshot: SchemaSnapshot, visible: frozenset[str]) -> set[str]:
        """计算护栏权限复核用的表白名单 = 快照表 ∩ 数据源可见表。

        visible 为空集合表示全部可见（取全部快照表）。
        """
        if not visible:
            return set(snapshot.tables.keys())
        return set(snapshot.tables.keys()) & set(visible)

    def _build_history(self, history: list[dict[str, Any]]) -> tuple[str, str]:
        """规范化并按预算截断多轮历史。

        参数:
            history: 原始历史列表；每项 {"role","content"} 或 {"question","sql"}。
        返回:
            (截断后的文本, 全量规范化的 sha1)；空历史返回 ("", "")。
        规则:
            - 从最新轮次向前保留，直到超出 NL2SQL_HISTORY_MAX_TOKENS；
            - hash 用全量内容计算（不随截断变化），保证缓存 key 稳定性。
        """
        if not history:
            return "", ""
        lines: list[str] = []
        for item in history:
            if not isinstance(item, dict):
                continue
            if "question" in item or "sql" in item:
                q = str(item.get("question") or "")
                s = str(item.get("sql") or "")
                lines.append(f"问: {q}\n此前SQL: {s}")
            else:
                role = str(item.get("role") or "user")
                content = str(item.get("content") or "")
                lines.append(f"{role}: {content}")
        canonical = "\n".join(lines)
        digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
        kept: list[str] = []
        used = 0
        for line in reversed(lines):
            cost = estimate_tokens(line)
            if used + cost > self.config.history_max_tokens:
                break
            kept.append(line)
            used += cost
        return "\n".join(reversed(kept)), digest

    @staticmethod
    def _missing_slots(
        question: str, linked: LinkedContext, semantic: SemanticLayer
    ) -> list[str]:
        """确定性槽位校验的最小实现：涉及时间敏感表但问题无时间词时提示补充。

        返回中文提示列表（并入 LLM 的 clarify_questions）；无缺口时为空列表。
        """
        has_time = any(w in question for w in _TIME_WORDS) or bool(_DATE_PATTERN.search(question))
        needs_time = any(
            semantic.tables.get(name).requires_time_filter
            for name in linked.candidate_names
            if semantic.tables.get(name) is not None
        )
        if needs_time and not has_time:
            return ["请补充时间范围（如上个月、2025年Q3）"]
        return []

    def _cache_material(
        self, *, query: str, db_id: str, history_hash: str,
        schema_fingerprint: str = "",
    ) -> str:
        """构造 SQL 缓存 material（进 RedisCache 的 key 组成部分）。

        组成（PRD FR-8）：规范化 query + db_id + semantic_version +
        schema_fingerprint + model_tag +（可选）history_hash。
        任一变更即天然失效。

        参数:
            query: 用户原始问题；这里先做空白归一化和小写化。
            db_id: 目标数据源 ID。
            history_hash: 全量多轮历史摘要；是否参与 key 由配置决定。
            schema_fingerprint: 元数据快照指纹；表/列/外键变化后会改变。

        返回:
            序列化后的缓存 material 字符串。tenant_id 不在这里拼接，
            由 RedisCache 按租户追加隔离前缀。
        """
        normalized = re.sub(r"\s+", " ", query).strip().lower()
        payload = {
            "v": 1,
            "q": normalized,
            "db": db_id.lower(),
            "sv": self.semantic.version,
            "sf": schema_fingerprint,
            "m": self.generator.model_tag,
        }
        if self.config.cache_key_include_history and history_hash:
            payload["h"] = history_hash
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _cache_get(self, material: str, tenant_id: str) -> dict[str, Any] | None:
        """读取缓存的 SQL 生成结果。

        参数:
            material: _cache_material() 构造的完整检索签名。
            tenant_id: 租户 ID，用于强制隔离不同租户的 SQL 缓存。

        返回:
            合法 JSON 字典；未启用、Redis 故障、值为空或结构非法时返回 None。
        """
        if self.cache is None or not self.config.sql_cache_enabled:
            return None
        try:
            raw = self.cache.get(
                self.config.sql_cache_scope, material, tenant_id=tenant_id
            )
        except Exception:  # noqa: BLE001 - 缓存故障不阻断主流程
            logger.exception("sql cache read failed")
            return None
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    def _cache_put(self, material: str, payload: dict[str, Any], *, tenant_id: str) -> None:
        """写入通过护栏校验后的 SQL 结果。

        参数:
            material: 与 _cache_get 相同的缓存签名。
            payload: 含 sql/confidence/used_tables/assumptions 的字典。
            tenant_id: 租户 ID，用于隔离缓存命名空间。

        说明:
            Redis 故障只记录日志并降级为无缓存，不影响本次生成结果。
        """
        if self.cache is None or not self.config.sql_cache_enabled:
            return
        try:
            self.cache.set(
                self.config.sql_cache_scope,
                material,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ttl=self.config.sql_cache_ttl_seconds,
                tenant_id=tenant_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("sql cache write failed")

    @staticmethod
    def _outcome_from_cache(payload: dict[str, Any]) -> GenerationOutcome | None:
        """把缓存 JSON 还原成 GenerationOutcome。

        参数:
            payload: _cache_get 反序列化得到的字典。

        返回:
            GenerationOutcome。SQL 为空时返回 None；confidence 解析失败时
            按保守默认值 0 处理，后续仍会完整重跑护栏。
        """
        sql = str(payload.get("sql") or "").strip()
        if not sql:
            return None
        try:
            confidence = float(payload.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        return GenerationOutcome(
            sql=sql,
            confidence=confidence,
            used_tables=[str(t) for t in payload.get("used_tables") or []],
            assumptions=[str(a) for a in payload.get("assumptions") or []],
        )

    @staticmethod
    def _fill_success(
        result: NL2SQLResult,
        outcome: GenerationOutcome,
        check: Any,
        *,
        status: str,
        audit: dict[str, Any],
    ) -> NL2SQLResult:
        """填充“生成成功但尚未执行”场景的公共字段。

        参数:
            result: 要补齐的返回对象。
            outcome: LLM 生成或缓存还原出的 SQL 及置信信息。
            check: 护栏通过后的 GuardrailResult，其规范化 SQL 是唯一可信输出。
            status: GENERATED 或 GENERATED_CACHE。
            audit: 召回、缓存、使用表等诊断信息。

        返回:
            补齐 status/ok/sql/confidence/used_tables/assumptions 后的 result。
        """
        result.status = status
        result.ok = True
        result.sql = check.sql
        result.confidence = outcome.confidence
        result.used_tables = list(check.used_tables)
        result.assumptions = outcome.assumptions
        result.audit = audit
        return result

    @staticmethod
    def _finish(
        result: NL2SQLResult,
        status: str,
        *,
        message: str = "",
        sql: str = "",
        clarify_questions: list[str] | None = None,
        assumptions: list[str] | None = None,
        confidence: float = 0.0,
        audit: dict[str, Any] | None = None,
    ) -> NL2SQLResult:
        """所有业务终止分支的统一收口。

        该方法不会抛异常，而是把当前分支的状态、说明和已有诊断写回 result，
        保证 MCP / agent 层始终拿到稳定的 NL2SQLResult 结构。

        参数:
            result: 要补齐的返回对象。
            status: 本模块顶部的状态常量之一。
            message: 中文状态说明，例如拦截原因、澄清提示或异常摘要。
            sql: 仅在执行失败等场景回填待排查 SQL；普通拒绝路径保持空串。
            clarify_questions: 需要用户补充的问题列表。
            assumptions: LLM 已声明的口径假设。
            confidence: LLM 自评置信度。
            audit: 本分支新增的诊断字段，会与 result 中已有 audit 合并。

        返回:
            补齐后的同一个 result 对象。
        """
        result.status = status
        result.ok = status != ERROR
        result.message = message
        if sql:
            result.sql = sql
        if clarify_questions:
            result.clarify_questions = clarify_questions
        if assumptions:
            result.assumptions = assumptions
        result.confidence = confidence
        if audit:
            result.audit = {**(result.audit or {}), **audit}
        return result
