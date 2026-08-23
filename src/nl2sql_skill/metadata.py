"""元数据层：表/列结构描述、schema fingerprint 与快照提供者。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from typing import Any, Protocol

from .config import DataSourceConfig


class MetadataError(RuntimeError):
    """元数据采集/加载失败时抛出；pipeline 捕获后映射为 status=error。"""


@dataclass(slots=True)
class ColumnMeta:
    """单个列的结构描述。

    属性:
        name: 列名（原样保留大小写）。
        data_type: MySQL 类型名，如 varchar(64) / bigint / datetime。
        comment: 列 COMMENT（information_schema 采集），可为空。
        is_primary_key: 是否主键列。
    """

    name: str
    data_type: str = ""
    comment: str = ""
    is_primary_key: bool = False


@dataclass(slots=True)
class TableMeta:
    """单张表的结构描述。

    属性:
        name: 表名。
        comment: 表 COMMENT，参与召回打分。
        columns: ColumnMeta 列表。
        foreign_keys: 字典 {本表列名: (引用表名, 引用列名)}，
            用于 schema linking 的一跳外键扩展。
    """

    name: str
    comment: str = ""
    columns: list[ColumnMeta] = field(default_factory=list)
    foreign_keys: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def primary_keys(self) -> list[str]:
        """返回主键列名列表。"""
        return [c.name for c in self.columns if c.is_primary_key]


@dataclass(frozen=True)
class SchemaSnapshot:
    """一个数据源的元数据快照（不可变）。

    属性:
        db_id: 所属数据源 ID。
        tables: 表名（小写）→ TableMeta。
        fingerprint: 对全部元数据内容计算的 sha256；
            元数据变更 → fingerprint 变化 → SQL 缓存 key 失效。
    """

    db_id: str
    tables: dict[str, TableMeta]
    fingerprint: str

    @classmethod
    def build(cls, db_id: str, tables: dict[str, TableMeta]) -> "SchemaSnapshot":
        """从表字典构造快照并自动计算 fingerprint。

        参数:
            db_id: 数据源 ID。
            tables: 表名字典（键会统一转小写）。
        返回:
            SchemaSnapshot 实例。
        """
        normalized = {name.lower(): meta for name, meta in tables.items()}
        canonical = json.dumps(
            [
                {
                    "name": t.name,
                    "comment": t.comment,
                    "columns": [
                        [c.name, c.data_type, c.comment, c.is_primary_key]
                        for c in t.columns
                    ],
                    "foreign_keys": sorted(
                        (col, ref[0], ref[1]) for col, ref in t.foreign_keys.items()
                    ),
                }
                for _, t in sorted(normalized.items())
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        return cls(
            db_id=db_id,
            tables=normalized,
            fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


class MetadataProvider(Protocol):
    """元数据提供者协议：按 db_id 返回快照，实现方可对接 DB 或静态配置。"""

    async def get_snapshot(self, db_id: str) -> SchemaSnapshot: ...


class StaticSchemaProvider:
    """测试/演示用的静态快照提供者：直接持有预构造的快照字典。

    参数:
        snapshots: db_id → SchemaSnapshot。
    """

    def __init__(self, snapshots: dict[str, SchemaSnapshot]) -> None:
        self._snapshots = snapshots

    async def get_snapshot(self, db_id: str) -> SchemaSnapshot:
        """返回预注册的快照；db_id 未注册时抛 MetadataError。"""
        if db_id not in self._snapshots:
            raise MetadataError(f"数据源未注册: {db_id}")
        return self._snapshots[db_id]


class InformationSchemaProvider:
    """从 MySQL information_schema 采集元数据的提供者，带进程内 TTL 快照缓存。

    设计要点（PRD FR-1.5）：
    - 快照缓存命中时完全不触达 MySQL，保证高频调用下的低延迟与低压力；
    - TTL 过期后下一次调用自动重新采集；
    - 连接为懒加载：仅在真正采集时才导入 pymysql 并建连，
      避免在无 DB 的环境（如 CI 单测）中引入硬依赖。
    """

    def __init__(
        self,
        datasources: dict[str, DataSourceConfig],
        *,
        snapshot_ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化提供者。

        参数:
            datasources: db_id → DataSourceConfig 注册表（来自 NL2SQLConfig）。
            snapshot_ttl_seconds: 快照缓存有效期（秒）；0 表示禁用缓存、每次直采。
            clock: 单调时钟函数，测试时可注入假时钟控制过期行为。
        """
        self._datasources = datasources
        self._ttl = snapshot_ttl_seconds
        self._clock = clock
        # 缓存结构: db_id → (到期时间戳, SchemaSnapshot)；无锁设计依赖事件循环单线程语义。
        self._cache: dict[str, tuple[float, SchemaSnapshot]] = {}

    async def get_snapshot(self, db_id: str) -> SchemaSnapshot:
        """返回指定数据源的快照；优先命中进程内 TTL 缓存。

        参数:
            db_id: 目标数据源 ID。
        返回:
            SchemaSnapshot（可能来自缓存）。
        异常:
            MetadataError: 数据源未注册或底层采集失败。
        """
        now = self._clock()
        cached = self._cache.get(db_id)
        if cached is not None and now < cached[0]:
            return cached[1]
        snapshot = await self._fetch_snapshot(db_id)
        if self._ttl > 0:
            self._cache[db_id] = (now + self._ttl, snapshot)
        return snapshot

    async def _fetch_snapshot(self, db_id: str) -> SchemaSnapshot:
        """真正执行元数据采集；子类/测试可覆写此方法替换数据来源。

        参数:
            db_id: 目标数据源 ID。
        返回:
            新鲜采集的 SchemaSnapshot。
        """
        return await self._collect_from_information_schema(db_id)

    async def _collect_from_information_schema(self, db_id: str) -> SchemaSnapshot:
        """连接目标 MySQL 并从 information_schema 采集表/列/外键信息。

        参数:
            db_id: 目标数据源 ID。
        返回:
            SchemaSnapshot。
        异常:
            MetadataError: db_id 未注册、pymysql 未安装或查询失败。
        """
        ds = self._datasources.get(db_id)
        if ds is None:
            raise MetadataError(f"数据源未注册: {db_id}")
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - 环境缺依赖
            raise MetadataError("需要安装 pymysql: pip install 'nl2sql-skill[sql]'") from exc

        conn_info = ds.parsed
        try:
            conn = pymysql.connect(
                host=str(conn_info["host"]),
                port=int(conn_info["port"]),  # type: ignore[arg-type]
                user=str(conn_info["user"]),
                password=str(conn_info["password"]),
                database=str(conn_info["database"]),
                charset="utf8mb4",
                connect_timeout=5,
                cursorclass=pymysql.cursors.DictCursor,
            )
        except Exception as exc:
            raise MetadataError(f"{db_id} 连接失败: {exc}") from exc

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT TABLE_NAME, TABLE_COMMENT
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                    """,
                    (conn_info["database"],),
                )
                table_rows: list[dict[str, Any]] = list(cur.fetchall())

                cur.execute(
                    """
                    SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT,
                           COLUMN_KEY
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                    ORDER BY TABLE_NAME, ORDINAL_POSITION
                    """,
                    (conn_info["database"],),
                )
                column_rows: list[dict[str, Any]] = list(cur.fetchall())

                cur.execute(
                    """
                    SELECT TABLE_NAME, COLUMN_NAME,
                           REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                    FROM information_schema.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL
                    """,
                    (conn_info["database"],),
                )
                fk_rows: list[dict[str, Any]] = list(cur.fetchall())
        except Exception as exc:
            raise MetadataError(f"{db_id} 元数据查询失败: {exc}") from exc
        finally:
            conn.close()

        tables: dict[str, TableMeta] = {}
        for row in table_rows:
            tables[str(row["TABLE_NAME"]).lower()] = TableMeta(
                name=str(row["TABLE_NAME"]),
                comment=str(row["TABLE_COMMENT"] or ""),
            )
        for row in column_rows:
            tbl = tables.get(str(row["TABLE_NAME"]).lower())
            if tbl is None:
                continue
            tbl.columns.append(
                ColumnMeta(
                    name=str(row["COLUMN_NAME"]),
                    data_type=str(row["COLUMN_TYPE"]),
                    comment=str(row["COLUMN_COMMENT"] or ""),
                    is_primary_key=row["COLUMN_KEY"] == "PRI",
                )
            )
        for row in fk_rows:
            tbl = tables.get(str(row["TABLE_NAME"]).lower())
            if tbl is None:
                continue
            tbl.foreign_keys[str(row["COLUMN_NAME"])] = (
                str(row["REFERENCED_TABLE_NAME"]).lower(),
                str(row["REFERENCED_COLUMN_NAME"]),
            )
        return SchemaSnapshot.build(db_id=db_id, tables=tables)
