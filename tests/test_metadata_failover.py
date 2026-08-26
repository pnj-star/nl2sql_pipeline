"""元数据可用性测试：stale 降级（FR 可用性要求）与 single-flight 并发去抖。"""

import asyncio

import pytest

from nl2sql_skill.config import DataSourceConfig
from nl2sql_skill.metadata import (
    InformationSchemaProvider,
    MetadataError,
    SchemaSnapshot,
    TableMeta,
)

DS = {
    "erp": DataSourceConfig(db_id="erp", dsn="mysql://ro:pw@127.0.0.1:3306/erp"),
}


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class FlakyProvider(InformationSchemaProvider):
    """按次数编排采集结果：可指定第 N 次采集抛 MetadataError。"""

    def __init__(self, clock: FakeClock, *, ttl: float = 10.0) -> None:
        super().__init__(DS, snapshot_ttl_seconds=ttl, clock=clock)
        self.fetch_count = 0
        self.fail_on: set[int] = set()

    async def _fetch_snapshot(self, db_id: str) -> SchemaSnapshot:
        self.fetch_count += 1
        if self.fetch_count in self.fail_on:
            raise MetadataError("metadata db unavailable")
        return SchemaSnapshot.build(db_id, {"t": TableMeta(name="t", comment=f"v{self.fetch_count}")})


def test_first_failure_raises_without_history():
    clock = FakeClock()
    provider = FlakyProvider(clock)
    provider.fail_on = {1}
    with pytest.raises(MetadataError):
        asyncio.run(provider.get_snapshot("erp"))
    assert not provider.is_stale("erp")
    assert provider.fetch_count == 1


def test_refresh_failure_falls_back_to_stale_snapshot_then_recovers():
    clock = FakeClock()
    provider = FlakyProvider(clock)
    provider.fail_on = {2}

    first = asyncio.run(provider.get_snapshot("erp"))  # fetch #1 成功
    assert not provider.is_stale("erp")

    clock.now = 111.0  # 越过 TTL(10)
    stale = asyncio.run(provider.get_snapshot("erp"))  # fetch #2 失败 → 降级
    assert stale is first
    assert provider.is_stale("erp")

    recovered = asyncio.run(provider.get_snapshot("erp"))  # fetch #3 成功 → 恢复
    assert recovered is not first
    assert not provider.is_stale("erp")


def test_stale_fallback_does_not_extend_ttl():
    clock = FakeClock()
    provider = FlakyProvider(clock)
    provider.fail_on = {2}
    asyncio.run(provider.get_snapshot("erp"))
    clock.now = 111.0
    asyncio.run(provider.get_snapshot("erp"))  # 降级
    assert provider.fetch_count == 2
    # 降级快照的到期时间不延长：同一个已过期的时间点会立即重试。
    asyncio.run(provider.get_snapshot("erp"))  # fetch #3 立即重试并成功
    assert provider.fetch_count == 3


class SlowProvider(InformationSchemaProvider):
    """带真实异步延迟的桩提供者，用于验证并发去抖。"""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(DS, snapshot_ttl_seconds=10.0, clock=clock)
        self.fetch_count = 0

    async def _fetch_snapshot(self, db_id: str) -> SchemaSnapshot:
        self.fetch_count += 1
        await asyncio.sleep(0.01)
        return SchemaSnapshot.build(db_id, {"t": TableMeta(name="t")})


def test_concurrent_refresh_single_flight():
    clock = FakeClock()
    provider = SlowProvider(clock)

    async def main():
        return await asyncio.gather(*[provider.get_snapshot("erp") for _ in range(5)])

    snapshots = asyncio.run(main())
    assert provider.fetch_count == 1  # 5 个并发请求只触发一次采集
    assert len({id(s) for s in snapshots}) == 1  # 全部拿到同一快照