"""SQL 生成器：prompt 组装 + OpenAI 兼容 LLM 调用（PRD FR-4）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from common_core.providers import OpenAICompatibleLLM

# MySQL 5.x（含 5.7，PRD FR-4.3）受限语法子集提示：窗口函数/CTE 会直接产生不兼容 SQL。
_MYSQL57_CONSTRAINTS = (
    "语法子集约束（MySQL 5.x）：禁止窗口函数（OVER 子句）与公共表表达式 "
    "（WITH/CTE），需要时改用派生表子查询；JSON 函数能力有限，优先使用普通列聚合。"
)


@dataclass(slots=True)
class GenerationOutcome:
    """一次 LLM 生成的结构化产出。

    属性:
        sql: LLM 给出的 SQL 文本；澄清场景为空串。
        confidence: LLM 自评置信度 [0,1]；解析缺失时按 0 处理（保守触发澄清）。
        used_tables: LLM 声明使用的表；仅作参考，权威值以 AST 解析为准。
        assumptions: LLM 声明的口径假设（时间字段选择、指标定义等）。
        clarify_questions: LLM 提出的澄清问题列表。
        repair_used: 是否触发了 JSON 解析失败的一次修复重试（审计用）。
        error: 生成阶段异常信息；正常时为空串。
    """

    sql: str = ""
    confidence: float = 0.0
    used_tables: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    clarify_questions: list[str] = field(default_factory=list)
    repair_used: bool = False
    error: str = ""

    @property
    def is_clarification(self) -> bool:
        """LLM 未给出 SQL 且提出了澄清问题时视为澄清路径。"""
        return not self.sql.strip() and bool(self.clarify_questions)


class SQLGenerator:
    """封装 system prompt 契约与 JSON 输出解析；不负责校验（护栏独立）。"""

    def __init__(self, llm: OpenAICompatibleLLM | None, model_tag: str = "") -> None:
        """初始化生成器。

        参数:
            llm: OpenAI 兼容客户端（common_core.providers.OpenAICompatibleLLM）；
                测试时可注入实现了 chat_json 接口的假对象。None 仅允许测试。
            model_tag: 参与 SQL 缓存 key 的模型标识（model+temperature 哈希），
                由 builder 计算；保证换模型后旧缓存自动失效。
        """
        self.llm = llm
        self.model_tag = model_tag

    async def generate(
        self,
        question: str,
        *,
        schema_blocks: str,
        history_text: str,
        few_shots: list[dict[str, str]] | None = None,
        extra_clarifications: list[str] | None = None,
        server_version: str = "8.0",
    ) -> GenerationOutcome:
        """调用 LLM 把自然语言问题转换为 SQL。

        参数:
            question: 用户当前问题。
            schema_blocks: schema linking 渲染好的候选表上下文。
            history_text: 截断后的多轮历史文本（可为空串）。
            few_shots: few-shot 示例列表 [{"question":..., "sql":...}]，可为空。
            extra_clarifications: pipeline 侧确定性校验发现的槽位缺失提示，
                会并入 prompt 要求 LLM 一并输出到 clarify_questions。
            server_version: MySQL 版本声明，控制语法子集提示。
        返回:
            GenerationOutcome；JSON 两次解析均失败时 error 非空。
        重试策略（PRD FR-4.6）:
            仅当 JSON 解析失败时允许一次修复重试；
            SQL 校验失败不在本层重试，由 pipeline 直接返回 rejected_guardrail。
        """
        messages = [
            {"role": "user", "content": self._build_user_prompt(
                question=question,
                schema_blocks=schema_blocks,
                history_text=history_text,
                few_shots=few_shots or [],
                extra_clarifications=extra_clarifications or [],
                server_version=server_version,
            )},
        ]

        # 第一次调用：传输层异常（超时/断连/鉴权）直接失败返回，
        # 不做修复重试 —— 修复重试只针对"模型输出不是合法 JSON"的场景。
        try:
            parsed = await self._chat_json(messages)
        except Exception as exc:  # noqa: BLE001 - 统一收敛为带原因的 error
            return GenerationOutcome(error=f"LLM 调用失败: {exc}")

        if not parsed:
            # 第一次解析失败：回喂更强的格式要求做唯一一次修复重试。
            messages.append({
                "role": "user",
                "content": "上一次输出不是合法 JSON。请严格只输出一个 JSON 对象，"
                           "字段为 sql/confidence/used_tables/assumptions/clarify_questions。",
            })
            try:
                repaired = await self._chat_json(messages)
            except Exception as exc:  # noqa: BLE001 - 重试阶段网络故障同样明确报错
                return GenerationOutcome(error=f"LLM 调用失败(重试): {exc}")
            if not repaired:
                return GenerationOutcome(error="LLM 返回无法解析为 JSON（含一次修复重试）")
            outcome = self._parse_payload(repaired)
            outcome.repair_used = True
            return outcome
        return self._parse_payload(parsed)

    async def _chat_json(self, messages: list[dict[str, str]]) -> dict:
        """调用 LLM 的 chat_json；客户端未配置时返回空字典（视为解析失败路径）。

        异常不做捕获、向上传播 —— 由 generate() 区分传输错误与解析错误。
        """
        if self.llm is None:
            return {}
        return await self.llm.chat_json(messages=messages, system_prompt=self._system_prompt())

    @staticmethod
    def _parse_payload(parsed: dict) -> GenerationOutcome:
        """把 LLM 返回的 JSON 字典收敛成 GenerationOutcome（缺字段给保守默认值）。"""
        if not parsed:
            return GenerationOutcome(error="LLM 返回无法解析为 JSON")
        try:
            confidence = float(parsed.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        return GenerationOutcome(
            sql=str(parsed.get("sql") or "").strip().rstrip(";"),
            confidence=max(0.0, min(confidence, 1.0)),
            used_tables=[str(t) for t in parsed.get("used_tables") or []],
            assumptions=[str(a) for a in parsed.get("assumptions") or []],
            clarify_questions=[str(q) for q in parsed.get("clarify_questions") or []],
        )

    @staticmethod
    def _system_prompt() -> str:
        """系统提示词：只读契约 + 输出 JSON 结构约定（中文，面向国内业务模型）。"""
        return (
            "你是企业 MySQL 数据库的 NL2SQL 引擎。规则：\n"
            "1. 只能生成单条 SELECT/WITH 只读查询，禁止任何写操作、DDL 与文件导出。\n"
            "2. 只能使用给定 schema 中出现的表和列，禁止编造。\n"
            "3. 使用 MySQL 方言语法。\n"
            "4. 必须只输出一个 JSON 对象，字段：\n"
            '   {"sql": "...", "confidence": 0~1, "used_tables": [],'
            ' "assumptions": [], "clarify_questions": []}\n'
            "5. 当缺少必要条件（时间范围、统计口径、分组粒度）或你对 schema 理解不足时，"
            "sql 留空字符串并在 clarify_questions 里给出具体的中文追问。"
        )

    @staticmethod
    def _syntax_constraint_part(server_version: str) -> str | None:
        """返回 MySQL 5.x 的语法子集约束文案；8.0+ 或无法解析时返回 None。

        版本归一化规则：仅 5.x 视为受限语法（保守起见 5.6/5.7 同策略），
        其余版本一律按 MySQL 8.0 全量方言处理，避免旧库生成不兼容 SQL。
        """
        raw = str(server_version or "").strip()
        head = raw.split(".")[0] if raw else ""
        if head == "5":
            return _MYSQL57_CONSTRAINTS
        return None

    @staticmethod
    def _build_user_prompt(
        *,
        question: str,
        schema_blocks: str,
        history_text: str,
        few_shots: list[dict[str, str]],
        extra_clarifications: list[str],
        server_version: str,
    ) -> str:
        """拼装用户侧 prompt：schema → few-shot → 历史 → 当前问题 → 槽位提醒。"""
        parts: list[str] = [f"# 数据库 schema（MySQL {server_version}）\n{schema_blocks}"]
        constraint = SQLGenerator._syntax_constraint_part(server_version)
        if constraint:
            parts.append(f"# 语法子集约束\n{constraint}")
        if few_shots:
            shots = "\n".join(
                f"问: {shot.get('question', '')}\nSQL: {shot.get('sql', '')}"
                for shot in few_shots
            )
            parts.append(f"# 参考示例\n{shots}")
        if history_text:
            parts.append(f"# 多轮对话历史\n{history_text}")
        parts.append(f"# 当前问题\n{question}")
        if extra_clarifications:
            joined = "; ".join(extra_clarifications)
            parts.append(f"# 系统检测到的可能缺口（供你判断是否需要澄清）\n{joined}")
        return "\n\n".join(parts)
