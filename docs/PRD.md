# nl2sql_skill 产品需求文档（PRD）

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v0.1（草案） |
| 所属仓库 | `nl2sql_skill`（本仓库目录） |
| 状态 | 待评审 |
| 目标读者 | 技能开发、平台架构、安全合规、评测团队 |

---

## 1. 背景与定位

### 1.1 背景

企业数据分析中大量问题以自然语言提出（"上月华东区 GMV 是多少"、"哪些 SKU 退货率环比上升"），但答案沉淀在 MySQL 业务库中。现有 agent 栈已经具备：

- `retrieve_skill`：知识库 RAG 检索，负责文档类依据；
- `structured_query_skill`：确定性 SQL 执行器，提供只读护栏、行数限制与租户级缓存；
- `common_core`：AgentContext、RedisCache、JWT 鉴权等公共设施；
- `eval_skill`：技能评测框架。

缺失的一环是 **NL → SQL 的生成与校验能力**。`nl2sql_skill` 补齐这一环，使 agent 能够把自然语言问题转换为经过校验、可安全执行的 MySQL SQL。

### 1.2 一句话定位

> `nl2sql_skill` 是面向 agent 的 MySQL 自然语言转 SQL 工具：输入用户问题与数据库上下文，返回**已通过语法解析与安全护栏校验的 SQL**（可选附带执行结果），不做回答生成与话术包装。

### 1.3 对标主流方案

| 方案 | 核心思路 | 本工具借鉴点 | 不照搬的部分 |
| --- | --- | --- | --- |
| Vanna AI | 训练向量化的 DDL/文档/SQL 三元组做 RAG 增强生成 | "DDL + 文档 + 示例问答"三库召回注入 prompt；示例自学习闭环 | 不绑定其训练/微调链路，改用检索式 few-shot + 可选微调 |
| Databricks Genie | 平台托管语义层 + LLM 生成 + 沙箱执行 | 表/列注释、值字典、指标口径等语义元数据驱动生成 | 不依赖 Databricks Unity Catalog，用 information_schema + 自建语义层 |
| Snowflake Cortex Analyst | 基于 YAML 语义模型（表、列、同义词、指标）约束生成 | 强 schema 约束的 prompt 结构、verified query 机制 | 不强制 YAML 格式，语义层以 DB 元数据 + 可编辑配置文件双轨供给 |
| DB-GPT | 多模型 Text2SQL 微调 + ChatData 场景 | 多轮上下文管理、SQL 执行结果回传摘要的编排方式 | 不内置整套平台，保持单工具边界 |
| Wren AI | 语义层建模（MDL）+ dbt 风格建模 | 列级血缘/外键关系辅助 JOIN 推导 | 不要求用户先建 MDL，支持从 information_schema 自动抽取 |
| Dataherald | 企业级 NL→SQL API，含规则引擎与人工审核 | 规则引擎（禁用模式、强制 WHERE）、置信度分级 | 不做 SaaS 化多租户门户，聚焦 MCP 工具形态 |
| 国内 ChatBI（观远/有数/DataWorks Copilot 等） | 指标口径管理 + 图表渲染 | 指标字典、同义词映射、澄清交互 | 渲染与 BI 看板不在本工具范围 |

综合结论：本工具对标 **"Vanna 的检索增强生成 + Cortex Analyst 的语义约束 + Genie 的沙箱护栏"** 的组合，但以轻量 MCP skill 形态交付。

## 2. 用户画像与核心场景

### 2.1 用户画像

| 角色 | 使用方式 | 关键诉求 |
| --- | --- | --- |
| 业务分析师（终端用户） | 通过上层 agent 提问 | 问题即答案；看不懂 SQL 也无妨 |
| Agent 开发者 | 在 LangGraph/MCP 中调用 `nl2sql_generate` | 返回结构稳定、status 明确、可直接进入执行或澄清分支 |
| 数据管理员 | 维护语义层配置（注释、同义词、指标口径、权限） | 无需写代码即可补充业务语义 |
| 安全/审计人员 | 审查访问日志与护栏策略 | 只读保证、租户隔离、PII 处理可证明 |

### 2.2 核心场景

1. **单轮取数**："上个月销售额 TOP10 的门店" → 单条 SELECT → 执行 → 返回结果集。
2. **多轮追问**：首轮"各区域销量"，追问"只看华东呢" → 结合会话历史补全条件重新生成。
3. **模糊提问澄清**："最近销售怎么样" → 缺少时间范围与指标定义 → 返回 `need_clarification` 及澄清问题列表，由 agent 向用户追问。
4. **跨表分析**：涉及 JOIN 的复合问题 → 依据外键与语义关系推导连接路径。
5. **知识融合回答**（与 retrieve_skill 协作）：业务规则类问题走检索，数据类问题走 NL2SQL，两路结果由调用方融合成统一回答。
6. **示例自学习**：管理员确认一条高质量的"问题→SQL"后写入示例库，后续同类问题作为 few-shot 召回。

## 3. 产品边界

### 3.1 职责划分

```text
用户提问
   │
   ▼
调用方 agent（编排层）
   │  决定路由：知识库 vs 数据库 vs 融合
   ▼
┌─────────────────────────────────────────────┐
│ nl2sql_skill                                │
│  ① 语义层/schema 召回                        │
│  ② LLM 生成 SQL                             │
│  ③ sqlglot 解析 + 护栏静态校验                │
│  ④ （可选）委托 structured_query_skill 执行   │
│  返回：SQL / 结果集 / status                  │
└─────────────────────────────────────────────┘
   │
   ▼
调用方 agent：结果摘要、图表、话术、兜底
```

**做**：schema 理解、SQL 生成、静态校验、可选执行编排、缓存、评测钩子、审计日志。

**不做**：

- 回答文本生成与话术润色（调用方职责，公共拼装逻辑见 `common_core.rag`）；
- 数据写入/DDL/DML——硬性禁止；
- BI 图表渲染；
- 非 MySQL 方言（PostgreSQL/Oracle 等列入 Roadmap 后期，见 §12）；
- 权限系统的实现——只消费外部传入的角色/权限声明并据此过滤 schema 与列。

### 3.2 与 structured_query_skill 的关系

`structured_query_skill` 已提供受护栏的执行器（SELECT/WITH only、单语句、行数上限、租户缓存）。本工具默认**复用**它执行生成的 SQL，不重复造执行轮子：

- `execute=false`（默认）：只返回 SQL，执行决策留给调用方；
- `execute=true`：内部调用 `StructuredQueryPipeline.query()`，返回结果集，继承其全部护栏与缓存行为。

这样安全边界单一：所有真正触达 MySQL 的语句都经由同一套护栏。

## 4. 功能需求（FR）

### FR-1 数据源注册与元数据采集

| 编号 | 需求 | 优先级 |
| --- | --- | --- |
| FR-1.1 | 支持注册多个 MySQL 数据源（`db_id` 唯一标识），每个数据源独立配置连接串（env 或配置文件引用，明文不入库不入日志）。 | P0 |
| FR-1.2 | 从 `information_schema` 自动采集：表清单、列名、列类型、主键、外键、索引、表/列 COMMENT。 | P0 |
| FR-1.3 | 为每张表采样少量非敏感样例行（可配开关与条数，默认关闭），用于 prompt 中的 value hint；开启采样时按 PII 列黑名单（`NL2SQL_SCHEMA_SAMPLE_PII_DENYLIST`，支持通配符）强制跳过命中列，不完全依赖总开关。 | P1 |
| FR-1.4 | 计算 schema fingerprint（对元数据内容哈希），元数据变更时自动失效相关缓存并触发重载。 | P0 |
| FR-1.5 | 支持手动触发与定时刷新元数据快照；快照持久化，LLM 调用使用快照而非直连探测，降低延迟与目标库压力。 | P0 |

### FR-2 语义层（Semantic Layer）

对标 Cortex Analyst / Genie：LLM 生成的质量上限取决于业务语义供给。

| 编号 | 需求 | 优先级 |
| --- | --- | --- |
| FR-2.1 | 语义配置文件（YAML 或 JSON，随技能分发）：为表/列补充业务别名、同义词、枚举值含义、指标公式（如 `GMV = SUM(amount)`）、时间字段语义（下单时间 vs 发货时间）。 | P0 |
| FR-2.2 | 同义词映射在 schema linking 时参与匹配：如 "营业额/GMV/sales" → `orders.amount`。 | P0 |
| FR-2.3 | 指标字典：预定义指标名、口径 SQL 片段、适用维度；命中指标的问题优先按口径生成。 | P1 |
| FR-2.4 | 权限声明：按角色声明可见表/列黑名单；生成阶段直接从 prompt 上下文中剔除不可见对象，并在校验阶段二次拦截。 | P0 |
| FR-2.5 | 语义层版本号纳入 SQL 缓存 key，配置变更自动失效缓存。 | P0 |

### FR-3 Schema Linking（候选裁剪）

大库不能整库塞进 prompt。对标 Vanna/Cortex：先召回再生成。

| 编号 | 需求 | 优先级 |
| --- | --- | --- |
| FR-3.1 | 将表/列元数据向量化（复用 common_core 嵌入设施），问题与表/列做相关性召回，选出 top-N 候选表及其关联列。 | P0 |
| FR-3.2 | 候选表自动扩展一跳外键邻接表（支撑 JOIN 推理）。 | P0 |
| FR-3.3 | 候选预算：prompt 中 schema 上下文设 token 上限（可配），超出时按相关性截断并在响应中标注被裁剪对象。 | P0 |
| FR-3.4 | 示例库召回：从"已验证问题→SQL"示例库（FR-7）中检索相似问题作为 few-shot 注入。 | P1 |

### FR-4 SQL 生成

| 编号 | 需求 | 优先级 |
| --- | --- | --- |
| FR-4.1 | LLM 生成遵循固定 prompt 模板：系统规则（方言=MySQL、只读、必须 LIMIT 等）+ 候选 schema + few-shot + 历史 + 当前问题。 | P0 |
| FR-4.2 | 输出结构化（JSON mode / function call）：`{sql, confidence, used_tables, assumptions}`，拒绝自由文本包裹。 | P0 |
| FR-4.3 | MySQL 方言适配：反引号标识符、`LIMIT n` 分页、8.0 窗口函数可用性检测（按数据源声明的 server version 选择语法子集）。 | P0 |
| FR-4.4 | 多轮上下文：传入 `history`（此前轮次的 question/sql/results_digest），代词消解（"它/该区域"）与条件继承；skill 内部按 `NL2SQL_HISTORY_MAX_TOKENS` 做预算截断（保留近期轮次、丢弃早期轮次），超限不报错只截断，防止上层传入超长 history 挤压 schema 上下文导致生成质量下跌。 | P0 |
| FR-4.5 | 低置信路径：当 LLM 自评 `confidence < threshold` 或关键槽位缺失（时间范围/分组粒度）时返回 `need_clarification` 并给出具体澄清问题。**备注：LLM 自评 confidence 存在虚高风险，不可单独作为澄清判断依据；必须同时执行槽位校验与 schema-linking 字段充足性校验，三者任一命中即触发澄清。** | P0 |
| FR-4.6 | 生成失败自动降级：仅 JSON 解析失败允许一次修复式重试（把解析错误信息回喂），仍失败则 `error`；SQL 校验失败（护栏拦截）与逻辑错误**禁止 skill 内部静默重试重新生成**，立即返回 `rejected_guardrail` 交给调用方编排层处理（受控的执行报错修正循环见 Roadmap）。 | P1 |

### FR-5 SQL 校验与安全护栏

所有 SQL 必须通过以下流水线才允许返回/执行：

```text
sqlglot.parse(dialect="mysql")
→ AST 白名单检查：仅 SELECT/WITH；禁 INSERT/UPDATE/DELETE/DROP/
  ALTER/TRUNCATE/GRANT/SET/USE/CALL/LOAD_FILE 等
→ 单语句检查（分号后不得再有语句）
→ 强制 LIMIT（缺失则自动追加 max_rows）
→ 权限复核：AST 中出现的表/列必须在租户可见集合内
→ （execute=true 时）EXPLAIN 干跑：预估扫描量超阈值则拒绝并提示优化
```

| 编号 | 需求 | 优先级 |
| --- | --- | --- |
| FR-5.1 | 基于 sqlglot 的 AST 级校验（非正则），覆盖注释混淆、hex 绕过、嵌套子查询写操作等已知绕过手法；显式拦截 `SELECT ... INTO OUTFILE/DUMPFILE`（属 SELECT 语法子集，仅靠语句类型白名单会放行）及 `LOAD_FILE()` 等文件访问函数。 | P0 |
| FR-5.2 | 自动追加 `LIMIT`；用户显式 LIMIT 超过 `max_rows` 时压到上限并在响应说明。 | P0 |
| FR-5.3 | EXPLAIN 成本闸门：`estimated_rows > cost_threshold` 时拒绝执行，返回 `rejected_guardrail` 与原因。EXPLAIN 结果基于统计信息估算，存在误判可能；闸门失败时按 `NL2SQL_EXPLAIN_FAIL_POLICY`（deny / allow）降级并告警，不做硬拒绝；同时评估 EXPLAIN 自身的数据库开销，仅在 execute=true 路径触发。 | P1 |
| FR-5.4 | 连接串使用只读账号（应用层约定 + 文档要求），即使护栏失守也无法写入。 | P0 |

### FR-6 执行编排（可选）

| 编号 | 需求 | 优先级 |
| --- | --- | --- |
| FR-6.1 | `execute=true` 时委托 `structured_query_skill` 执行，透传 `max_rows`；执行超时由执行器侧连接/读超时配置管理。 | P0 |
| FR-6.2 | 返回结果集（列名 + 行数组，行数受 `max_rows` 截断）与 `truncated` 标记。 | P0 |
| FR-6.3 | 结果集过大时的摘要 digest（行数、数值聚合概览）供多轮上下文使用，避免历史膨胀。 | P2 |
| FR-6.4 | 执行侧异常状态映射（execute=true）：下游护栏拦截 → `rejected_guardrail`；下游执行超时 / 数据库连接异常 → `error`（message 注明执行阶段）；下游结果为空 → 正常返回 `executed` + 空 rows，由调用方决定话术。 | P0 |

### FR-7 示例库与自学习

对标 Vanna 的训练三元组，但简化为运营侧可维护的示例库。

| 编号 | 需求 | 优先级 |
| --- | --- | --- |
| FR-7.1 | 提供 `nl2sql_feedback` 工具（P1）：管理员提交 `{question, sql, verified=true}` 写入示例库（MySQL 表或 JSONL，按 tenant/db 隔离）；写入前必须复用 guardrails 做 AST 安全校验，非法 SQL 直接拒绝入库并返回原因，防止污染 few-shot 示例。 | P1 |
| FR-7.2 | 示例库向量化并参与 FR-3.4 的 few-shot 召回；相似度低于 `NL2SQL_EXAMPLE_MIN_SIMILARITY`（默认 0.75，可配）的示例不注入 prompt，防噪声。 | P1 |
| FR-7.3 | 示例去重与冲突标记（同一 question 不同 sql 时保留最新 verified 版本）。 | P2 |

### FR-8 缓存体系

| 层 | 内容 | Key 组成 | 命名空间 | 默认 TTL |
| --- | --- | --- | --- | --- |
| schema 缓存 | 元数据快照 + embedding | `db_id` + fingerprint | `nl2sql_schema` | 手动/定时刷新 |
| SQL 缓存 | 规范化问题 → 校验通过的 SQL | 规范化 query + db_id + semantic_version + model_config_hash +（可选）history_hash（`NL2SQL_CACHE_KEY_INCLUDE_HISTORY` 控制，默认 false；多轮会话每追加一轮即 miss，命中率损失大于收益） | `nl2sql_sql_cache` | 可配，默认 24h |
| 结果缓存 | （仅 execute=true）SQL → 结果集 | 继承 structured_query_skill 的 key | 其自身命名空间 | 其自身策略 |

规范：沿用 `common_core.RedisCache`，key 含 tenant_id 实现隔离；schema/semantic/model 任一变更即失效。`need_clarification`、`rejected_guardrail`、`error` 一律不写入 SQL 缓存（与 retrieve_skill 的 no_context 不写缓存原则一致）。**缓存命中后仍必须执行完整 AST 护栏校验后再返回/执行**：护栏规则可能随版本升级（如新增 INTO OUTFILE 拦截），不重跑会让旧缓存 SQL 绕过新规则；sqlglot 解析为毫秒级本地操作，成本可忽略。

### FR-9 可观测性与审计

| 编号 | 需求 | 优先级 |
| --- | --- | --- |
| FR-9.1 | 每次调用记录结构化审计日志：request_id、tenant_id、user_id、原始问题（可配脱敏）、生成 SQL、status、耗时、token 用量、是否命中缓存、使用的表/列；另记录 `candidate_tables_all`（召回的全部候选表）与 `truncated_tables`（被 token 预算截断的表），用于排查 schema 截断导致的生成 badcase。 | P0 |
| FR-9.2 | 日志中的连接串、密码、auth_token 绝对不落盘；PII 列值默认掩码。 | P0 |
| FR-9.3 | 返回 `trace_id` 供跨链路追踪（与 retrieve_skill 行为一致）。 | P0 |
| FR-9.4 | 暴露 Prometheus 指标：请求数、status 分布、端到端延迟、LLM 延迟、缓存命中率、护栏拦截数。 | P1 |

### FR-10 评测与回归

| 编号 | 需求 | 优先级 |
| --- | --- | --- |
| FR-10.1 | 内置评测脚本对接 `eval_skill`：跑 golden set（question → expected_sql/result 断言），输出 Execution Accuracy 与 Exact Match。 | P0 |
| FR-10.2 | 公开基准冒烟：Spider/BIRD dev 子集（MySQL 方言转换）用于回归比较，不作上线门槛。 | P2 |
| FR-10.3 | CI 门槛建议：golden set Execution Accuracy ≥ 85%（M1 目标），护栏绕过用例 100% 拦截。 | P0 |
| FR-10.4 | 护栏负样本集：注入、写操作、越权表、超大扫描等至少 30 条用例进 CI。 | P0 |

## 5. 接口设计

### 5.1 MCP 工具定义

架构约束：核心业务主链路优先调用 §5.2 的 Python 本地 API（`build_nl2sql_pipeline()`），MCP Server 仅作为对外暴露的可选薄外壳，业务逻辑不依赖 MCP 存在。

暴露两个 stdio/HTTP MCP 工具：

#### `nl2sql_generate`

必传参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `query` | string | 用户自然语言问题 |
| `tenant_id` | string | 租户 ID |
| `db_id` | string | 数据源 ID |
| `request_id` | string | 幂等/追踪 ID |

可选参数：

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `auth_token` | string | — | JWT；AUTH_MODE=jwt 时必传，claims 需匹配 scope |
| `session_id` | string | "" | 会话 ID，多轮上下文关联 |
| `user_id` | string | "" | 审计归属 |
| `history` | array | [] | `[{role, content}]` 或 `[{question, sql}]`，多轮上下文 |
| `execute` | bool | false | 是否委托 structured_query_skill 执行 |
| `max_rows` | int | 配置值 | 行数上限 |
| `clarify_threshold` | float | 配置值 | 本次覆盖置信阈值 |
| `top_k_tables` | int | 配置值 | schema 召回候选表数 |

响应 schema（与 retrieve_skill 风格对齐）：

```json
{
  "status": "generated",
  "ok": true,
  "query": "上月华东GMV",
  "sql": "SELECT ...",
  "confidence": 0.92,
  "used_tables": ["orders", "shops"],
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
  "message": ""
}
```

status 取值：

| status | 含义 | 调用方建议 |
| --- | --- | --- |
| `generated` | 生成并通过全部校验 | 直接展示 SQL 或进入执行/摘要 |
| `generated_cache` | 未执行且命中 SQL 缓存 | 同上，成本最低 |
| `executed` | execute=true 且执行成功（无论 SQL 是否来自缓存）；此时以 `cache_hit=true` 标识缓存来源，status 取 `executed` 优先于 `generated_cache` | 使用 rows/columns 渲染 |
| `need_clarification` | 置信不足、缺关键槽位，或候选表存在但缺少回答问题必需的字段 | 把 clarify_questions 转给用户 |
| `no_schema` | schema 召回完全无候选表/问题与库无关 | 提示换个问法或检查 db_id |
| `rejected_guardrail` | 校验/EXPLAIN/权限拦截 | 如实转述 message，不要重试原问题 |
| `error` | 内部异常 | 用 trace_id/request_id 排障 |

#### `nl2sql_feedback`（P1）

入参 `{tenant_id, db_id, question, sql, verified, request_id, auth_token?}`；效果见 FR-7。

响应 `{status, ok, message, request_id, trace_id}`：

| status | 含义 |
| --- | --- |
| `feedback_saved` | 通过 AST 校验并入库成功 |
| `rejected_guardrail` | SQL 未通过校验被拒，message 给出原因 |
| `error` | 存储/内部异常 |

### 5.2 Python API（库内调用）

遵循 skill-template 结构：

```python
from nl2sql_skill.builder import build_nl2sql_pipeline
from nl2sql_skill.types import NL2SQLRequest

pipeline = build_nl2sql_pipeline()          # 读环境变量装配
result = await pipeline.generate(
    NL2SQLRequest(query="...", tenant_id="t1", db_id="d1", request_id="r1"),
)                                            # → NL2SQLResult（dataclass，含上述字段）
```

## 6. 配置项（环境变量）

命名风格与现有技能一致，前缀 `NL2SQL_`：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NL2SQL_DB_<ID>_DSN` | — | 各数据源连接串（只读账号） |
| `NL2SQL_DB_<ID>_SERVER_VERSION` | 8.0 | 方言子集选择 |
| `NL2SQL_SCHEMA_REFRESH_CRON` | disabled | 元数据刷新计划 |
| `NL2SQL_SCHEMA_SAMPLE_ROWS_ENABLED` | false | 样例行采样开关 |
| `NL2SQL_SCHEMA_SAMPLE_PII_DENYLIST` | *phone*,*id_card* | 采样列黑名单（通配符），命中列跳过采样 |
| `NL2SQL_SEMANTIC_CONFIG_DIR` | ./semantic | 语义层 YAML 目录 |
| `NL2SQL_LLM_PROVIDER` | openai_compatible | LLM 提供方 |
| `NL2SQL_LLM_MODEL` | — | 模型名 |
| `NL2SQL_LLM_TEMPERATURE` | 0.0 | 生成温度 |
| `NL2SQL_CLARIFY_THRESHOLD` | 0.75 | 置信阈值 |
| `NL2SQL_MAX_ROWS` | 200 | 默认行上限 |
| `NL2SQL_COST_THRESHOLD_ROWS` | 5000000 | EXPLAIN 扫描量闸门 |
| `NL2SQL_SCHEMA_CONTEXT_MAX_TOKENS` | 4000 | schema 上下文预算 |
| `NL2SQL_SQL_CACHE_TTL_SECONDS` | 86400 | SQL 缓存 TTL |
| `NL2SQL_SQL_CACHE_ENABLED` | true | SQL 缓存开关 |
| `NL2SQL_CACHE_KEY_INCLUDE_HISTORY` | false | history 是否参与 SQL 缓存 key；默认关闭避免多轮 miss |
| `NL2SQL_HISTORY_MAX_TOKENS` | 2000 | 多轮 history 的 token 预算，超限截断早期轮次 |
| `NL2SQL_EXAMPLE_MIN_SIMILARITY` | 0.75 | few-shot 示例注入的最小相似度阈值 |
| `NL2SQL_EXPLAIN_FAIL_POLICY` | deny | EXPLAIN 失败时放行（allow）/拒绝（deny），失败必告警 |
| `AUTH_MODE` | none | none/jwt，与全仓一致 |

## 7. 安全与合规需求

1. **只读硬保证**：三层防线——只读账号（部署要求）、AST 白名单、执行器既有护栏；任何一层都足以阻断写操作。
2. **租户隔离**：所有缓存与示例库按 tenant_id 隔离；JWT claims 校验与 retrieve_skill 同规则。
3. **最小暴露**：schema 召回前先按角色权限过滤，不可见表/列根本不进 prompt。
4. **PII 保护**：样例采样默认关；审计日志中 PII 值掩码；结果集不做二次脱敏（执行层已有租户约束，脱敏策略归数据源治理）。
5. **审计可追溯**：每次生成留痕（谁、何时、问了什么、生成了什么 SQL、是否执行）；日志保留周期按公司合规要求配置。
6. **注入防御**：用户问题只作为自然语言进入 prompt，永不拼接进 SQL 字符串；参数化由执行器处理。

## 8. 非功能需求

| 维度 | 指标 |
| --- | --- |
| 端到端延迟（不含执行） | 热 schema：P50 ≤ 3s，P95 ≤ 8s；缓存命中：P95 ≤ 300ms；元数据冷启动（首次加载/刷新快照）单独观测，不混入常规指标——测试与压测脚本必须过滤冷启动样本后统计 SLA，冷启动仅观测、不纳入 SLA 考核 |
| 执行延迟 | 继承 structured_query_skill 超时配置 |
| 可用性 | 无单点：LLM 不可达时明确报错；MySQL 元数据不可达时使用最近快照并告警 |
| 并发 | 单实例 ≥ 20 QPS（受 LLM 配额主导，需支持限流排队） |
| 成本 | SQL 缓存命中率目标 ≥ 40%（稳态）；prompt token 上限可配防止爆账单 |
| 兼容性 | Python 3.12+；MySQL 5.7 / 8.0；Windows/Linux 开发环境均可运行 |

## 9. 里程碑规划

| 里程碑 | 内容 | 验收标准 |
| --- | --- | --- |
| M0 骨架 | 目录脚手架（skill-template）、类型定义、配置装载、单元测试骨架 | `pytest` 通过；配置缺项报错清晰 |
| M1 最小闭环 | 元数据采集 + 简单召回 + LLM 生成 + sqlglot 校验 + `generated` 返回 | golden set Execution Accuracy ≥ 85%；护栏负样本 100% 拦截 |
| M2 执行与缓存 | execute 委托、三级缓存、多轮 history、澄清路径 | 多轮用例通过；缓存命中路径测试覆盖 |
| M3 运营化 | feedback 示例库、EXPLAIN 闸门、审计/指标、评测脚本接入 eval_skill | 审计字段完整；评测报告可一键产出 |

## 10. 目录规划（预期交付物）

```text
nl2sql_skill/
├── SKILL.md                 # 面向 agent 的使用说明（评审通过后编写）
├── README.md
├── pyproject.toml
├── .env.example             # 上述配置项模板
├── docs/
│   └── PRD.md               # 本文档
├── src/nl2sql_skill/
│   ├── __init__.py
│   ├── types.py             # Request/Result dataclass
│   ├── config.py            # 环境变量装载
│   ├── metadata.py          # information_schema 采集 + fingerprint
│   ├── semantic.py          # 语义层加载/同义词/指标
│   ├── linking.py           # schema 召回与候选裁剪
│   ├── generator.py         # prompt 组装 + LLM 调用
│   ├── guardrails.py        # sqlglot AST 校验
│   ├── pipeline.py          # 总编排
│   ├── builder.py           # 依赖装配
│   └── mcp.py               # MCP server 入口（nl2sql_generate / nl2sql_feedback）
├── tests/                   # 单测 + 护栏负样本 + golden set
└── evals/                   # 评测数据与脚本入口
```

## 11. 风险与开放问题

| # | 风险/问题 | 应对 |
| --- | --- | --- |
| 1 | LLM 幻觉编造不存在的表/列 | AST 校验兜底 + `used_tables` 回显核对 + need_clarification 降级 |
| 2 | 大库 prompt 爆炸 | schema 召回裁剪（FR-3.3）+ token 预算 |
| 3 | 指标口径不一致导致"数字对不上" | 语义层指标字典（FR-2.3）+ assumptions 显式回显 |
| 4 | 多租户下语义配置维护成本 | 先按 db_id 共享、租户 override 后置到 P2 |
| 5 | MySQL 5.7 无窗口函数/CTE | SERVER_VERSION 声明选择语法子集（FR-4.3） |
| 6 | 缓存返回过期 SQL（schema 已变） | fingerprint/semantic_version 进 key，变更即失效（FR-1.4/2.5） |
| 7 | 用户问"为什么这个数和报表不一样" | 超出边界：agent 引导至 retrieve_skill 查口径文档 |

## 12. 后续演进（Roadmap，非本期承诺）

- PostgreSQL / ClickHouse / Doris 等方言扩展（sqlglot 天然支持多方言，架构预留 dialect 参数）；
- 语义层可视化编辑界面；
- 生成 SQL 的自动修正循环（skill 内部受控重试：执行报错回喂重生成，最大次数可配上限，禁止模型自主无限循环）；
- SQL 缓存 TTL 随机抖动（防止同批 key 同步过期引发雪崩）；
- 新增 `invalid_datasource` 状态枚举（db_id 不存在或元数据加载失败时替代笼统的 error/no_schema）；
- 与 retrieve_skill 的联合路由节点（问题分类 → RAG/NL2SQL/融合）下沉为通用编排组件。
