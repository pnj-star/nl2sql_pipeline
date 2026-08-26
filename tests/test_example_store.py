"""few-shot 示例库读侧测试（PRD FR-7）：加载、db/tenant 隔离、相似度过滤。"""

import json

from nl2sql_skill.example_store import ExampleRecord, ExampleStore


def _write(path, lines):
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")


def test_load_and_search_by_similarity(tmp_path):
    _write(
        tmp_path / "ex.jsonl",
        [
            {"question": "本月订单总额", "sql": "SELECT SUM(amount) FROM orders", "db_id": "erp"},
            {"question": "本月订单总额是多少", "sql": "SELECT SUM(amount) FROM orders WHERE t>=?", "db_id": "erp"},
        ],
    )
    store = ExampleStore.from_path(tmp_path / "ex.jsonl", min_similarity=0.5)
    hits = store.search("本月订单总额", "erp")
    assert len(hits) == 2
    assert {"question", "sql"} <= set(hits[0])


def test_db_isolation_excludes_other_datasources(tmp_path):
    _write(
        tmp_path / "ex.jsonl",
        [
            {"question": "本月订单总额", "sql": "SELECT 1 FROM orders", "db_id": "erp"},
            {"question": "本月工资总额", "sql": "SELECT 2 FROM payroll", "db_id": "hr"},
            {"question": "全库通用示例", "sql": "SELECT 3", "db_id": ""},
        ],
    )
    store = ExampleStore.from_path(tmp_path / "ex.jsonl", min_similarity=0.0)
    hits = store.search("本月订单总额", "erp")
    assert [h["sql"] for h in hits] == ["SELECT 1 FROM orders", "SELECT 3"]


def test_tenant_isolation(tmp_path):
    _write(
        tmp_path / "ex.jsonl",
        [
            {"question": "租户A的问题", "sql": "SELECT 1", "db_id": "erp", "tenant_id": "tenant-a"},
            {"question": "全局的问题", "sql": "SELECT 2", "db_id": "erp", "tenant_id": ""},
        ],
    )
    store = ExampleStore.from_path(tmp_path / "ex.jsonl", min_similarity=0.0)
    hits = store.search("租户A的问题", "erp", tenant_id="tenant-b")
    assert [h["sql"] for h in hits] == ["SELECT 2"]  # 只留全局示例


def test_similarity_threshold_filters_low_match(tmp_path):
    _write(
        tmp_path / "ex.jsonl",
        [{"question": "本月订单总额", "sql": "SELECT SUM(amount) FROM orders", "db_id": "erp"}],
    )
    store = ExampleStore.from_path(tmp_path / "ex.jsonl", min_similarity=0.99)
    assert store.search("完全不相关的内容", "erp") == []


def test_unverified_examples_excluded(tmp_path):
    _write(
        tmp_path / "ex.jsonl",
        [
            {"question": "本月订单总额", "sql": "SELECT 1", "db_id": "erp", "verified": False},
            {"question": "本月订单总额", "sql": "SELECT 2", "db_id": "erp", "verified": True},
        ],
    )
    store = ExampleStore.from_path(tmp_path / "ex.jsonl", min_similarity=0.0)
    assert [h["sql"] for h in store.search("本月订单总额", "erp")] == ["SELECT 2"]


def test_broken_lines_skipped(tmp_path):
    path = tmp_path / "ex.jsonl"
    path.write_text('{"question": "ok", "sql": "SELECT 1"}\nnot-json\n{"sql": "no question"}\n', encoding="utf-8")
    store = ExampleStore.from_path(path, min_similarity=0.0)
    assert len(store) == 1
    assert store._examples[0].question == "ok"


def test_missing_file_yields_empty_store(tmp_path):
    store = ExampleStore.from_path(tmp_path / "nope.jsonl")
    assert len(store) == 0 and store.search("q", "erp") == []


def test_top_k_caps_results(tmp_path):
    _write(
        tmp_path / "ex.jsonl",
        [{"question": "问题A", "sql": "SELECT 1", "db_id": "erp"},
         {"question": "问题A一样", "sql": "SELECT 2", "db_id": "erp"},
         {"question": "问题A一模一样", "sql": "SELECT 3", "db_id": "erp"}],
    )
    store = ExampleStore.from_path(tmp_path / "ex.jsonl", min_similarity=0.0, default_top_k=2)
    assert len(store.search("问题A", "erp")) == 2