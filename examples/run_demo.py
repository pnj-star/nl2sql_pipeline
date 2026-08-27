"""nl2sql_skill 完整示例：真实 MySQL 元数据 → 语义层 → few-shot → 生成 → 护栏 → 执行。

前置：
    1) 先在 MySQL 执行 examples/sql/schema.sql（建库建表+示例数据）；
    2) 复制 examples/.env.example 为 examples/.env，填入 test-mysql 的只读账号；
    3) pip install pymysql（元数据采集与 execute=true 需要）。

用法：
    python examples/run_demo.py            # 真实 LLM（examples/.env 里配好密钥）
    python examples/run_demo.py --fake     # 无密钥也能跑通全流程（生成用预置 SQL 演示）
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent
ROOT = EXAMPLES.parent

# 本地开发模式：未安装 pip 包时也能直接 import
for sp in (ROOT / "common_core" / "src", ROOT / "nl2sql_skill" / "src"):
    if str(sp) not in sys.path:
        sys.path.insert(0, str(sp))

from nl2sql_skill import (  # noqa: E402
    NL2SQLConfig,
    build_nl2sql_config,
    build_nl2sql_pipeline,
)
from nl2sql_skill.types import ExecutorQuery, NL2SQLRequest  # noqa: E402


QUESTIONS = [
    ("8月黑木耳佣金多少", {"execute": True}),
    ("香菇的抽成是多少", {}),
    ("银耳是固定佣金还是按比例？", {}),
    ("哪个产品佣金最高", {"execute": True}),
]


# ---------------------------------------------------------------- 假 LLM ----
class FakeLLM:
    """演示用 LLM：按问题关键词返回预置 SQL，验证除生成外的完整链路。"""

    _RULES = [
        (r"佣金最高|哪个.*高", "SELECT p.name, cr.rate FROM products p JOIN commission_rules cr ON cr.product_id = p.id ORDER BY cr.rate DESC LIMIT 1"),
        (r"银耳", "SELECT cr.flat_amount FROM commission_rules cr JOIN products p ON p.id = cr.product_id WHERE p.name = '银耳'"),
        (r"香菇", "SELECT cr.rate FROM commission_rules cr JOIN products p ON p.id = cr.product_id WHERE p.name = '香菇'"),
    ]
    _DEFAULT = (
        "SELECT cr.rate FROM commission_rules cr JOIN products p ON p.id = cr.product_id "
        "WHERE p.name = '黑木耳' "
        "AND cr.effective_from <= '2026-08-31' "
        "AND (cr.effective_to IS NULL OR cr.effective_to >= '2026-08-01')"
    )

    def __init__(self) -> None:
        self.calls = 0

    async def chat_json(self, messages: list[dict], *, system_prompt: str = "") -> dict:
        self.calls += 1
        content = str(messages[-1].get("content", ""))
        m = re.search(r"# 当前问题\s*\n(.+)", content)
        question = m.group(1).strip() if m else ""
        sql = self._DEFAULT
        for pattern, candidate in self._RULES:
            if re.search(pattern, question):
                sql = candidate
                break
        return {
            "sql": sql,
            "confidence": 0.9,
            "used_tables": [],
            "assumptions": [],
            "clarify_questions": [],
        }


# ------------------------------------------------------------ 演示执行器 ----
@dataclass
class DemoResult:
    rows: list[dict] = field(default_factory=list)
    error: str | None = None
    truncated: bool = False


class DemoSqlExecutor:
    """只读演示执行器：pymysql 直连 MySQL，只放行 SELECT（pipeline 已护栏，双保险）。"""

    def __init__(self, dsn: str) -> None:
        from nl2sql_skill.config import DataSourceConfig

        self._conn_info = DataSourceConfig(db_id="demo", dsn=dsn).parsed

    async def aquery(self, request: ExecutorQuery) -> DemoResult:
        import pymysql

        sql = request.sql.strip()
        if not sql.lower().startswith("select") and ";" not in sql:
            return DemoResult(error="演示执行器只允许 SELECT")
        info = self._conn_info
        conn = pymysql.connect(
            host=str(info["host"]),
            port=int(info["port"]),
            user=str(info["user"]),
            password=str(info["password"]),
            database=str(info["database"]),
            charset="utf8mb4",
            connect_timeout=5,
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = list(cur.fetchmany((request.max_rows or 200) + 1))
            return DemoResult(
                rows=rows[: request.max_rows or 200],
                truncated=len(rows) > (request.max_rows or 200),
            )
        finally:
            conn.close()


def _resolve_config() -> NL2SQLConfig:
    """加载 examples/.env，并把语义层/示例库固定指向示例自带文件。"""
    env_file = EXAMPLES / ".env"
    config = build_nl2sql_config(dotenv_paths=[str(env_file)] if env_file.exists() else None)
    return dataclasses.replace(
        config,
        semantic_dir=str(EXAMPLES / "semantic"),
        example_store_path=str(EXAMPLES / "few_shots" / "samples.jsonl"),
    )


async def _run(use_fake_llm: bool) -> None:
    config = _resolve_config()
    demo = config.datasources.get("commerce")
    if demo is None:
        raise SystemExit("examples/.env 未配置 NL2SQL_DB_COMMERCE_DSN，请先复制 .env.example 填写")

    try:
        pipeline = build_nl2sql_pipeline(
            config=config,
            llm=FakeLLM() if use_fake_llm else None,
            executor=DemoSqlExecutor(demo.dsn),
        )
    except Exception as exc:  # noqa: BLE001 - 配置缺失/依赖未装时给出友好提示
        raise SystemExit(f"pipeline 构建失败: {exc}\n提示：真实模式需 NL2SQL_LLM_BASE_URL/MODEL；确认已 pip install pymysql")

    print("=" * 70)
    print(f"模式: {'fake LLM（预置 SQL 演示）' if use_fake_llm else '真实 LLM（examples/.env）'}")
    print(f"数据源: {demo.dsn.split('@')[-1]}")
    print("=" * 70)

    for i, (query, opts) in enumerate(QUESTIONS, start=1):
        req = NL2SQLRequest(query=query, tenant_id="t1", db_id="commerce", request_id=f"demo-{i}", **opts)
        try:
            result = await pipeline.generate(req)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[{i}] {query}\n    异常: {exc}")
            continue
        print(f"\n[{i}] 问: {query}")
        print(f"    status={result.status}  cache_hit={result.cache_hit}  message={result.message or '-'}")
        if result.sql:
            print(f"    sql: {result.sql}")
        if result.rows:
            print(f"    rows: {result.rows}")
        if result.digest:
            print(f"    digest: {result.digest}")


def main() -> None:
    parser = argparse.ArgumentParser(description="nl2sql_skill 完整示例")
    parser.add_argument("--fake", action="store_true", help="用预置 SQL 演示生成，不依赖 LLM 密钥")
    args = parser.parse_args()
    asyncio.run(_run(use_fake_llm=args.fake))


if __name__ == "__main__":
    main()