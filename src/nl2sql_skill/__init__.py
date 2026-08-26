"""nl2sql_skill：面向 agent 的 MySQL 自然语言转 SQL pipeline 组件。

对外统一导出：配置（NL2SQLConfig / DataSourceConfig）、元数据提供者、
schema 召回、生成器、护栏校验器、总编排（NL2SQLPipeline）、
执行器协议（QueryExecutor / ExecutorQuery），以及 MCP 入口
（create_mcp_server / main）。
"""

from .builder import (
    build_cache,
    build_example_store,
    build_llm,
    build_metadata_provider,
    build_nl2sql_config,
    build_nl2sql_pipeline,
    build_semantic_layer,
    model_tag_for,
)
from .cost_guard import CostDecision, CostGuard, MySQLExplainCostGuard
from .example_store import ExampleRecord, ExampleStore, load_examples
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
from .mcp import create_mcp_server, main
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
    "CostDecision",
    "CostGuard",
    "ExampleRecord",
    "ExampleStore",
    "MySQLExplainCostGuard",
    "build_cache",
    "build_example_store",
    "build_llm",
    "load_examples",
    "build_metadata_provider",
    "build_nl2sql_config",
    "build_nl2sql_pipeline",
    "build_semantic_layer",
    "create_mcp_server",
    "load_semantic_layer",
    "main",
    "model_tag_for",
]

__version__ = "0.1.0"