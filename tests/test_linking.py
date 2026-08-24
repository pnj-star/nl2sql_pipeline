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
    # 列名 name 不应通过子串命中 hostname（A2 修复）。
    # bigram 兜底可能返回低分候选，但绝不能出现"列名命中:name"的原因标签，
    # 因为那是主打分子串误报，与兜底召回是不同机制。
    users = TableMeta(
        name="users",
        comment="用户表",
        columns=[ColumnMeta(name="id"), ColumnMeta(name="name")],
    )
    snap = SchemaSnapshot.build("db2", {"users": users})
    ctx = SchemaLinker().link("hostname 分布统计", snap, SemanticLayer(version="v"))
    for cand in ctx.candidates:
        assert "列名命中:name" not in cand.reason


def test_bigram_fallback_recovers_when_primary_scoring_misses():
    """主打分为零时 bigram 兜底应召回相关表而不是直接返回空。"""
    products = TableMeta(
        name="products",
        comment="商品库存管理",
        columns=[
            ColumnMeta(name="sku", comment="库存单位编码"),
            ColumnMeta(name="stock_qty", comment="当前库存数量"),
        ],
    )
    snap = SchemaSnapshot.build("mall", {"products": products})
    # 问题不含表名/列名/同义词，但共享了"库存"关键词的字符片段。
    ctx = SchemaLinker(top_k_tables=5).link("库存周转率怎么看", snap, SemanticLayer(version="v"))
    assert ctx.candidate_names  # 不应为空
    assert any("bigram" in c.reason for c in ctx.candidates)


def test_column_comment_matching_improves_recall():
    """列注释中的中文描述应该参与打分（不仅限列名）。"""
    orders = TableMeta(
        name="orders",
        comment="订单表",
        columns=[ColumnMeta(name="create_time", comment="下单时间")],
    )
    snap = SchemaSnapshot.build("erp", {"orders": orders})
    sem = SemanticLayer(version="v")
    # "下单时间"不在列名 create_time 里也不在表注释里，但在列注释里。
    ctx = SchemaLinker().link("查一下最近的下单时间分布", snap, sem)
    assert "orders" in ctx.candidate_names
