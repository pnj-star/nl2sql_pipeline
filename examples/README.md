# examples：农产品佣金查询完整示例

一套可直接跑通的 `nl2sql_skill` 用法：用户问"8月黑木耳佣金多少"，
pipeline 自动读 MySQL 元数据 → 语义层/示例库召回 → 生成 SQL → 护栏校验 →
执行返回佣金结果。全套不依赖 Milvus / 向量库。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `sql/schema.sql` | 建 `commerce` 库 + `products` / `commission_rules` 表 + 示例数据（黑木耳/香菇/银耳/红枣） |
| `semantic/commission.yaml` | 语义层：同义词（佣金/提成/抽成/返点）+ 字段说明，解决"抽成"等问法命中问题 |
| `few_shots/samples.jsonl` | few-shot 示例库：已验证的问答样例，生成时注入最相似的做参照 |
| `.env.example` | 配置模板：LLM + test-mysql 只读 DSN + 语义层/示例库路径 |
| `run_demo.py` | 可运行演示脚本（真实 LLM 或 `--fake` 两种模式） |

## 三步跑通

```bash
# 1) 建库建表插数据（在你 DataGrip / mysql 客户端执行）
#    test-mysql: 127.0.0.1:3307，登录后执行 sql/schema.sql 全部内容

# 2) 复制配置并填只读账号（LLM 密钥可先留空，用 --fake 模式）
cp examples/.env.example examples/.env

# 3) 运行（二选一）
python examples/run_demo.py --fake      # 不配密钥：生成用预置 SQL，元数据/语义层/护栏/执行全真实
python examples/run_demo.py             # 真实 LLM：先在 examples/.env 填 NL2SQL_LLM_BASE_URL/API_KEY/MODEL
```

依赖：`pymysql`（元数据采集与 execute=true 需要，`pip install pymysql`）；
真实模式还需要可访问的 OpenAI 兼容 LLM 网关。

## 看完结果可以怎么验证

- 问"香菇的抽成是多少"验证语义层同义词（samples.jsonl + commission.yaml 生效）；
- 改 `semantic/commission.yaml` 加同义词后重跑，看召回命中变化；
- 执行真实 LLM 模式对比预置 SQL 与模型生成 SQL 的差异。