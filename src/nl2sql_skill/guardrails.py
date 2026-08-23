"""SQL 安全护栏：基于 sqlglot AST 的只读校验（PRD FR-5）。

校验流水线：原始文本防御 → 解析（单语句）→ 语句类型白名单 →
危险节点/函数扫描 → INTO OUTFILE 拦截 → 权限复核 → LIMIT 强制。
任何一步失败都会给出中文 reason，pipeline 直接映射为 rejected_guardrail。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

# 原始文本层防御：INTO OUTFILE/DUMPFILE 属 SELECT 语法子集。
# 当前 sqlglot(30.x) 对该语法直接 ParseError，但未来版本支持解析后，
# 仅靠"顶层必须是 SELECT"的白名单会放行 —— 因此这里双保险显式拦截。
_INTO_FILE_PATTERN = re.compile(r"\binto\s+(outfile|dumpfile)\b", re.IGNORECASE)

# 禁止出现的 AST 节点类型：覆盖写操作、DDL、权限与任意命令。
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Create,
    exp.Grant,
    exp.Merge,
    exp.Command,  # SET / USE / KILL / SHOW 等无法归类为标准 AST 的语句
)


@dataclass(slots=True)
class GuardrailResult:
    """护栏校验结果。

    属性:
        ok: 是否全部通过。
        sql: 通过时为规范化后的 SQL（补齐/压低 LIMIT）；失败时为空串。
        reason: 失败原因（中文，可直接透出给调用方）。
        used_tables: SQL 引用的物理表列表（已排除 CTE 别名，小写去重）。
        used_columns: SQL 引用的列名列表（小写去重），供审计与越权列分析。
    """

    ok: bool
    sql: str = ""
    reason: str = ""
    used_tables: list[str] = field(default_factory=list)
    used_columns: list[str] = field(default_factory=list)


class GuardrailValidator:
    """无状态的护栏校验器；一个实例可被并发复用。"""

    def __init__(self, dialect: str = "mysql") -> None:
        """"参数 dialect: SQL 方言，默认 mysql（PRD 范围内固定）。"""
        self.dialect = dialect

    def validate(
        self,
        sql: str,
        *,
        allowed_tables: set[str] | frozenset[str] | None = None,
        max_rows: int = 200,
    ) -> GuardrailResult:
        """对一条 SQL 执行完整校验并返回规范化结果。

        参数:
            sql: 待校验 SQL 文本（通常来自 LLM 生成或缓存）。
            allowed_tables: 允许访问的物理表白名单（小写集合）；
                None 表示跳过表级权限复核（如测试场景）。
            max_rows: 强制 LIMIT 的上限；缺失时追加，超出时压低。
        返回:
            GuardrailResult；ok=False 时 sql 为空且带中文 reason。
        """
        text = (sql or "").strip()
        if not text:
            return GuardrailResult(ok=False, reason="SQL 为空")

        if _INTO_FILE_PATTERN.search(text):
            return GuardrailResult(ok=False, reason="禁止使用 INTO OUTFILE/DUMPFILE 导出文件")

        try:
            statements = sqlglot.parse(text, dialect=self.dialect)
        except ParseError as exc:
            return GuardrailResult(ok=False, reason=f"SQL 解析失败: {exc}")

        if len(statements) != 1 or isinstance(statements[0], exp.Block):
            return GuardrailResult(ok=False, reason="仅允许单条语句")
        expression = statements[0]

        # WITH ... SELECT 在 sqlglot 中也是 Select 节点，天然通过；
        # 其余语句类型在白名单外直接拒绝。
        if not isinstance(expression, exp.Select):
            return GuardrailResult(ok=False, reason="仅允许 SELECT/WITH 只读查询")

        for node in expression.walk():
            if isinstance(node, _FORBIDDEN_NODES):
                return GuardrailResult(
                    ok=False,
                    reason=f"检测到禁止的语句成分: {type(node).__name__}",
                )
            # LOAD_FILE 等文件访问函数：sqlglot 将未知函数解析为 Anonymous，
            # 取函数名字符串做小写比对即可稳定命中。
            if isinstance(node, exp.Anonymous):
                name = str(node.this or "").lower()
                if name in ("load_file", "loadfile"):
                    return GuardrailResult(ok=False, reason=f"禁止使用文件访问函数: {name}")

        # 未来 sqlglot 若支持解析 INTO OUTFILE，AST 层仍能拦住（纵深防御）。
        if expression.args.get("into") is not None:
            return GuardrailResult(ok=False, reason="禁止使用 INTO 子句导出文件")

        cte_names = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)}
        used_tables: list[str] = []
        seen_tables: set[str] = set()
        for table_node in expression.find_all(exp.Table):
            name = table_node.name.lower()
            if name in cte_names or not name or name in seen_tables:
                continue
            seen_tables.add(name)
            used_tables.append(name)

        used_columns = sorted({c.name.lower() for c in expression.find_all(exp.Column)})

        if allowed_tables is not None:
            outside = [t for t in used_tables if t not in allowed_tables]
            if outside:
                return GuardrailResult(
                    ok=False,
                    reason=f"引用了不可见的表: {', '.join(outside)}",
                    used_tables=used_tables,
                    used_columns=used_columns,
                )

        limit_value = self._enforce_limit(expression, max_rows=max_rows)
        normalized = expression.sql(dialect=self.dialect)
        return GuardrailResult(
            ok=True,
            sql=normalized,
            used_tables=used_tables,
            used_columns=used_columns,
        )

    def _enforce_limit(self, select: exp.Select, *, max_rows: int) -> int:
        """在外层 SELECT 上强制 LIMIT。

        规则（PRD FR-5.2）：
        - 缺失 LIMIT → 追加 max_rows；
        - 已有 LIMIT 且数值 > max_rows → 压低到 max_rows；
        - 已有 LIMIT 且 ≤ max_rows → 保持不变。

        参数:
            select: 最外层 Select AST 节点（会被原地修改）。
            max_rows: 行数上限。
        返回:
            最终生效的行数上限值。
        """
        existing = select.args.get("limit")
        effective = max_rows
        if existing is not None:
            literal = existing.expression
            try:
                current = int(literal.this)  # type: ignore[union-attr]
                effective = min(current, max_rows)
            except (AttributeError, TypeError, ValueError):
                # LIMIT 表达式不是字面量（极少见），保守压到上限。
                effective = max_rows
        select.set("limit", exp.Limit(expression=exp.Literal.number(effective)))
        return effective
