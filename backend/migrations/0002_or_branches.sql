-- partfit / 0002 OR 分支逐支分析（在 0001_initial.sql 之上追加）
-- 运行时由 src.config.database.init_db() 以 IF NOT EXISTS 幂等执行（COLUMN_UPGRADES），
-- 此文件为可评审的 DDL 快照；新装环境由 create_all 直接建出全量列。

-- 查询日志：OR 组逐支分析结果（命中分区 / 扫描量 / 费用 / 阻断原因）与组数
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS or_groups JSONB NOT NULL DEFAULT '[]';
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS or_group_count INTEGER NOT NULL DEFAULT 0;

-- 改写建议：逐支等价核对结果与发布闸（任一支无法证明等价 → publishable = false）
ALTER TABLE rewrite_suggestion ADD COLUMN IF NOT EXISTS publishable BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE rewrite_suggestion ADD COLUMN IF NOT EXISTS branch_checks JSONB NOT NULL DEFAULT '[]';
ALTER TABLE rewrite_suggestion ADD COLUMN IF NOT EXISTS publish_blockers JSONB NOT NULL DEFAULT '[]';
