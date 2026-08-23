"""依赖装配：从环境变量或显式入参构造完整 pipeline（对齐仓库 builder 惯例）。"""

from __future__ import annotations

import hashlib
from typing import Any

from common_core.config import CacheConfig, ConfigError, LLMConfig, load_env_files
from common_core.providers import OpenAICompatibleLLM, RedisCache

from .config import NL2SQLConfig
from .generator import SQLGenerator
from .guardrails import GuardrailValidator
from .linking import SchemaLinker
from .metadata import InformationSchemaProvider
from .pipeline import NL2SQLPipeline
from .semantic import SemanticLayer, load_semantic_layer


def build_nl2sql_config(
    *,
    env: dict[str, str] | None = None,
    dotenv_paths: tuple[str, ...] | list[str] | None = None,
    override: bool = False,
) -> NL2SQLConfig:
    """构造行为配置（可选先加载 .env 文件）。

    参数:
        env: 注入的配置字典；None 时读 os.environ。
        dotenv_paths: 需要先加载的 .env 路径列表。
        override: .env 是否覆盖已有环境变量。
    返回:
        NL2SQLConfig。
    """
    if dotenv_paths:
        load_env_files(*dotenv_paths, override=override)
    return NL2SQLConfig.from_env(env=env)


def build_cache(
    *,
    env: dict[str, str] | None = None,
    dotenv_paths: tuple[str, ...] | list[str] | None = None,
    prefix: str = "REDIS_",
) -> RedisCache:
    """构造 Redis 缓存客户端（common_core 通用实现，故障自动降级）。"""
    if dotenv_paths:
        load_env_files(*dotenv_paths, override=False)
    return RedisCache(CacheConfig.from_env(prefix=prefix, env=env))


def build_llm(*, env: dict[str, str] | None = None) -> OpenAICompatibleLLM:
    """按 NL2SQL_LLM_ 前缀构造 OpenAI 兼容客户端并校验必填项。

    参数:
        env: 注入配置字典；None 时读 os.environ。
    返回:
        OpenAICompatibleLLM。
    异常:
        ConfigError: base_url 或 model 缺失时 fail-fast。
    """
    llm_config = LLMConfig.from_env(prefix="NL2SQL_LLM_", env=env)
    missing = [name for name, val in (("BASE_URL", llm_config.base_url), ("MODEL", llm_config.model)) if not val]
    if missing:
        raise ConfigError("缺少必需的 LLM 配置: NL2SQL_LLM_" + ", NL2SQL_LLM_".join(missing))
    return OpenAICompatibleLLM(llm_config)


def build_metadata_provider(config: NL2SQLConfig) -> InformationSchemaProvider:
    """用配置里的数据源注册表构造元数据提供者（information_schema 采集）。"""
    return InformationSchemaProvider(config.datasources)


def build_semantic_layer(config: NL2SQLConfig) -> SemanticLayer:
    """从配置目录加载语义层；目录未配置/为空时返回空语义层。"""
    return load_semantic_layer(config.semantic_dir or None)


def model_tag_for(env: dict[str, str] | None = None) -> str:
    """计算参与 SQL 缓存 key 的模型标识（model+temperature 的 sha1 前 12 位）。

    参数:
        env: 与 build_llm 相同的配置来源。
    返回:
        短哈希字符串；模型或温度变化都会改变它，从而使旧缓存失效。
    """
    llm_config = LLMConfig.from_env(prefix="NL2SQL_LLM_", env=env)
    raw = f"{llm_config.model}:{llm_config.temperature}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def build_nl2sql_pipeline(
    config: NL2SQLConfig | None = None,
    *,
    env: dict[str, str] | None = None,
    dotenv_paths: tuple[str, ...] | list[str] | None = None,
    llm: Any | None = None,
    executor: Any | None = None,
    cache: RedisCache | None = None,
    semantic: SemanticLayer | None = None,
    few_shot_provider: Any | None = None,
) -> NL2SQLPipeline:
    """一步装配完整 pipeline；所有依赖均可显式注入覆盖（测试友好）。

    参数:
        config: 行为配置；None 时由 env/dotenv_paths 构造。
        env / dotenv_paths: 构造 config 与 LLM 时的配置来源。
        llm: 显式注入的 LLM 客户端（测试注入假对象）；None 时按环境构建真客户端。
        executor: 执行器（structured_query_skill 兼容协议）；execute=true 时必须提供。
        cache: SQL 缓存；None 时禁用缓存。
        semantic: 语义层；None 时按 config.semantic_dir 加载。
        few_shot_provider: 示例库召回函数（FR-7），签名 (question, db_id) -> list。
    返回:
        NL2SQLPipeline 实例。
    """
    resolved_env = env
    if config is None:
        config = build_nl2sql_config(env=env, dotenv_paths=dotenv_paths)
        if env is None and dotenv_paths:
            # .env 已载入进程环境；后续 LLMConfig 直接读 os.environ 即可。
            resolved_env = None
    tag = model_tag_for(env=resolved_env)
    generator = SQLGenerator(
        llm=llm if llm is not None else build_llm(env=resolved_env),
        model_tag=tag,
    )
    return NL2SQLPipeline(
        config=config,
        metadata_provider=build_metadata_provider(config),
        linker=SchemaLinker(
            top_k_tables=config.top_k_tables,
            schema_context_max_tokens=config.schema_context_max_tokens,
        ),
        generator=generator,
        guardrails=GuardrailValidator(),
        semantic=semantic if semantic is not None else build_semantic_layer(config),
        executor=executor,
        cache=cache,
        few_shot_provider=few_shot_provider,
    )
