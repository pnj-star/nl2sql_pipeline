"""运行时配置：数据源注册表与 NL2SQL 行为开关，全部来自环境变量。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from common_core.config import ConfigError, env_bool, env_float, env_int, env_str

# 匹配形如 NL2SQL_DB_<ID>_DSN 的环境变量名，捕获中间的 <ID> 作为 db_id。
# <ID> 仅允许大写字母/数字/下划线，避免特殊字符污染缓存 key 与日志。
_DSN_PATTERN = re.compile(r"^NL2SQL_DB_([A-Z0-9_]+)_DSN$")


@dataclass(frozen=True)
class DataSourceConfig:
    """单个 MySQL 数据源的连接与权限配置。

    属性:
        db_id: 数据源唯一标识（环境变量里的 <ID> 段）。
        dsn: 连接串 mysql://user:pass@host:port/db；必须使用只读账号。
        server_version: 服务器大版本声明（"8.0"/"5.7"），用于方言语法子集选择。
        visible_tables: 可见表白名单（小写集合）；为空集合表示全部表可见。
    """

    db_id: str
    dsn: str
    server_version: str = "8.0"
    visible_tables: frozenset[str] = field(default_factory=frozenset)

    @property
    def parsed(self) -> dict[str, object]:
        """把 DSN 解析为连接参数字典。

        返回:
            含 host/port/user/password/database 五个键的字典；
            DSN 格式非法时抛出 ConfigError，fail-fast 避免带病运行。
        """
        try:
            u = urlparse(self.dsn)
        except ValueError as exc:
            raise ConfigError(f"{self.db_id} DSN 解析失败: {exc}") from exc
        if u.scheme not in ("mysql", "mysql2") or not u.hostname or not (u.path or "").strip("/"):
            raise ConfigError(
                f"{self.db_id} DSN 格式非法，期望 mysql://user:pass@host:port/db"
            )
        return {
            "host": u.hostname,
            "port": u.port or 3306,
            "user": unquote(u.username or ""),
            "password": unquote(u.password or ""),
            "database": u.path.strip("/"),
        }


@dataclass(frozen=True)
class NL2SQLConfig:
    """pipeline 全部行为开关与预算（对应 PRD §6 环境变量表）。

    属性:
        datasources: db_id → DataSourceConfig 注册表。
        semantic_dir: 语义层配置目录；空串表示未启用语义层。
        clarify_threshold: 默认置信阈值，低于它触发 need_clarification。
        max_rows: 执行侧默认行数上限（执行超时由执行器自身的连接/读超时管理）。
        history_max_tokens: 多轮 history 的 token 预算，超限截断早期轮次。
        schema_context_max_tokens: schema 召回上下文的 token 预算。
        top_k_tables: 召回候选表数量上限。
        example_min_similarity: few-shot 示例注入的最小相似度阈值。
        sql_cache_enabled / sql_cache_ttl_seconds / cache_key_include_history:
            SQL 缓存开关、TTL、history 是否参与缓存 key。
        sql_cache_scope: Redis 缓存命名空间。
    """

    datasources: dict[str, DataSourceConfig] = field(default_factory=dict)
    semantic_dir: str = ""
    clarify_threshold: float = 0.75
    max_rows: int = 200
    history_max_tokens: int = 2000
    schema_context_max_tokens: int = 4000
    top_k_tables: int = 8
    example_min_similarity: float = 0.75
    example_store_path: str = ""               # few-shot 示例库 JSONL 路径；空=不启用
    executor_enabled: bool = False             # execute=true 时启用内置只读执行器
    cost_guard_enabled: bool = False           # EXPLAIN 成本闸门开关（PRD FR-5.3）
    cost_threshold_rows: int = 5_000_000       # 预估扫描行数上限
    explain_fail_policy: str = "deny"          # EXPLAIN 失败策略: deny=拒绝, allow=放行
    sql_cache_enabled: bool = True
    sql_cache_ttl_seconds: int = 86400
    # 默认关闭：全量 history 参与 key 会让多轮会话每追加一轮就 miss，
    # 命中率损失大于收益；确需严格区分多轮前缀时再显式打开（PRD C4 决议）。
    cache_key_include_history: bool = False
    sql_cache_scope: str = "nl2sql_sql_cache"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "NL2SQLConfig":
        """从环境变量（或注入的 env 字典）构造配置。

        参数:
            env: 可选配置字典；None 时读取 os.environ。测试时注入字典即可。
        返回:
            填充完成的 NL2SQLConfig；数据源解析失败会抛 ConfigError。
        """
        import os

        source = env if env is not None else os.environ
        datasources: dict[str, DataSourceConfig] = {}
        for key, value in source.items():
            m = _DSN_PATTERN.match(key)
            if not m:
                continue
            db_id = m.group(1).lower()
            visible = {
                item.strip().lower()
                for item in env_str(f"NL2SQL_DB_{m.group(1)}_VISIBLE_TABLES", default="", env=env).split(",")
                if item.strip()
            }
            datasources[db_id] = DataSourceConfig(
                db_id=db_id,
                dsn=value,
                server_version=env_str(
                    f"NL2SQL_DB_{m.group(1)}_SERVER_VERSION",
                    default="8.0",
                    env=env,
                ),
                visible_tables=frozenset(visible),
            )
        return cls(
            datasources=datasources,
            executor_enabled=env_bool("NL2SQL_EXECUTOR_ENABLED", default=False, env=env),
            semantic_dir=env_str("NL2SQL_SEMANTIC_DIR", default="", env=env),
            clarify_threshold=env_float("NL2SQL_CLARIFY_THRESHOLD", default=0.75, env=env),
            max_rows=env_int("NL2SQL_MAX_ROWS", default=200, env=env),
            history_max_tokens=env_int("NL2SQL_HISTORY_MAX_TOKENS", default=2000, env=env),
            schema_context_max_tokens=env_int(
                "NL2SQL_SCHEMA_CONTEXT_MAX_TOKENS", default=4000, env=env
            ),
            top_k_tables=env_int("NL2SQL_TOP_K_TABLES", default=8, env=env),
            example_min_similarity=env_float(
                "NL2SQL_EXAMPLE_MIN_SIMILARITY", default=0.75, env=env
            ),
            example_store_path=env_str("NL2SQL_EXAMPLE_STORE_PATH", default="", env=env),
            cost_guard_enabled=env_bool("NL2SQL_COST_GUARD_ENABLED", default=False, env=env),
            cost_threshold_rows=env_int("NL2SQL_COST_THRESHOLD_ROWS", default=5_000_000, env=env),
            explain_fail_policy=env_str("NL2SQL_EXPLAIN_FAIL_POLICY", default="deny", env=env),
            sql_cache_enabled=env_bool("NL2SQL_SQL_CACHE_ENABLED", default=True, env=env),
            sql_cache_ttl_seconds=env_int(
                "NL2SQL_SQL_CACHE_TTL_SECONDS", default=86400, env=env
            ),
            cache_key_include_history=env_bool(
                "NL2SQL_CACHE_KEY_INCLUDE_HISTORY", default=False, env=env
            ),
        )
