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
import json
import logging
import re
import uuid
from typing import Any, Callable

from common_core.providers import RedisCache

from .config import NL2SQLConfig
from .generator import GenerationOutcome, SQLGenerator
from .guardrails import GuardrailValidator
from .linking import LinkedContext, SchemaLinker, estimate_tokens
from .metadata import MetadataError, MetadataProvider, SchemaSnapshot
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
        self.few_shot_provider = few_shot_provider

    # ------------------------------------------------------------------ 入口 --
    async def generate(self, request: NL2SQLRequest) -> NL2SQLResult:
        """执行一次完整的 NL2SQL 流程。

        参数:
            request: 调用入参（见 types.NL2SQLRequest）。
        返回:
            NL2SQLResult，status 字段是调用方唯一需要判断的权威信号。
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
        try:
            return await self._generate_inner(request, result)
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，任何异常都不得逃逸
            logger.exception("nl2sql internal error request_id=%s", request.request_id)
            result.status = ERROR
            result.ok = False
            result.message = f"内部异常: {exc}"
            return result

    async def _generate_inner(self, request: NL2SQLRequest, result: NL2SQLResult) -> NL2SQLResult:
        """主流程实现（异常由 generate 外层统一兜底）。"""
        datasource = self.config.datasources.get(request.db_id.lower())
        if datasource is None:
            return self._finish(result, NO_SCHEMA, message=f"数据源未注册: {request.db_id}")

        snapshot = await self.metadata_provider.get_snapshot(request.db_id)
        history_text, history_hash = self._build_history(request.history)

        linked = self.linker.link(request.query, snapshot, self.semantic)
        audit = {
            "candidate_tables_all": linked.candidate_names,
            "truncated_tables": linked.truncated_tables,
            "schema_fingerprint": snapshot.fingerprint,
            "semantic_version": self.semantic.version,
        }

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
        )
        cached_payload = self._cache_get(material, request.tenant_id)
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
            few_shots = self.few_shot_provider(
                request.query, request.db_id, self.config.example_min_similarity
            )

        top_k = request.top_k_tables or self.config.top_k_tables
        outcome = await self.generator.generate(
            request.query,
            schema_blocks=linked.blocks_text,
            history_text=history_text,
            few_shots=few_shots[:top_k],
            extra_clarifications=slot_hints,
            server_version=datasource.server_version,
        )
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

        check = self.guardrails.validate(
            outcome.sql, allowed_tables=allowed_tables, max_rows=max_rows
        )
        if not check.ok:
            return self._finish(
                result,
                REJECTED_GUARDRAIL,
                message=check.reason,
                confidence=outcome.confidence,
                assumptions=outcome.assumptions,
                audit={**audit, "used_tables_declared": outcome.used_tables, "guardrail_reason": check.reason},
            )

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
        return self._fill_success(result, outcome, check, status=GENERATED, audit=audit)

    # ---------------------------------------------------------------- 执行侧 --
    async def _execute(
        self,
        result: NL2SQLResult,
        sql: str,
        max_rows: int,
        request: NL2SQLRequest,
        audit: dict[str, Any],
    ) -> NL2SQLResult:
        """委托执行器运行 SQL 并按 FR-6.4 做状态映射。"""
        if self.executor is None:
            return self._finish(result, ERROR, message="执行器未配置，无法 execute=true", audit=audit)
        exec_request = ExecutorQuery(sql=sql, max_rows=max_rows)
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

    def _cache_material(self, *, query: str, db_id: str, history_hash: str) -> str:
        """构造 SQL 缓存 material（进 RedisCache 的 key 组成部分）。

        组成（PRD FR-8）：规范化 query + db_id + semantic_version +
        model_tag +（可选）history_hash。任一变更即天然失效。
        """
        normalized = re.sub(r"\s+", " ", query).strip().lower()
        payload = {
            "v": 1,
            "q": normalized,
            "db": db_id.lower(),
            "sv": self.semantic.version,
            "m": self.generator.model_tag,
        }
        if self.config.cache_key_include_history and history_hash:
            payload["h"] = history_hash
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _cache_get(self, material: str, tenant_id: str) -> dict[str, Any] | None:
        """读缓存；Redis 不可用/未启用时静默返回 None。"""
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
        """写缓存；仅最终通过校验的成功结果会走到这里（失败路径不落缓存）。"""
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
        """把缓存 JSON 还原成 GenerationOutcome；结构不合法时返回 None 当作未命中。"""
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
        """填充生成成功（未执行）场景的公共字段。"""
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
        """终止分支的统一收口：设置状态、消息并回填审计。"""
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
