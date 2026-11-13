"""QueryLog schemas — 解析结果字段与算法 1 输出一一对应。"""
from pydantic import BaseModel


class PredicateOut(BaseModel):
    expression: str
    operator: str
    literal: str
    usable: bool
    reason: str
    wrapper_kind: str | None = None


class BranchOut(BaseModel):
    """一条 OR 支的估算结果（命中分区 / 扫描量 / 费用 / 阻断原因）。"""

    index: int
    expression: str
    touches_partition_key: bool
    prunable: bool
    blocked_reason: str
    blocked_expression: str | None = None
    predicates: list[PredicateOut] = []
    hit_partitions: list[str] = []
    hit_partition_count: int = 0
    hit_bytes: int = 0
    scan_bytes: int = 0
    cost_cny: float = 0.0
    note: str = ""


class OrGroupOut(BaseModel):
    """一个 OR 组：逐支结果 + 分区并集汇总。"""

    index: int
    expression: str
    prunable: bool
    blocked_reason: str
    branches: list[BranchOut] = []
    union_partitions: list[str] = []
    union_partition_count: int = 0
    union_bytes: int = 0
    scan_bytes: int = 0
    cost_cny: float = 0.0
    note: str = ""


class QueryListItem(BaseModel):
    id: int
    external_id: str
    title: str
    source: str
    submitted_by: str
    query_date: str
    duration_ms: int
    is_scheduled: bool
    parse_status: str
    target_table: str | None
    prunable: bool
    blocked_reason: str
    scan_bytes: int
    scan_cost_cny: float
    hit_partition_count: int
    total_partition_count: int
    col_ratio: float
    or_group_count: int = 0


class QueryDetail(QueryListItem):
    sql_text: str
    parse_error: str | None
    blocked_expression: str | None
    predicates: list[PredicateOut]
    or_groups: list[OrGroupOut] = []
    hit_partitions: list[str]
    projected_columns: list[str]
    referenced_columns: list[str]
    has_star: bool
    full_scan_bytes: int
    rationale: str
    suggestion_count: int = 0


class QueryImportResult(BaseModel):
    imported: int
    updated: int
    total: int
    analysed: int
    blocked: int
    message: str
