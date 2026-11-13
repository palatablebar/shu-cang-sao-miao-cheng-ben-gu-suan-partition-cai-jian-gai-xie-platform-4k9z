"""OR 分支（支）级拆解：逐支命中分区 / 扫描量 / 费用 / 阻断原因 + 组并集汇总。

背景
----
运营查询常把多个条件用 OR 拼在一起（``dt = '…' OR order_status = 'PAID'``）。
只算一支的口径会把「日期支命中 1 个分区」当成整条查询的扫描量，而状态支
实际上要扫全表 —— 既低估费用，也会让局部改写发布后丢掉另一支的记录。

口径
----
* 支（branch）= 顶层 OR 的每一个析取支；支内部仍按 AND 展开求交。
* 支命中 = 支内全部可用分区谓词的交集；支内没有可用分区谓词 → 该支命中全部分区。
* 组命中 = 各支命中的**并集**（去重，按分区自然顺序排列）。
* 整条查询命中 = 各 OR 组并集 ∩ 其余（非 OR）约束的交集，由 ``sql_analyzer`` 汇总。
* 支费用 = 支扫描字节 × 单价；各支费用之和 ≥ 组费用（并集去重），
  展示层不得把任何单支费用当成整条费用。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlglot import exp

from src.analysis.predicates import (
    PartitionPredicate,
    build_predicate,
    partition_filter,
    split_and,
)
from src.constants.cost import scan_cost_cny

# 支级阻断原因：该支没有任何分区列谓词（复用 PruneBlockReason 的枚举值）
REASON_NON_PARTITION = "non_partition_filter"

REASON_LABEL = {
    "function_wrapped": "分区列被函数包裹",
    "implicit_cast": "隐式类型转换",
    "expression_not_usable": "表达式不可裁剪",
    "missing_predicate": "缺少分区谓词",
    "non_partition_filter": "分支不含分区列",
}


@dataclass
class BranchAnalysis:
    """一条 OR 支的完整估算结果（字段名与 API ``BranchOut`` 一一对应）。"""

    index: int
    expression: str
    touches_partition_key: bool
    prunable: bool
    blocked_reason: str
    blocked_expression: str | None
    predicates: list[dict]
    hit_partitions: list[str]
    hit_partition_count: int
    hit_bytes: int
    scan_bytes: int
    cost_cny: float
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OrGroupAnalysis:
    """一个 OR 组的逐支结果 + 并集汇总（字段名与 API ``OrGroupOut`` 一一对应）。"""

    index: int
    expression: str
    prunable: bool
    blocked_reason: str
    branches: list[BranchAnalysis]
    union_partitions: list[str]
    union_partition_count: int
    union_bytes: int
    scan_bytes: int
    cost_cny: float
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


def _predicate_payload(predicate: PartitionPredicate) -> dict:
    return {
        "expression": predicate.expression_sql,
        "operator": predicate.operator,
        "literal": predicate.literal_sql,
        "usable": predicate.usable,
        "reason": predicate.reason,
        "wrapper_kind": predicate.wrapper_kind,
    }


def analyze_branch(
    index: int,
    node: exp.Expression,
    profile,
    col_ratio: float,
    cost_per_gib_cny: float | None,
) -> BranchAnalysis:
    """估算一条 OR 支：支内 AND 求交；无可用分区谓词 → 该支扫全表。"""
    expression = node.sql(dialect="postgres")
    predicates: list[PartitionPredicate] = []
    for conjunct in split_and(node):
        predicate = build_predicate(conjunct, profile.partition_key.lower(), profile.partition_type)
        if predicate is not None:
            predicates.append(predicate)

    usable = [p for p in predicates if p.usable and p.evaluator is not None]
    blocked = [p for p in predicates if not p.usable]
    touches = bool(predicates)
    total = len(profile.partitions)

    if usable:
        filters = [partition_filter(p) for p in usable]
        hit = [dt for dt, _ in profile.partitions if all(fn(dt) for fn in filters if fn)]
        prunable = True
        blocked_reason = "none"
        blocked_expression = None
        note = f"裸列比较可裁剪 → 命中 {len(hit)}/{total} 个分区"
        if blocked:
            note += f"；支内另有 {len(blocked)} 个谓词不可用于裁剪（AND 语义下不影响命中）"
    elif touches:
        first = blocked[0]
        hit = [dt for dt, _ in profile.partitions]
        prunable = False
        blocked_reason = first.reason
        blocked_expression = first.expression_sql
        note = (
            f"分区谓词不可用（{REASON_LABEL.get(first.reason, first.reason)}）"
            f"→ 该支扫描全部 {total} 个分区"
        )
    else:
        hit = [dt for dt, _ in profile.partitions]
        prunable = False
        blocked_reason = REASON_NON_PARTITION
        blocked_expression = expression
        note = f"该支不含分区列 {profile.partition_key} 的谓词 → 该支扫描全部 {total} 个分区"

    bytes_by_dt = dict(profile.partitions)
    hit_bytes = sum(bytes_by_dt.get(dt, 0) for dt in hit)
    scan_bytes = int(round(hit_bytes * col_ratio))
    cost = scan_cost_cny(scan_bytes, cost_per_gib_cny) if cost_per_gib_cny is not None else 0.0
    return BranchAnalysis(
        index=index,
        expression=expression,
        touches_partition_key=touches,
        prunable=prunable,
        blocked_reason=blocked_reason,
        blocked_expression=blocked_expression,
        predicates=[_predicate_payload(p) for p in predicates],
        hit_partitions=hit,
        hit_partition_count=len(hit),
        hit_bytes=hit_bytes,
        scan_bytes=scan_bytes,
        cost_cny=cost,
        note=note,
    )


def analyze_or_group(
    index: int,
    node: exp.Expression,
    branch_nodes: list[exp.Expression],
    profile,
    col_ratio: float,
    cost_per_gib_cny: float | None,
) -> OrGroupAnalysis:
    """逐支估算后按分区并集汇总整个 OR 组。"""
    branches = [
        analyze_branch(i, branch, profile, col_ratio, cost_per_gib_cny)
        for i, branch in enumerate(branch_nodes)
    ]

    order = {dt: pos for pos, (dt, _) in enumerate(profile.partitions)}
    seen: set[str] = set()
    union: list[str] = []
    for branch in branches:
        for dt in branch.hit_partitions:
            if dt not in seen:
                seen.add(dt)
                union.append(dt)
    union.sort(key=lambda dt: order.get(dt, len(order)))

    bytes_by_dt = dict(profile.partitions)
    union_bytes = sum(bytes_by_dt.get(dt, 0) for dt in union)
    scan_bytes = int(round(union_bytes * col_ratio))
    cost = scan_cost_cny(scan_bytes, cost_per_gib_cny) if cost_per_gib_cny is not None else 0.0

    prunable = all(branch.prunable for branch in branches)
    blocking = next((branch for branch in branches if not branch.prunable), None)
    blocked_reason = "none" if prunable else (blocking.blocked_reason if blocking else "none")

    branch_notes = "；".join(
        f"支 #{branch.index + 1} 命中 {branch.hit_partition_count} 个分区"
        if branch.prunable
        else f"支 #{branch.index + 1} {REASON_LABEL.get(branch.blocked_reason, branch.blocked_reason)} → 全表"
        for branch in branches
    )
    note = (
        f"{branch_notes}；并集去重后 {len(union)}/{len(profile.partitions)} 个分区"
    )
    return OrGroupAnalysis(
        index=index,
        expression=node.sql(dialect="postgres"),
        prunable=prunable,
        blocked_reason=blocked_reason,
        branches=branches,
        union_partitions=union,
        union_partition_count=len(union),
        union_bytes=union_bytes,
        scan_bytes=scan_bytes,
        cost_cny=cost,
        note=note,
    )
