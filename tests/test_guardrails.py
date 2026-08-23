"""护栏单测：覆盖 PRD FR-5 全部拦截场景。"""

from nl2sql_skill.guardrails import GuardrailValidator


def test_select_passes_and_limit_appended():
    r = GuardrailValidator().validate("SELECT id FROM orders", allowed_tables={"orders"}, max_rows=50)
    assert r.ok and "LIMIT 50" in r.sql and r.used_tables == ["orders"]


def test_overlimit_capped():
    r = GuardrailValidator().validate("SELECT id FROM orders LIMIT 99999", max_rows=100)
    assert r.ok and "LIMIT 100" in r.sql


def test_update_rejected():
    r = GuardrailValidator().validate("UPDATE orders SET status=1")
    assert not r.ok and "只读" in r.reason


def test_multi_statement_rejected():
    r = GuardrailValidator().validate("SELECT 1; DROP TABLE t")
    assert not r.ok and "单条" in r.reason


def test_into_outfile_rejected_by_raw_pattern():
    r = GuardrailValidator().validate("SELECT * INTO OUTFILE '/tmp/x' FROM t")
    assert not r.ok and "OUTFILE" in r.reason


def test_load_file_rejected():
    r = GuardrailValidator().validate("SELECT LOAD_FILE('/etc/passwd') FROM t")
    assert not r.ok and "load_file" in r.reason.lower()


def test_permission_violation_rejected():
    r = GuardrailValidator().validate("SELECT * FROM secret_table", allowed_tables={"orders"})
    assert not r.ok and "不可见" in r.reason


def test_cte_alias_not_counted_as_table():
    sql = "WITH top AS (SELECT id FROM orders LIMIT 10) SELECT * FROM top"
    r = GuardrailValidator().validate(sql, allowed_tables={"orders"})
    assert r.ok and r.used_tables == ["orders"] and "LIMIT" in r.sql
