# nl2sql_skill

基于 `common_core` 的 MySQL 自然语言转 SQL 能力层。它不包含 LangGraph 编排，也不做回答生成；只负责把自然语言问题转换为经过 sqlglot AST 安全校验的只读 SQL，并可选执行返回结果集。

调用方（agent / MCP client）只看 [SKILL.md](SKILL.md) 的调用契约；部署与二次开发操作见下文。

## 核心能力

- 多数据源注册与 information_schema 元数据采集（TTL 快照缓存、stale 降级、single-flight 去抖）
- Schema Linking：词法 + 列注释 + 语义层 + bigram 召回，外键一跳扩展支撑 JOIN 推理
- LLM 生成结构化 JSON：sql / confidence / used_tables / assumptions / clarify_questions
- sqlglot AST 安全护栏：只读白名单、写操作/DDL/文件导出拦截、强制 LIMIT、单语句校验
- SQL 缓存：query + db_id + semantic_version + schema_fingerprint + model_tag 联合 key
- 可选执行编排：委托外部执行器返回结果集与数值摘要 digest
- EXPLAIN 成本闸门、few-shot 示例库、MySQL 5.x 语法提示

## 处理流程

```mermaid
flowchart TD
    REQ[用户问题 query / tenant_id / db_id] --> REG{db_id 已注册?}
    REG -- 否 --> NO[status = no_schema]
    REG -- 是 --> MD[元数据快照 TTL 缓存 + stale 降级]
    MD --> LK[Schema Linking 词法 + 注释 + 语义 + bigram]
    LK -- 无候选表 --> NO
    LK --> CACHE{SQL 缓存命中?}
    CACHE -- 命中 --> GUARD
    CACHE -- 未命中 --> EX[example_store few-shot 注入]
    EX --> GEN[generator LLM 生成 JSON]
    GEN --> CLAR{置信不足 / 缺槽位?}
    CLAR -- 是 --> NC[status = need_clarification + clarify_questions]
    CLAR -- 否 --> GUARD[guardrails sqlglot AST 护栏 + 补 LIMIT]
    GUARD -- 拦截 --> RG[status = rejected_guardrail]
    GUARD -- 通过 --> WRITE[写入 SQL 缓存]
    WRITE --> EXEC{execute = true?}
    EXEC -- 否 --> G2[status = generated / generated_cache]
    EXEC -- 是 --> CG[cost_guard EXPLAIN 成本闸门]
    CG -- 超阈值 --> RG
    CG -- 通过 --> Q[executor 执行器]
    Q -- 异常 --> ER[status = error]
    Q -- 成功 --> OK[status = executed rows + digest]
```

## Docker 快速开始（推荐）

镜像已发布到 GHCR，无需本地安装 Python 依赖：

```bash
docker pull ghcr.io/pnj-star/nl2sql_pipeline:v0.1.4
```

准备 `.env`（复制 [.env.example](.env.example) 后填写），至少包含：

```text
NL2SQL_LLM_BASE_URL=https://api.openai.com/v1
NL2SQL_LLM_API_KEY=your-api-key
NL2SQL_LLM_MODEL=gpt-4o-mini
NL2SQL_DB_ERP_DSN=mysql://ro_user:ro_pass@host.docker.internal:3307/erp_db
AUTH_MODE=disabled
```

启动并验证：

```bash
docker run -d --name nl2sql -p 8000:8000 --env-file .env ghcr.io/pnj-star/nl2sql_pipeline:v0.1.4
curl http://127.0.0.1:8000/health
docker logs nl2sql
docker stop nl2sql
```

- 健康检查：`http://127.0.0.1:8000/health`
- MCP streamable-http 地址：`http://127.0.0.1:8000/streamable`
- 镜像默认监听 `0.0.0.0:8000`，入口命令为 `nl2sql-skill-mcp`
- Windows/macOS 访问宿主机 MySQL/LLM 使用 `host.docker.internal`
- 本地构建验证可改为：`docker build -t nl2sql-skill:test .`

## 本地开发

以下命令在已克隆并进入 `nl2sql_skill` 目录后执行（不要重复 `cd nl2sql_skill`）：

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[sql,semantic,mcp,test]"
Copy-Item .env.example .env   # Windows；Linux/macOS 用 cp .env.example .env
```

最小调用示例：

```python
import asyncio
from nl2sql_skill.builder import build_nl2sql_pipeline
from nl2sql_skill.types import NL2SQLRequest

async def main():
    result = await build_nl2sql_pipeline().generate(
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

本地起 MCP 服务：

```bash
nl2sql-skill-mcp --env-file .env --transport streamable-http --port 8000
```

如果提示找不到 `nl2sql-skill-mcp`，确认已激活虚拟环境，或改用等价命令：

```bash
python -m nl2sql_skill.mcp --env-file .env --transport streamable-http --port 8000
```

## 配置项

完整变量说明见 [.env.example](.env.example)，关键变量速查：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NL2SQL_LLM_BASE_URL` | — | OpenAI 兼容接口地址 |
| `NL2SQL_LLM_API_KEY` | — | 接口密钥 |
| `NL2SQL_LLM_MODEL` | — | 模型名 |
| `NL2SQL_DB_<ID>_DSN` | — | MySQL 只读账号连接串 |
| `NL2SQL_DB_<ID>_SERVER_VERSION` | 8.0 | 5.x 时注入受限语法子集提示 |
| `NL2SQL_DB_<ID>_VISIBLE_TABLES` | 空=全部 | 表白名单 |
| `AUTH_MODE` | jwt | disabled=本地调试；jwt=校验 JWT |
| `NL2SQL_EXAMPLE_STORE_PATH` | 空=不启用 | few-shot 示例库 JSONL 路径 |
| `NL2SQL_COST_GUARD_ENABLED` | false | EXPLAIN 成本闸门开关 |
| `NL2SQL_COST_THRESHOLD_ROWS` | 5000000 | 预估扫描行数上限 |
| `NL2SQL_EXPLAIN_FAIL_POLICY` | deny | EXPLAIN 失败策略 deny/allow |
| `NL2SQL_SEMANTIC_DIR` | 空=不启用 | 语义层 YAML/JSON 目录 |
| `NL2SQL_CLARIFY_THRESHOLD` | 0.75 | 置信阈值 |
| `NL2SQL_MAX_ROWS` | 200 | 行数上限 |
| `NL2SQL_SQL_CACHE_ENABLED` | true | SQL 缓存开关 |
| `NL2SQL_SQL_CACHE_TTL_SECONDS` | 86400 | 缓存 TTL |

## 代码修改后如何发布新版本

1. 修改代码，并在 `pyproject.toml` 中把 `version` 升到新版本（例如 `0.1.5`），README 镜像 tag 同步更新。
2. 本地先验证：`docker build -t nl2sql-skill:test .`，再 `docker run --rm -p 8000:8000 --env-file .env nl2sql-skill:test`。
3. 发布到 GitHub 并触发 Docker 自动构建：

```bash
git add 本次改动的文件
git commit -m "fix: 说明这次改动"
git push
git tag v0.1.5
git push origin v0.1.5
```

命令含义：

| 命令 | 作用 |
| --- | --- |
| `git add` | 把本次改动加入暂存区 |
| `git commit -m "..."` | 在本地保存一个提交 |
| `git push` | 把提交上传到 GitHub |
| `git tag v0.1.5` | 给当前提交打发布标记 |
| `git push origin v0.1.5` | 推送标记，触发 Docker 工作流自动构建并发布到 GHCR |

等 GitHub Actions 的 `Docker` 工作流跑绿后，把 Docker 命令里的版本号换成 `v0.1.5` 即可拉取新镜像。

提醒：
- `git add` 后面必须写真实文件名，例如 `git add README.md pyproject.toml src/nl2sql_skill/mcp.py`，不要照抄示例里的“本次改动的文件”。
- 顺序不要乱：先 `git add` 和 `git commit`，再 `git push`，最后才 `git tag` 并 `git push origin <tag>`。
- 如果 tag 提前误推了，先删除再重建：`git tag -d v0.1.5`、`git push origin :refs/tags/v0.1.5`，新提交完成后重新打 tag 并推送。

注意：如果 `common_core` 依赖也改了，必须先发布 `common_core` 到 PyPI，再发布本包，顺序不能反。

## 测试

```bash
python -m pytest tests -q
```

## 边界与 Roadmap

- 不包含 LangGraph 编排，也不做回答文本生成
- 当前优先支持 MySQL 5.7 / 8.0，PostgreSQL / ClickHouse 后续规划
- 列级权限依赖外部系统，当前通过 `visible_tables` 做表级过滤