---
name: nl2sql_skill
description: 面向 agent 的 MySQL 自然语言转 SQL 工具。传入用户问题与数据源上下文，返回已通过 sqlglot AST 安全校验的只读 SQL（可选附带执行结果）。提供 schema 召回、语义层增强、SQL 缓存、多轮上下文与澄清交互。
---

# nl2sql_skill

`nl2sql_generate` 是面向 agent 的 NL2SQL 工具。传入用户自然语言问题、目标数据源 ID 与租户上下文，返回通过安全护栏的 SQL；`execute=true` 时委托执行器运行并返回结果集。回答生成与话术包装不属于本工具职责，由调用方 agent 完成。

## 什么时候用

- 用户问题需要从 MySQL 业务库取数时调用 `nl2sql_generate`。
- 需要将查询结果与知识库检索结果（如 `retrieve_skill`）融合成一版回答时，用本工具获取数据侧依据。
- 不需要手写 schema 拼装、prompt 组装或 SQL 安全校验；这些由本工具在单次调用内完成。

## 调用方式

必传参数：`query`、`tenant_id`、`db_id`、`request_id`。

可选参数：

- `auth_token`: JWT 令牌（AUTH_MODE=jwt 时必传）。
- `session_id`: 会话 ID，多轮上下文归组。
- `user_id`: 用户 ID，审计归属。
- `history`: 多轮历史列表，每项支持 `{"role","content"}` 或 `{"question","sql"}`。
- `execute`: 默认 false 只返回 SQL；true 时内部委托执行器并返回 `rows`/`columns`。
- `max_rows`: 本次行数上限覆盖，默认配置值 200。
- `clarify_threshold`: 本次置信阈值覆盖，默认配置值 0.75。
- `top_k_tables`: schema 召回候选表数量覆盖，默认配置值 8。

## 处理流程

```text
query + tenant_id + db_id + request_id
→ 元数据快照加载（TTL 缓存命中时不触达 MySQL）
   └─ db_id 未注册 → status=no_schema，结束
→ Schema Linking（词法 + 列注释 + 语义描述 + bigram 兜底召回）
   └─ 候选表为空 → status=no_schema，结束
→ 确定性槽位校验（时间敏感表缺时间范围 → 追加澄清提示）
→ SQL 缓存检查
   ├─ 命中 → 重跑护栏校验
   │         ├─ 通过 → status=generated_cache 或 executed（cache_hit=true）
   │         └─ 未通过 → 当作未命中继续生成
   └─ 未命中 → 注入 few-shot → LLM 生成 JSON
      ├─ LLM 调用失败 → status=error，结束
      ├─ JSON 解析失败 → 允许一次修复重试，仍失败 → error
      ├─ 置信不足 / 槽位缺失 / LLM 主动要求澄清
      │   → status=need_clarification + clarify_questions，结束
      └─ 有 SQL → sqlglot AST 护栏校验
         ├─ 写操作 / 越权表 / 文件导出等 → status=rejected_guardrail，结束
         └─ 通过 → 自动补齐 LIMIT → 写入 SQL 缓存
            → execute=true ? 委托执行器返回 rows : 返回 generated
```

## 返回值

```json
{
  "status": "generated",
  "ok": true,
  "query": "上月华东区GMV",
  "sql": "SELECT SUM(amount) FROM orders WHERE ...",
  "confidence": 0.92,
  "used_tables": ["orders"],
  "assumptions": ["时间范围取订单创建时间"],
  "clarify_questions": [],
  "rows": [],
  "columns": [],
  "truncated": false,
  "cache_hit": false,
  "tenant_id": "t1",
  "db_id": "erp-prod",
  "request_id": "req-42",
  "trace_id": "trc-...",
  "message": "",
  "audit": {"metadata_fetch_s": 0.0012, "llm_generate_s": 0.8105}
}
```

| 字段 | 说明 |
| --- | --- |
| `status` | 机器可读状态码（权威信号），见下表 |
| `ok` | 是否无内部异常，等价于 `status != "error"` |
| `sql` | 通过全部校验的最终 SQL；澄清/拦截场景为空串 |
| `confidence` | LLM 自评置信度 [0,1]；存在虚高风险，仅作参考信号 |
| `used_tables` | SQL 实际引用的物理表（AST 解析权威值） |
| `assumptions` | LLM 声明的口径假设，供上层展示核对 |
| `clarify_questions` | need_clarification 时非空，转给用户追问 |
| `rows` / `columns` | execute=true 且成功时的结果集与列名 |
| `cache_hit` | 是否命中 SQL 缓存 |
| `audit` | 耗时打点、候选表全量、schema fingerprint 等 |
| `message` | 状态的人类可读说明 |

## 按 status 处理结果

| status | 含义 | 调用方行为建议 |
| --- | --- | --- |
| `generated` | 生成并通过全部校验（未执行） | 直接展示 SQL 或进入执行/摘要 |
| `generated_cache` | 未执行且命中 SQL 缓存 | 同上，成本最低 |
| `executed` | execute=true 且执行成功 | 使用 `rows`/`columns` 渲染 |
| `need_clarification` | 置信不足、缺关键槽位 | 把 `clarify_questions` 转给用户 |
| `no_schema` | 数据源未注册或没有召回到相关表 | 提示换个问法或检查 db_id |
| `rejected_guardrail` | 护栏拦截 | 如实转述 message，不要重试原问题 |
| `error` | 内部异常 | 用 `trace_id`/`request_id` 排障 |

## 缓存与成本

- SQL 缓存默认开启（Redis）。key 覆盖规范化 query、db_id、semantic_version、schema_fingerprint 和 model_tag；任一变更即天然失效。
- 缓存命中后跳过 LLM 但必须重跑完整 AST 护栏校验再返回——护栏规则升级后旧缓存不会绕过新检查。
- 澄清、拦截和异常结果一律不写入缓存。

## 边界

- 只做 NL 到 SQL 的生成与静态校验，不做回答文本生成。
- 硬性禁止写入：INSERT / UPDATE / DELETE / DROP / ALTER / TRUNCATE / GRANT 等，AST 白名单加执行器双层防线。
- 非 MySQL 方言暂不支持。
- 权限系统由外部实现；本工具消费 visible_tables 白名单做表级过滤。
