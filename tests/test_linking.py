"""召回单测：同义词命中、外键扩展、预算截断（PRD FR-3）。"""

from nl2sql_skill.linking import SchemaLinker
from nl2sql_skill.metadata import ColumnMeta, SchemaSnapshot, TableMeta
from nl2sql_skill.semantic import SemanticLayer, SemanticTable


def _snapshot() -> SchemaSnapshot:
    orders = TableMeta(
        name="orders",
        comment="订单表",
        columns=[ColumnMeta(name="id"), ColumnMeta(name="shop_id")],
        foreign_keys={"shop_id": ("shops", "id")},
    )
    shops = TableMeta(name="shops", comment="门店表", columns=[ColumnMeta(name="id")])
    users = TableMeta(name="users", comment="用户表", columns=[ColumnMeta(name="id")])
    return SchemaSnapshot.build("db1", {"orders": orders, "shops": shops, "users": users})


def _semantic() -> SemanticLayer:
    return SemanticLayer(
        version="t1",
        tables={
            "orders": SemanticTable(synonyms=["订单量", "GMV"]),
            "shops": SemanticTable(requires_time_filter=True),
        },
    )


def test_synonym_hit_ranks_table():
    ctx = SchemaLinker().link("上月订单量多少", _snapshot(), _semantic())
    assert "orders" in ctx.candidate_names


def test_fk_expansion_pulls_neighbor():
    ctx = SchemaLinker().link("查一下 orders 的数据分布", _snapshot(), _semantic())
    assert "orders" in ctx.candidate_names and "shops" in ctx.candidate_names


def test_budget_truncation_records_dropped():
    linker = SchemaLinker(top_k_tables=10, schema_context_max_tokens=1)
    ctx = linker.link("orders 数据", _snapshot(), _semantic())
    assert ctx.truncated_tables  # 预算为 1 token 时全部被裁剪并记录


def test_no_match_returns_empty():
    ctx = SchemaLinker().link("天气怎么样", _snapshot(), _semantic())
    assert ctx.candidates == []


def test_word_boundary_prevents_substring_false_positive():
    # 列名 name 不应命中 hostname（子串假阳性，A2 修复）。
    users = TableMeta(
        name="users",
        comment="用户表",
        columns=[ColumnMeta(name="id"), ColumnMeta(name="name")],
    )
    snap = SchemaSnapshot.build("db2", {"users": users})
    ctx = SchemaLinker().link("hostname 分布统计", snap, SemanticLayer(version="v"))
    assert ctx.candidates == []
