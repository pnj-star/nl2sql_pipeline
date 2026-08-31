---
name: nl2sql_skill
description: 面向 agent 的 MySQL 自然语言转 SQL 工具。传入用户问题与数据源上下文，返回已通过 sqlglot AST 安全校验的只读 SQL（可选附带执行结果）。提供 schema 召回、语义层增强、SQL 缓存、多轮上下文与澄清交互。
---

# nl2sql_skill

`nl2sql_generate` 是面向 agent 的 NL2SQL 工具：传入用户自然语言问题、目标数据源 ID 与租户上下文，返回通过安全护栏的只读 SQL；`execute=true` 时再返回执行结果。回答生成与话术包装不属于本工具职责，由调用方 agent 完成。

## 什么时候用

- 用户问题需要从 MySQL 业务库取数时，调用 `nl2sql_generate`。
- 需要把查询结果与知识库检索结果（如 `retrieve_skill`）融合成一版回答时，用本工具获取数据侧依据。
- 不需要自己拼 schema、写 prompt 或做 SQL 安全检查；这些在单次调用内完成。

不要用它：

- 用户要的是写操作（插入/更新/删除）、建表或导出文件——工具会直接拒绝，告知用户不支持即可。
- 数据源不是 MySQL（如 PostgreSQL / ClickHouse）——暂不支持。

## 调用方式

工具名：`nl2sql_generate`。

必传参数：

- `query`: 用户自然语言问题。
- `tenant_id`: 租户标识。
- `db_id`: 数据源 ID（需在服务端注册）。
- `request_id`: 调用方生成的请求 ID，用于排障与链路关联。

可选参数：

- `auth_token`: JWT 令牌（服务端开启鉴权时必传）。
- `session_id`: 会话 ID；同一会话的历史会自动带入，支持多轮追问。
- `user_id`: 用户 ID，审计归属。
- `history`: 显式多轮历史列表，每项 `{"role","content"}` 或 `{"question","sql"}`。
- `execute`: `true` 时返回执行结果 `rows` / `columns` / `digest`；默认 `false` 只返回 SQL。
- `max_rows`: 本次行数上限覆盖（默认 200）。
- `clarify_threshold`: 本次置信阈值覆盖（默认 0.75）。
- `top_k_tables`: schema 召回候选表数量覆盖（默认 8）。

调用示例：

```json
{
  "query": "上个月华东区销售额 TOP10 的门店",
  "tenant_id": "t1",
  "db_id": "erp",
  "request_id": "req-42",
  "execute": true
}
```

## 一次调用会发生什么

```text
问题 → 元数据/schema 召回 → 缓存检查 → (未命中) LLM 生成 → AST 安全护栏 → (可选) 执行
```

你会收到一个稳定结构的 `NL2SQLResult`，按 `status` 字段分支处理即可。agent 需要特别注意：

- `need_clarification`: 问题缺关键条件（时间范围、统计口径、分组粒度等）。把返回的 `clarify_questions` **原样转给用户追问**，不要自行编造答案。
- `rejected_guardrail`: SQL 被安全护栏或成本闸门拦截。如实转述 `message`，**不要重试同一个问法**。
- `no_schema`: 数据源未注册或没召回到相关表。提示用户换问法或检查 `db_id`。
- `error`: 内部异常。用 `trace_id` / `request_id` 反馈排障。

## 返回值

```json
{
  "status": "generated",
  "ok": true,
  "query": "你们产品有什么",
  "sql": "SELECT SUM(amount) FROM orders WHERE ...",
  "confidence": 0.92,
  "used_tables": ["orders"],
  "assumptions": ["时间范围取订单创建时间"],
  "clarify_questions": [],
  "rows": [],
  "columns": [],
  "digest": null,
  "truncated": false,
  "cache_hit": false,
  "tenant_id": "t1",
  "db_id": "erp",
  "request_id": "req-42",
  "trace_id": "",
  "message": "",
  "audit": {}
}
```

关键字段：

| 字段 | 说明 |
| --- | --- |
| `status` | 机器可读状态码（权威信号），见下表 |
| `sql` | 通过全部校验的最终只读 SQL；澄清/拦截/异常时为空串 |
| `confidence` | LLM 自评置信度 [0,1]，存在虚高风险，仅作参考 |
| `assumptions` | LLM 声明的口径假设，供你转述/核对 |
| `clarify_questions` | `need_clarification` 时非空，转给用户追问 |
| `rows` / `columns` / `digest` | `execute=true` 成功时的结果集、列名、数值摘要 |
| `cache_hit` | 是否命中 SQL 缓存（不影响你处理结果的方式） |
| `message` | 状态的人类可读说明 |

按 `status` 处理结果：

| status | 含义 | 调用方行为 |
| --- | --- | --- |
| `generated` | 生成并通过校验（未执行） | 展示 SQL，或自行决定下一步 |
| `generated_cache` | 未执行且命中缓存 | 同上，成本最低 |
| `executed` | execute=true 已执行 | 用 `rows` / `digest` 渲染 |
| `need_clarification` | 缺关键槽位 | 把 `clarify_questions` 转给用户 |
| `no_schema` | 数据源未注册/无相关表 | 建议换问法或检查 db_id |
| `rejected_guardrail` | 护栏/成本闸门拦截 | 如实转述 message，不重试原问法 |
| `error` | 内部异常 | 用 trace_id/request_id 反馈 |

## 行为约定

- 只读契约：只生成 `SELECT`/`WITH` 只读查询；写操作、DDL、文件导出一律拒绝。
- 多轮对话：同一 `session_id` 的历史会自动参与生成，你只传递用户的最新问题即可。
- 成本保护：`execute=true` 时服务端会做 EXPLAIN 成本闸门，超大扫描可能返回 `rejected_guardrail`，无需重试。