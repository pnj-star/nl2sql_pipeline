"""生成器测试：MySQL 5.x 语法子集提示（PRD FR-4.3）与版本归一化。"""

from nl2sql_skill.generator import SQLGenerator


def _prompt(server_version: str) -> str:
    return SQLGenerator._build_user_prompt(
        question="本月订单总额",
        schema_blocks="orders(id, amount)",
        history_text="",
        few_shots=[],
        extra_clarifications=[],
        server_version=server_version,
    )


def test_mysql57_prompt_includes_syntax_constraints():
    prompt = _prompt("5.7")
    assert "语法子集约束" in prompt
    assert "窗口函数" in prompt and "WITH/CTE" in prompt


def test_mysql571x_prompt_includes_syntax_constraints():
    prompt = _prompt("5.7.44")
    assert "语法子集约束" in prompt


def test_mysql80_prompt_has_no_constraint_block():
    prompt = _prompt("8.0")
    assert "语法子集约束" not in prompt
    assert "MySQL 8.0" in prompt


def test_unknown_version_defaults_to_80():
    prompt = _prompt("mariadb-10.6")
    assert "语法子集约束" not in prompt


def test_syntax_constraint_part_returns_none_for_modern():
    assert SQLGenerator._syntax_constraint_part("8.0.36") is None
    assert SQLGenerator._syntax_constraint_part("5.7") is not None
    assert SQLGenerator._syntax_constraint_part("") is None