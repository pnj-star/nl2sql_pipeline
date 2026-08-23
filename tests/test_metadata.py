"""元数据提供者单测：TTL 快照缓存行为（PRD FR-1.5 / A1 修复）。"""

import asyncio

from nl2sql_skill.metadata import (
    InformationSchemaProvider,
    SchemaSnapshot,
    TableMeta,
)


class CountingProvider(InformationSchemaProvider):
    """覆写 _fetch_snapshot 的桩提供者：统计真实采集次数，模拟 DB 查询成本。"""

    def __init__(self, *, ttl: float) -> None:
        super().__init__({}, snapshot_ttl_seconds=ttl)
        self.fetches = 0

    async def _fetch_snapshot(self, db_id):
        self.fetches += 1
        return SchemaSnapshot.build(db_id, {"t": TableMeta(name="t")})


def test_snapshot_cached_within_ttl():
    provider = CountingProvider(ttl=300)
    s1 = asyncio.run(provider.get_snapshot("db1"))
    s2 = asyncio.run(provider.get_snapshot("db1"))
    assert s1 is s2 and provider.fetches == 1


def test_snapshot_refreshed_after_ttl():
    now = [0.0]
    provider = CountingProvider(ttl=100)
    provider._clock = lambda: now[0]
    asyncio.run(provider.get_snapshot("db1"))
    now[0] = 101  # 越过 TTL
    asyncio.run(provider.get_snapshot("db1"))
    assert provider.fetches == 2
