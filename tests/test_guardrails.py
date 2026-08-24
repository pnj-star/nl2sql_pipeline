"""护栏单测：覆盖 PRD FR-5 拦截场景（≥30 条负样本）与正向回归。

负样本分类：
  写操作（INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE/GRANT/MERGE）
  命令语句（SET/USE/KILL/SHOW/CALL/LOCK/HANDLER）
  文件访问（INTO OUTFILE/DUMPFILE、LOAD_FILE、LOAD DATA INFILE）
  注入与多语句（分号拼接、注释拆分、UNION 写操作）
  越权访问（单表/多表/子查询/JOIN 引用不可见表）
  混淆绕过（反引号大写、注释包裹）
"""

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


# ---------------------------------------------------------------- 写操作 --

def test_insert_rejected():
    r = GuardrailValidator().validate("INSERT INTO orders (id) VALUES (1)")
    assert not r.ok and "只读" in r.reason


def test_delete_rejected():
    r = GuardrailValidator().validate("DELETE FROM orders WHERE id = 1")
    assert not r.ok and "只读" in r.reason


def test_drop_table_rejected():
    r = GuardrailValidator().validate("DROP TABLE orders")
    assert not r.ok


def test_alter_table_rejected():
    r = GuardrailValidator().validate("ALTER TABLE orders ADD COLUMN x INT")
    assert not r.ok


def test_truncate_rejected():
    r = GuardrailValidator().validate("TRUNCATE TABLE orders")
    assert not r.ok


def test_create_table_rejected():
    r = GuardrailValidator().validate("CREATE TABLE t2 AS SELECT * FROM orders")
    assert not r.ok


def test_grant_rejected():
    r = GuardrailValidator().validate("GRANT ALL ON mydb.* TO 'u'@'h'")
    assert not r.ok


def test_merge_rejected():
    # MySQL 不支持 MERGE，但 sqlglot 解析为 Merge 节点也应拦截。
    r = GuardrailValidator().validate(
        "MERGE INTO orders USING src ON orders.id = src.id WHEN MATCHED THEN UPDATE SET status=1"
    )
    assert not r.ok


# ------------------------------------------------------------- 命令语句 --

def test_set_command_rejected():
    r = GuardrailValidator().validate("SET @x = 1")
    assert not r.ok


def test_use_database_rejected():
    r = GuardrailValidator().validate("USE mysql")
    assert not r.ok


def test_show_tables_rejected():
    r = GuardrailValidator().validate("SHOW TABLES")
    assert not r.ok


def test_kill_connection_rejected():
    r = GuardrailValidator().validate("KILL 12345")
    assert not r.ok


def test_call_procedure_rejected():
    r = GuardrailValidator().validate("CALL dangerous_procedure()")
    assert not r.ok


def test_lock_tables_rejected():
    r = GuardrailValidator().validate("LOCK TABLES orders READ")
    assert not r.ok


# ------------------------------------------------------------ 文件访问 ---

def test_into_dumpfile_rejected():
    r = GuardrailValidator().validate("SELECT * INTO DUMPFILE '/tmp/x' FROM orders")
    assert not r.ok


def test_load_data_infile_rejected():
    r = GuardrailValidator().validate(
        "LOAD DATA INFILE '/tmp/data.csv' INTO TABLE orders"
    )
    assert not r.ok


# ------------------------------------------------------- 注入与多语句 ----

def test_semicolon_then_insert_rejected():
    r = GuardrailValidator().validate("SELECT 1; INSERT INTO logs VALUES ('x')")
    assert not r.ok and "单条" in r.reason


def test_comment_split_statement_rejected():
    # 注释不能隐藏第二条语句：sqlglot.parse 会把两段都解析出来。
    r = GuardrailValidator().validate("SELECT 1; /* noop */ DROP TABLE users")
    assert not r.ok


def test_union_with_write_attempted():
    # UNION 里嵌写操作的完整语法在 MySQL 中不合法，
    # 但 sqlglot 可能解析为非 Select 或报 ParseError，两种路径都必须拒绝。
    r = GuardrailValidator().validate(
        "SELECT id FROM orders UNION SELECT id FROM orders; DROP TABLE orders"
    )
    assert not r.ok


def test_trailing_semicolon_only_is_ok():
    """末尾单个分号是合法的 MySQL 写法，不应被误拦。"""
    r = GuardrailValidator().validate("SELECT id FROM orders;")
    assert r.ok


# ------------------------------------------------------------ 越权访问 ---

def test_join_referencing_forbidden_table_rejected():
    r = GuardrailValidator().validate(
        "SELECT o.id FROM orders o JOIN audit_log a ON o.id = a.order_id",
        allowed_tables={"orders"},
    )
    assert not r.ok and "audit_log" in r.reason


def test_subquery_referencing_forbidden_table_rejected():
    r = GuardrailValidator().validate(
        "SELECT id FROM orders WHERE shop_id IN (SELECT id FROM internal_shops)",
        allowed_tables={"orders"},
    )
    assert not r.ok and "internal_shops" in r.reason


def test_multiple_tables_one_outside_allowlist_rejected():
    r = GuardrailValidator().validate(
        "SELECT * FROM orders CROSS JOIN payroll",
        allowed_tables={"orders"},
    )
    assert not r.ok and "payroll" in r.reason


def test_case_insensitive_table_bypass_rejected():
    # 大写表名不应绕过小写白名单比对。
    r = GuardrailValidator().validate(
        "SELECT * FROM SECRET_TABLE", allowed_tables={"orders"}
    )
    assert not r.ok


def test_backtick_quoted_forbidden_table_rejected():
    # 反引号引用的表名同样必须走白名单校验。
    r = GuardrailValidator().validate(
        "SELECT * FROM `secret_data`", allowed_tables={"orders"}
    )
    assert not r.ok


# --------------------------------------------------------- 边界与空值 ---

def test_empty_sql_rejected():
    r = GuardrailValidator().validate("")
    assert not r.ok


def test_whitespace_only_sql_rejected():
    r = GuardrailValidator().validate("   \n\t  ")
    assert not r.ok


def test_parse_error_rejected():
    """语法错误必须被拒绝，无论走 ParseError 还是白名单路径。"""
    v = GuardrailValidator()
    assert not v.validate("SELEC * FORM orders").ok
    assert not v.validate("SELECT * FROM").ok
    # 未闭合括号应触发真正的 ParseError 路径。
    r = v.validate("SELECT COUNT(* FROM orders")
    assert not r.ok


# ----------------------------------------------------- 正向回归（防误拦）--

def test_nested_subquery_within_allowed_tables_passes():
    r = GuardrailValidator().validate(
        "SELECT id FROM orders WHERE shop_id IN (SELECT id FROM shops)",
        allowed_tables={"orders", "shops"},
    )
    assert r.ok and set(r.used_tables) == {"orders", "shops"}


def test_inner_join_between_allowed_tables_passes():
    r = GuardrailValidator().validate(
        "SELECT o.id, s.name FROM orders o INNER JOIN shops s ON o.shop_id = s.id",
        allowed_tables={"orders", "shops"},
    )
    assert r.ok


def test_aggregation_with_group_by_passes():
    r = GuardrailValidator().validate(
        "SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status",
        allowed_tables={"orders"},
    )
    assert r.ok and "LIMIT" in r.sql


def test_comment_wrapped_select_passes():
    """注释包裹合法 SELECT 不应被误拦。"""
    r = GuardrailValidator().validate(
        "/* 查询订单 */ SELECT id FROM orders", allowed_tables={"orders"}
    )
    assert r.ok


def test_backtick_quoted_allowed_table_passes():
    r = GuardrailValidator().validate(
        "SELECT * FROM `orders`", allowed_tables={"orders"}
    )
    assert r.ok and "LIMIT" in r.sql
