"""few-shot 示例库（PRD FR-7 读侧）：从 JSONL 文件加载已验证的 question/sql 示例。

- 文件 schema：每行 JSON {"question","sql","db_id"?,"verified"?,"tenant_id"?,"semantic_version"?}；
- 召回时按 db_id 隔离过滤（空 db_id 示例视为全库通用），按字符相似度排序取 top_k，
  低于 min_similarity 的示例不注入；
- 写侧（去重、复用护栏校验等）由外部运营工具负责，本模块只提供只读召回，
  保持 skill 内部独立可复用。
"""

from __future__ import annotations

import difflib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExampleRecord:
    """一条已验证的 question → sql 示例。

    属性:
        question: 用户问题的规范化文本。
        sql: 对应的只读 SQL。
        db_id: 所属数据源；空串表示全库通用。
        tenant_id: 所属租户；空串表示全部租户可见。
        verified: 是否经过人工/护栏核验；False 的示例不参与召回。
    """

    question: str
    sql: str
    db_id: str = ""
    tenant_id: str = ""
    verified: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ExampleRecord":
        """从 JSON 字典构造；question/sql 缺失或为空时抛 ValueError。"""
        question = str(raw.get("question", "")).strip()
        sql = str(raw.get("sql", "")).strip()
        if not question or not sql:
            raise ValueError("示例行缺少 question 或 sql")
        return cls(
            question=question,
            sql=sql,
            db_id=str(raw.get("db_id", "")).strip(),
            tenant_id=str(raw.get("tenant_id", "")).strip(),
            verified=bool(raw.get("verified", True)),
        )


def load_examples(path: Path) -> list[ExampleRecord]:
    """加载 JSONL 文件；跳过损坏行并告警，文件缺失时按空库处理不抛错。"""
    if not path.exists():
        logger.warning("示例库文件不存在，按空库处理: %s", path)
        return []
    records: list[ExampleRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                records.append(ExampleRecord.from_dict(raw))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning("跳过损坏的示例行 %s:%d: %s", path, line_no, exc)
    return records


class ExampleStore:
    """只读 few-shot 示例库；启动时一次性加载进内存，之后不可变。

    参数:
        examples: 全量示例列表。
        default_top_k: search() 省略 top_k 时使用的截断数。
        min_similarity: 相似度下限；低于该值的示例不返回。
    """

    def __init__(
        self,
        examples: list[ExampleRecord],
        *,
        default_top_k: int = 3,
        min_similarity: float = 0.75,
    ) -> None:
        self._examples = examples
        self.default_top_k = default_top_k
        self.min_similarity = min_similarity

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        default_top_k: int = 3,
        min_similarity: float = 0.75,
    ) -> "ExampleStore":
        """从 JSONL 路径构造示例库；路径不存在时得到空库。"""
        return cls(
            load_examples(Path(path)),
            default_top_k=default_top_k,
            min_similarity=min_similarity,
        )

    def __len__(self) -> int:
        return len(self._examples)

    def search(
        self,
        question: str,
        db_id: str,
        min_similarity: float | None = None,
        *,
        top_k: int | None = None,
        tenant_id: str = "",
    ) -> list[dict[str, str]]:
        """按 db_id 隔离 + 字符相似度召回 top_k 示例（few_shot_provider 协议）。

        参数:
            question: 待匹配的用户问题。
            db_id: 数据源标识；示例的 db_id 非空且不等于它时被排除。
            min_similarity: 相似度下限；None 用构造时的默认值。
            top_k: 返回条数上限；None 用构造时的默认值。
            tenant_id: 租户标识；示例带 tenant_id 且不等于它时被排除。

        返回:
            [{"question","sql"}] 列表，按相似度降序。
        """
        threshold = self.min_similarity if min_similarity is None else min_similarity
        limit = self.default_top_k if top_k is None else top_k
        matched: list[tuple[float, ExampleRecord]] = []
        for ex in self._examples:
            if ex.db_id and ex.db_id != db_id:
                continue
            if ex.tenant_id and ex.tenant_id != tenant_id:
                continue
            if not ex.verified:
                continue
            sim = difflib.SequenceMatcher(None, question, ex.question).ratio()
            if sim < threshold:
                continue
            matched.append((sim, ex))
        matched.sort(key=lambda item: item[0], reverse=True)
        return [
            {"question": ex.question, "sql": ex.sql}
            for _, ex in matched[:limit]
        ]


__all__ = ["ExampleRecord", "ExampleStore", "load_examples"]