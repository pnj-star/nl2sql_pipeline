"""Schema Linking：从问题文本召回候选表并按 token 预算裁剪（PRD FR-3）。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .metadata import SchemaSnapshot, TableMeta
from .semantic import SemanticLayer

_TOKEN_PATTERN = re.compile(r"[a-z0-9_\u4e00-\u9fff]+")

# 词边界匹配：要求 term 左右是非字母数字字符（或行首/行尾）。
# 用于列名命中判断，避免 "name" 误匹配 "hostname" 这类子串假阳性（PRD A2 修复）。
_WORD_BOUNDARY_CACHE: dict[str, re.Pattern[str]] = {}


def contains_word(text: str, term: str) -> bool:
    """带缓存的词边界子串匹配。

    参数:
        text: 已转小写的待检索文本。
        term: 已转小写的目标词（表名/列名等 ASCII 标识符）。
    返回:
        True 表示 term 以完整词的形式出现在 text 中。
        说明: 中文没有 \\b 语义，因此该函数仅用于 ASCII 标识符；
        中文同义词仍走 SemanticLayer.match_tables 的子串包含逻辑。
    """
    if not term:
        return False
    pattern = _WORD_BOUNDARY_CACHE.get(term)
    if pattern is None:
        pattern = re.compile(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])")
        _WORD_BOUNDARY_CACHE[term] = pattern
    return pattern.search(text) is not None


def estimate_tokens(text: str) -> int:
    """粗略估算文本 token 数（零依赖启发式）。

    规则: 中英混排场景按 2 字符 ≈ 1 token 计。仅用于预算控制，
    不追求与具体 tokenizer 对齐；真实计费以 LLM 返回 usage 为准。
    """
    return max(1, math.ceil(len(text) / 2))


@dataclass(slots=True)
class CandidateTable:
    """一个候选表及其召回得分。

    属性:
        table: 表结构描述。
        score: 召回得分，越高越相关。
        reason: 命中原因（"问题命中"/"同义词命中"/"外键关联"等），写入审计。
    """

    table: TableMeta
    score: float
    reason: str


@dataclass(slots=True)
class LinkedContext:
    """一次召回的完整产出。

    属性:
        candidates: 预算内的候选表（已排序）。
        truncated_tables: 因超出 token 预算被裁掉的表名列表（审计用）。
        blocks_text: 拼装好的 schema 上下文文本，直接注入 prompt。
        total_tokens: blocks_text 的估算 token 数。
    """

    candidates: list[CandidateTable] = field(default_factory=list)
    truncated_tables: list[str] = field(default_factory=list)
    blocks_text: str = ""
    total_tokens: int = 0

    @property
    def candidate_names(self) -> list[str]:
        """返回候选表名列表（小写）。"""
        return [c.table.name.lower() for c in self.candidates]


class SchemaLinker:
    """基于词法/同义词匹配的召回器（可插拔 embedding 升级点）。"""

    def __init__(
        self,
        *,
        top_k_tables: int = 8,
        schema_context_max_tokens: int = 4000,
        embedder=None,
    ) -> None:
        """初始化召回器。

        参数:
            top_k_tables: 最多保留多少张候选表。
            schema_context_max_tokens: 注入 prompt 的 schema 上下文预算。
            embedder: 可选嵌入函数 embed(text)->list[float]，预留升级接口；
                None 时使用纯词法打分（M0 默认）。
        """
        self.top_k_tables = top_k_tables
        self.schema_context_max_tokens = schema_context_max_tokens
        self.embedder = embedder

    def link(
        self,
        query: str,
        snapshot: SchemaSnapshot,
        semantic: SemanticLayer,
    ) -> LinkedContext:
        """对一个问题执行召回 → 外键扩展 → 预算裁剪。

        参数:
            query: 用户原始问题。
            snapshot: 目标数据源的元数据快照。
            semantic: 语义层（提供同义词）。
        返回:
            LinkedContext；无任何命中时候选为空（pipeline 映射 no_schema）。
        打分规则（词法版）:
            +5 表名出现在问题中; +4 同义词命中; +3 列名出现在问题中;
            +2 列注释与问题共享关键词; +1 表注释/语义描述与问题共享关键词;
            全部为 0 时触发字符 bigram 兜底召回。得分>0 才进入候选池。
        """
        query_lower = query.lower()
        tokens = set(_TOKEN_PATTERN.findall(query_lower))
        scored: dict[str, CandidateTable] = {}
        synonym_hits = semantic.match_tables(query_lower)

        for name_lower, table in snapshot.tables.items():
            score = 0.0
            reasons: list[str] = []
            if name_lower in query_lower:
                score += 5
                reasons.append("问题命中")
            if name_lower in synonym_hits:
                score += 4
                reasons.append("同义词命中")
            for column in table.columns:
                column_lower = column.name.lower()
                # 短名（<=2 字符，如 id）只接受精确 token 命中，
                # 长名允许词边界子串，杜绝 hostname 含 name 的误报。
                matched = column_lower in tokens or (
                    len(column_lower) > 2 and contains_word(query_lower, column_lower)
                )
                if matched:
                    score += 3
                    reasons.append(f"列名命中:{column.name}")
                # 列注释参与匹配：业务人员通常用中文描述列含义，
                # 如 "下单时间" / "客户姓名"，这些信息不在列名里。
                col_comment_hits = sum(
                    1 for tk in _tokenize_cn(column.comment or "") if tk in tokens
                )
                if col_comment_hits:
                    score += min(col_comment_hits, 2)
                    reasons.append(f"列注释命中:{column.name}")

            comment_hits = sum(1 for tk in _tokenize_cn(table.comment) if tk in tokens)
            if comment_hits:
                score += min(comment_hits, 3)
                reasons.append("注释命中")

            # 语义层 description 参与匹配（补充同义词覆盖不到的业务口径）。
            sem_cfg = semantic.tables.get(name_lower)
            if sem_cfg and sem_cfg.description:
                desc_hits = sum(
                    1 for tk in _tokenize_cn(sem_cfg.description) if tk in tokens
                )
                if desc_hits:
                    score += min(desc_hits, 2)
                    reasons.append("语义描述命中")

            if score > 0:
                scored[name_lower] = CandidateTable(
                    table=table,
                    score=score,
                    reason=";".join(dict.fromkeys(reasons)),
                )

        # 主打分全部为 0 时，退化到字符 bigram 相似度兜底召回，
        # 避免因为表名/同义词/列名都不在用户措辞里就返回空结果。
        if not scored:
            scored = self._bigram_fallback(query_lower, snapshot, semantic)

        ranked = sorted(scored.values(), key=lambda c: c.score, reverse=True)[
            : self.top_k_tables
        ]

        # 一跳外键扩展：候选表的引用表自动入池（JOIN 推理前提，PRD FR-3.2）。
        selected: dict[str, CandidateTable] = {c.table.name.lower(): c for c in ranked}
        for cand in list(ranked):
            # foreign_keys 结构: {本表列名: (引用表名, 引用列名)}，
            # 因此需要先解出 value 再拆元组，避免把列名误当表名。
            for _col_name, (ref_table, _ref_col) in cand.table.foreign_keys.items():
                if ref_table in selected or ref_table not in snapshot.tables:
                    continue
                selected[ref_table] = CandidateTable(
                    table=snapshot.tables[ref_table],
                    score=cand.score + 0.5,
                    reason=f"外键关联({cand.table.name})",
                )

        # 按分数排序后逐块渲染，超预算的表记入 truncated_tables。
        ordered = sorted(selected.values(), key=lambda c: c.score, reverse=True)
        context = LinkedContext()
        budget_parts: list[str] = []
        used_tokens = 0
        for cand in ordered:
            block = render_table_block(cand.table, semantic)
            block_tokens = estimate_tokens(block)
            if used_tokens + block_tokens > self.schema_context_max_tokens:
                context.truncated_tables.append(cand.table.name.lower())
                continue
            budget_parts.append(block)
            used_tokens += block_tokens
            context.candidates.append(cand)
        context.blocks_text = "\n\n".join(budget_parts)
        context.total_tokens = used_tokens
        return context

    def _bigram_fallback(
        self,
        query_lower: str,
        snapshot: SchemaSnapshot,
        semantic: SemanticLayer,
    ) -> dict[str, CandidateTable]:
        """字符 bigram 相似度兜底：主打分为零时的最后防线。

        对每张表拼接所有文本字段（表名、表注释、列名、列注释、
        同义词、语义描述），提取字符 bigram 与查询 bigram 计算重叠率。
        重叠率超过阈值的表以低分入池，让 LLM 有机会看到相关 schema。
        """
        query_bigrams = _char_bigrams(query_lower)
        if not query_bigrams:
            return {}
        results: dict[str, CandidateTable] = {}
        for name_lower, table in snapshot.tables.items():
            parts: list[str] = [
                name_lower,
                table.comment or "",
            ]
            parts.extend(col.name for col in table.columns)
            parts.extend(col.comment or "" for col in table.columns)
            sem_cfg = semantic.tables.get(name_lower)
            if sem_cfg:
                parts.extend(sem_cfg.synonyms)
                if sem_cfg.description:
                    parts.append(sem_cfg.description)
            combined = " ".join(p for p in parts if p).lower()
            table_bigrams = _char_bigrams(combined)
            if not table_bigrams:
                continue
            overlap = len(query_bigrams & table_bigrams) / len(query_bigrams)
            if overlap > 0.12:
                results[name_lower] = CandidateTable(
                    table=table,
                    score=round(overlap * 3, 2),
                    reason=f"bigram召回({overlap:.0%})",
                )
        return results


def _tokenize_cn(text: str) -> set[str]:
    """把中文/英文混合文本切成短词集合（用于注释命中打分）。"""
    words: set[str] = set()
    for raw in _TOKEN_PATTERN.findall(text.lower()):
        if len(raw) <= 4:
            words.add(raw)
        else:
            # 英文长词按驼峰/下划线再切一刀；中文长串取 2-gram 近似。
            parts = re.split(r"[_\s]", raw)
            for part in parts:
                if len(part) <= 6:
                    words.add(part)
    return words


def _char_bigrams(text: str) -> set[str]:
    """提取字符 bigram 集合（用于兜底召回的相似度计算）。"""
    bigrams: set[str] = set()
    for tok in _TOKEN_PATTERN.findall(text):
        for i in range(len(tok) - 1):
            bigrams.add(tok[i : i + 2])
    return bigrams


def render_table_block(table: TableMeta, semantic: SemanticLayer) -> str:
    """把一张表渲染成 prompt 友好的 CREATE TABLE 样式文本块。

    参数:
        table: 表结构描述。
        semantic: 语义层（补充业务别名/描述/时间过滤声明）。
    返回:
        形如 "-- 表 orders (别名词)\\nCREATE TABLE ... ;" 的文本块。
    """
    sem = semantic.tables.get(table.name.lower())
    lines: list[str] = []
    header = f"-- 表 {table.name}"
    if sem and sem.synonyms:
        header += f"（业务别名: {'、'.join(sem.synonyms)}）"
    if sem and sem.description:
        header += f"\n-- 说明: {sem.description}"
    if sem and sem.requires_time_filter:
        header += "\n-- 注意: 该表查询通常需要限定时间范围"
    lines.append(header)
    col_defs = []
    for col in table.columns:
        piece = f"{col.name} {col.data_type}".strip()
        if col.comment:
            piece += f" COMMENT '{col.comment}'"
        col_defs.append(piece)
    pk = ", ".join(table.primary_keys)
    if pk:
        col_defs.append(f"PRIMARY KEY ({pk})")
    for col_name, (ref_table, ref_col) in table.foreign_keys.items():
        col_defs.append(
            f"FOREIGN KEY ({col_name}) REFERENCES {ref_table}({ref_col})"
        )
    lines.append(f"CREATE TABLE {table.name} (\n  " + ",\n  ".join(col_defs) + "\n);")
    return "\n".join(lines)
