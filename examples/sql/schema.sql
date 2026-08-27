-- ============================================================
-- nl2sql_skill 完整示例：建库建表 + 示例数据（农产品佣金场景）
-- 用法：在 test-mysql（127.0.0.1:3307）的任意 Console 整段执行
-- 注意：这是一份可重复执行的示例脚本，会重建 commerce 库里的
--       products / commission_rules 两张表，示例数据以本文件为准。
-- ============================================================

CREATE DATABASE IF NOT EXISTS commerce
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE commerce;

DROP TABLE IF EXISTS commission_rules;
DROP TABLE IF EXISTS products;

CREATE TABLE products (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '产品ID',
  name        VARCHAR(128)    NOT NULL COMMENT '产品名称',
  category    VARCHAR(64)     NOT NULL DEFAULT '' COMMENT '产品类目，如 农特产品/干货',
  status      TINYINT         NOT NULL DEFAULT 1 COMMENT '状态：1=在售 0=停售',
  created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
              ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  KEY idx_name (name),
  KEY idx_category (category)
) ENGINE=InnoDB COMMENT='产品主表';

CREATE TABLE commission_rules (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '佣金规则ID',
  product_id     BIGINT UNSIGNED NOT NULL COMMENT '产品ID，关联 products.id',
  rule_type      TINYINT         NOT NULL DEFAULT 1 COMMENT '佣金类型：1=按销售额比例 2=每件固定金额',
  rate           DECIMAL(8,4)    DEFAULT NULL COMMENT '佣金比例（rule_type=1 时生效，0.0500=5%）',
  flat_amount    DECIMAL(12,2)   DEFAULT NULL COMMENT '单件固定佣金（rule_type=2 时生效，单位元）',
  effective_from DATE            NOT NULL COMMENT '生效起始日期（含）',
  effective_to   DATE            DEFAULT NULL COMMENT '生效截止日期（含）；NULL=长期有效',
  created_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  KEY idx_product_eff (product_id, effective_from),
  CONSTRAINT fk_comm_product FOREIGN KEY (product_id) REFERENCES products (id)
) ENGINE=InnoDB COMMENT='产品佣金/提成/抽成规则表';

INSERT INTO products (id, name, category) VALUES
(1, '黑木耳', '农特产品'),
(2, '香菇',   '农特产品'),
(3, '银耳',   '农特产品'),
(4, '红枣',   '农特产品');

-- 黑木耳/香菇/红枣按销售额比例抽佣，银耳按件固定佣金
INSERT INTO commission_rules (product_id, rule_type, rate, flat_amount, effective_from, effective_to) VALUES
(1, 1, 0.0500, NULL,  '2026-08-01', '2026-08-31'),
(2, 1, 0.0600, NULL,  '2026-08-01', '2026-08-31'),
(3, 2, NULL,   3.0000, '2026-08-01', '2026-08-31'),
(4, 1, 0.0800, NULL,  '2026-08-01', '2026-08-31');