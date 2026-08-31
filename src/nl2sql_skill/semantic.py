"""语义层：同义词/业务描述/时间过滤声明，从 JSON/YAML 目录加载。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SemanticTable:
    """单张表的语义增强配置。

    属性:
        synonyms: 业务别名列表（如 ["营业额","GMV","sales"]），参与召回匹配。
        description: 业务描述，注入 prompt 帮助 LLM 理解口径。
        requires_time_filter: True 表示该表查询必须带时间范围；
            问题缺少时间词时会追加澄清问题（FR-4.5 槽位校验的最小实现）。
    """

    synonyms: list[str] = field(default_factory=list)
    description: str = ""
    requires_time_filter: bool = False


@dataclass(frozen=True)
class SemanticLayer:
    """整个语义层的内存视图。

    属性:
        version: 全部语义文件内容哈希；变更即失效 SQL 缓存（PRD FR-2.5）。
        tables: 表名（小写）→ SemanticTable。
    """

    version: str
    tables: dict[str, SemanticTable] = field(default_factory=dict)

    def match_tables(self, query_lower: str) -> set[str]:
        """找出问题文本中命中了表名或别名的表集合。

        参数:
            query_lower: 已转小写的用户问题。
        返回:
            命中的表名集合（小写）。匹配规则：子串包含，简单但零依赖。
        """
        hits: set[str] = set()
        for table_name, sem in self.tables.items():
            if table_name in query_lower or any(s.lower() in query_lower for s in sem.synonyms):
                hits.add(table_name)
        return hits


def load_semantic_layer(directory: str | Path | None) -> SemanticLayer:
    """从目录加载语义层配置。

    参数:
        directory: 配置目录路径；None 或不存在/为空时返回空语义层（版本号固定）。
    返回:
        SemanticLayer；version 为全部文件内容的 sha256，任何文件变更都会改变它。
    说明:
        支持 *.json 与 *.yaml/*.yml；YAML 需要 pyyaml（extra semantic）。
        文件格式约定：
        {"orders": {"synonyms": [...], "description": "...", "requires_time_filter": true}}
    """
    if not directory:
        return SemanticLayer(version="empty")
    root = Path(directory)
    if not root.is_absolute():
        # 相对路径按 nl2sql_skill 项目根目录解析，避免随启动目录变化。
        root = Path(__file__).resolve().parents[2] / root
    if not root.is_dir():
        return SemanticLayer(version="missing")

    contents: dict[str, dict[str, object]] = {}
    hasher = hashlib.sha256()
    for path in sorted(root.iterdir()):
        suffix = path.suffix.lower()
        if suffix == ".json":
            raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                contents.update(parsed)  # type: ignore[arg-type]
            hasher.update(raw.encode("utf-8"))
        elif suffix in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError as exc:
                raise RuntimeError(
                    f"解析 {path.name} 需要 pyyaml: pip install 'nl2sql-skill[semantic]'"
                ) from exc
            raw = path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(raw)
            if isinstance(parsed, dict):
                contents.update(parsed)  # type: ignore[arg-type]
            hasher.update(raw.encode("utf-8"))

    tables = {
        str(name).lower(): SemanticTable(
            synonyms=[str(s) for s in cfg.get("synonyms", [])],
            description=str(cfg.get("description", "")),
            requires_time_filter=bool(cfg.get("requires_time_filter", False)),
        )
        for name, cfg in contents.items()
        if isinstance(cfg, dict)
    }
    return SemanticLayer(version=hasher.hexdigest()[:16], tables=tables)
