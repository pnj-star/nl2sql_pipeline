# nl2sql_skill

基于 `common_core` 组件的 MySQL 自然语言转 SQL 能力层。它不包含 LangGraph 编排，也不做回答生成与话术包装；只负责把自然语言问题转换为经过 sqlglot AST 安全校验的只读 SQL，可选委托执行器运行并返回结果集。

本文件面向想复用 / 扩展 / 部署该 skill 的开发者。如果你只是要调用它（agent / MCP 调用方），看 [SKILL.md](SKILL.md) 的调用契约即可。

## 能力

- 多数据源注册与 information_schema 元数据采集（TTL 快照缓存，非阻塞线程池执行）。
- Schema Linking：词法匹配 + 列注释 + 语义层描述 + 字符 bigram 兜底召回；外键一跳扩展支撑 JOIN 推理。
- LLM 生成结构化 JSON（sql / confidence / used_tables / assumptions / clarify_questions）。
- sqlglot AST 安全护栏：SELECT/WITH 白名单、写操作/DDL/权限/文件导出拦截、强制 LIMIT、单语句校验。
- SQL 缓存：query + db_id + semantic_version + schema_fingerprint + model_tag 联合 key，命中后仍重跑护栏。
- 确定性槽位校验（时间敏感表缺时间范围时触发澄清）。
- 多轮上下文预算截断（history 按 token 预算保留近期轮次）。
- 可选执行编排（execute=true 委托 structured_query_skill）。

## 文件布局

- `src/nl2sql_skill/builder.py`: 从环境变量构建完整 pipeline（LLM、元数据提供者、缓存等）。
- `src/nl2sql_skill/pipeline.py`: `NL2SQLPipeline` 总编排，串联元数据 → 召回 → 生成 → 护栏 → 执行。
- `src/nl2sql_skill/metadata.py`: information_schema 采集 + SchemaSnapshot fingerprint + TTL 缓存。
- `src/nl2sql_skill/linking.py`: Schema Linker（词法打分、bigram 兜底、token 预算裁剪）。
- `src/nl2sql_skill/semantic.py`: 语义层加载（同义词、描述、时间过滤声明），JSON/YAML 双格式。
- `src/nl2sql_skill/generator.py`: prompt 组装 + OpenAI 兼容 LLM 调用 + JSON 解析修复重试。
- `src/nl2sql_skill/guardrails.py`: sqlglot AST 校验器（32 条负样本测试覆盖）。
- `src/nl2sql_skill/config.py`: 数据源注册表与行为开关的环境变量装载。
- `src/nl2sql_skill/types.py`: NL2SQLRequest / NL2SQLResult dataclass 与状态码枚举。
- `tests/`: 63 个单元测试（含 32 条护栏负样本 + 9 条正向回归）。

## 安装

`nl2sql_skill` 依赖 `common-core[llm,cache]` 和 `sqlglot`；启用 MySQL 元数据采集时需要 `pymysql`。本地开发：

```powershell
cd D:\my_project\Skill\nl2sql_skill
pip install -e ../common_core --no-deps
pip install -e ".[sql,semantic,test]"
```

## 使用示例

```python
import asyncio
from nl2sql_skill.builder import build_nl2sql_pipeline
from nl2sql_skill.types import NL2SQLRequest

pipeline = build_nl2sql_pipeline()  # 读 .env / 环境变量装配

async def main():
    result = await pipeline.generate(
        NL2SQLRequest(
            query="上个月华东区销售额 TOP10 的门店",
            tenant_id="t1",
            db_id="erp",
            request_id="req-1",
        )
    )
    print(result.status, result.sql)

asyncio.run(main())
```

带多轮上下文和执行：

```python
result = await pipeline.generate(
    NL2SQLRequest(
        query="只看杭州的",
        tenant_id="t1",
        db_id="erp",
        request_id="req-2",
        history=[
            {"question": "上个月各区域销量", "sql": "SELECT region, SUM(qty) FROM ..."},
        ],
        execute=True,
    )
)
print(result.status, result.rows, result.columns)
```

## 配置

复制 `.env.example` 为 `.env` 后按环境填写。关键变量速查：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NL2SQL_LLM_BASE_URL` | — | OpenAI 兼容接口地址 |
| `NL2SQL_LLM_API_KEY` | — | 接口密钥 |
| `NL2SQL_LLM_MODEL` | — | 模型名 |
| `NL2SQL_DB_<ID>_DSN` | — | MySQL 只读账号连接串 |
| `NL2SQL_DB_<ID>_VISIBLE_TABLES` | 空=全部 | 表白名单 |
| `NL2SQL_SEMANTIC_DIR` | 空=不启用 | 语义层 YAML/JSON 目录 |
| `NL2SQL_CLARIFY_THRESHOLD` | 0.75 | 置信阈值 |
| `NL2SQL_MAX_ROWS` | 200 | 行数上限 |
| `NL2SQL_SQL_CACHE_ENABLED` | true | SQL 缓存开关 |
| `NL2SQL_SQL_CACHE_TTL_SECONDS` | 86400 | 缓存 TTL |

完整说明见 [.env.example](.env.example) 内注释。

## 测试

```powershell
python -m pytest nl2sql_skill/tests -v
```

护栏负样本覆盖：写操作（INSERT/DELETE/DROP 等）、命令注入（SET/USE/KILL）、文件访问（INTO OUTFILE/LOAD_FILE）、越权访问（子查询/JOIN 引用不可见表）、混淆绕过（反引号/大写表名）和边界空值，共 32 条拦截场景加 9 条正向回归。

## 边界

- 不包含 LangGraph：编排由上层 agent 或 instances 完成。
- 不做回答文本生成与话术润色：调用方拿到 rows/sql 后自行组装回复。
- 非 MySQL 方言暂不支持（PostgreSQL / ClickHouse 列入后续 Roadmap）。
- 权限系统由外部实现；本工具消费 visible_tables 做表级过滤，列级权限待后续版本补充。
