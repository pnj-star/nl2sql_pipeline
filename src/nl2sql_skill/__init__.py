"""nl2sql_skill：面向 agent 的 MySQL 自然语言转 SQL pipeline 组件。

当前版本仅提供纯 Python pipeline（无 MCP/SKILL 入口），核心链路：
元数据快照 → schema 召回 → LLM 生成 → sqlglot 护栏校验 →（可选）执行。
"""

from .config import DataSourceConfig, NL2SQLConfig
from .guardrails import GuardrailResult, GuardrailValidator
from .linking import LinkedContext, SchemaLinker
from .metadata import (
    ColumnMeta,
    InformationSchemaProvider,
    MetadataError,
    SchemaSnapshot,
    StaticSchemaProvider,
    TableMeta,
)
from .pipeline import NL2SQLPipeline
from .semantic import SemanticLayer, SemanticTable, load_semantic_layer
from .types import (
    EXECUTED,
    ERROR,
    GENERATED,
    GENERATED_CACHE,
    NEED_CLARIFICATION,
    NO_SCHEMA,
    REJECTED_GUARDRAIL,
    ExecutorQuery,
    NL2SQLRequest,
    NL2SQLResult,
    QueryExecutor,
)

__all__ = [
    "ColumnMeta",
    "DataSourceConfig",
    "ERROR",
    "EXECUTED",
    "ExecutorQuery",
    "GENERATED",
    "GENERATED_CACHE",
    "GuardrailResult",
    "GuardrailValidator",
    "InformationSchemaProvider",
    "LinkedContext",
    "MetadataError",
    "NEED_CLARIFICATION",
    "NL2SQLConfig",
    "NL2SQLPipeline",
    "NL2SQLRequest",
    "NL2SQLResult",
    "NO_SCHEMA",
    "QueryExecutor",
    "REJECTED_GUARDRAIL",
    "SchemaLinker",
    "SchemaSnapshot",
    "SemanticLayer",
    "SemanticTable",
    "StaticSchemaProvider",
    "TableMeta",
    "load_semantic_layer",
]
