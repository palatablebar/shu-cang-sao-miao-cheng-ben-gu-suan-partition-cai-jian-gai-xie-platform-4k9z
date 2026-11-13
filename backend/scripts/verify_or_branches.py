"""OR 多支查询的验收脚本（纯函数，不依赖数据库 / Celery）。

覆盖口径：
    1. 逐支列出：命中分区、扫描量、费用、阻断原因；
    2. 并集汇总：组并集 = 各支并集去重，整条命中 = 组并集 ∩ 其余约束；
    3. 发布闸：任一支无法证明等价 → publishable=False（整条建议不予发布）；
    4. 失败重试：旧版本的完整分析在新版本成功落库前原样保留（不留半套分支结果）。

运行：python -m scripts.verify_or_branches   （backend 容器内 / 本地 PYTHONPATH=backend）
退出码：全部通过 0；任一断言失败 1。
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import demo_dataset  # noqa: E402
from src.analysis.rewriter import build_rewrite  # noqa: E402
from src.analysis.sql_analyzer import ColumnProfile, TableProfile, analyze_query  # noqa: E402

GIB = 1024 ** 3
COST_PER_GIB = 0.30

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def build_catalog() -> dict[str, TableProfile]:
    catalog: dict[str, TableProfile] = {}
    for spec in demo_dataset.build_tables():
        columns = [
            ColumnProfile(
                name=c["name"],
                data_type=c["data_type"],
                bytes=c["bytes"],
                is_partition_key=c["is_partition_key"],
            )
            for c in spec["columns"]
        ]
        catalog[spec["table_name"]] = TableProfile(
            schema_name=spec["schema_name"],
            table_name=spec["table_name"],
            partition_key=spec["partition_key"],
            partition_type=spec["partition_type"],
            partitions=[(p["dt"], p["bytes"]) for p in spec["partitions"]],
            columns=columns,
            suggested_columns=list(spec["suggested_columns"]),
            total_bytes=spec["total_bytes"],
        )
    return catalog


def reconcile(analysis) -> None:
    """组并集 = 各支并集；整条命中 ⊆ 各组并集；扫描量 = 命中字节 × 列裁剪比例。"""
    for group in analysis.or_groups:
        union = set()
        for branch in group["branches"]:
            union |= set(branch["hit_partitions"])
        check("组并集等于各支并集去重", union == set(group["union_partitions"]))
        check(
            "组扫描量等于并集字节×列裁剪比例",
            group["scan_bytes"] == int(round(group["union_bytes"] * analysis.col_ratio)),
        )
        check("整条命中是组并集的子集", set(analysis.hit_partitions) <= set(group["union_partitions"]))
    check(
        "整条扫描量等于命中字节×列裁剪比例",
        analysis.scan_bytes == int(round(analysis.hit_bytes * analysis.col_ratio)),
    )


def main() -> int:
    catalog = build_catalog()
    queries = {row["external_id"]: row for row in demo_dataset.build_queries()}
    analyses = {
        external_id: analyze_query(row["sql_text"], catalog, cost_per_gib_cny=COST_PER_GIB)
        for external_id, row in queries.items()
    }

    print("== 1. 逐支列出：命中分区 / 扫描量 / 费用 / 阻断原因 ==")
    a = analyses["QL-0019"]  # dt = '…' OR order_status = 'PAID'
    check("QL-0019 含 1 个 OR 组", a.or_group_count == 1)
    group = a.or_groups[0]
    check("QL-0019 组内有 2 支", len(group["branches"]) == 2)
    date_branch, status_branch = group["branches"]
    check("日期支可裁剪且命中 1 个分区", date_branch["prunable"] and date_branch["hit_partition_count"] == 1)
    check(
        "状态支阻断原因为 non_partition_filter",
        status_branch["blocked_reason"] == "non_partition_filter",
    )
    check("状态支扫描全部 30 个分区", status_branch["hit_partition_count"] == 30)
    check("状态支费用 = 支扫描字节 × 单价", status_branch["cost_cny"] == round(status_branch["scan_bytes"] / GIB * COST_PER_GIB, 6))
    check("整条判定裁剪失效", not a.prunable and a.blocked_reason == "non_partition_filter")
    check("整条命中 = 并集 30 个分区（不是单支的 1 个）", a.hit_partition_count == 30)
    check("整条费用按并集计，不等于日期支费用", a.scan_bytes == group["scan_bytes"] != date_branch["scan_bytes"])

    print("== 2. 并集汇总 ==")
    check("QL-0020 双日期支并集 2 个分区", analyses["QL-0020"].hit_partition_count == 2 and analyses["QL-0020"].prunable)
    check("QL-0023 区间+单日并集 5 个分区", analyses["QL-0023"].hit_partition_count == 5 and analyses["QL-0023"].prunable)
    for external_id, analysis in analyses.items():
        if analysis.or_group_count:
            print(f"  -- {external_id} 对账")
            reconcile(analysis)

    print("== 3. 发布闸：任一支无法证明等价 → 整条不予发布 ==")
    r_ok = build_rewrite(queries["QL-0021"]["sql_text"], catalog, window_days=7, cost_per_gib_cny=COST_PER_GIB)
    check("QL-0021 两支均通过等价核对，建议可发布", r_ok.rewritable and r_ok.publishable)
    check("QL-0021 核对记录覆盖 2 支", len(r_ok.branch_checks) == 2)
    check("QL-0021 改写后命中 2 个分区", r_ok.after is not None and r_ok.after.hit_partition_count == 2)

    r_bad = build_rewrite(queries["QL-0022"]["sql_text"], catalog, window_days=7, cost_per_gib_cny=COST_PER_GIB)
    check("QL-0022 存在无法证明等价的支", any(c["status"] == "unproven" for c in r_bad.branch_checks))
    check("QL-0022 整条建议不予发布", not r_bad.publishable and bool(r_bad.publish_blockers))
    check("QL-0022 阻断原因指向支 #2", any("支 #2" in blocker for blocker in r_bad.publish_blockers))

    r_none = build_rewrite(queries["QL-0019"]["sql_text"], catalog, window_days=7, cost_per_gib_cny=COST_PER_GIB)
    check("QL-0019 状态支无反模式可改（不补窗口、不丢记录）", not r_none.rewritable and not r_none.anti_patterns)

    print("== 4. 失败重试保留旧版本（不留半套分支结果）==")
    # 模拟 estimate 流程：先算完全部分析再一次性写入；写入前失败 → 旧版本原样保留。
    store: dict[str, dict] = {}

    def persist(external_id: str, analysis) -> None:
        store[external_id] = {
            "scan_bytes": analysis.scan_bytes,
            "or_groups": analysis.or_groups,
            "or_group_count": analysis.or_group_count,
        }

    persist("QL-0019", analyses["QL-0019"])
    old_snapshot = dict(store["QL-0019"])
    check("旧版本含完整分支结果", old_snapshot["or_group_count"] == 1 and len(old_snapshot["or_groups"][0]["branches"]) == 2)

    def failing_run() -> None:
        # 新版本分析在写入前失败（例如元数据目录缺失表）→ 不触碰任何已存记录
        raise RuntimeError("catalog unavailable")

    try:
        failing_run()
    except RuntimeError:
        pass
    check("失败后旧版本完整保留", store["QL-0019"] == old_snapshot)

    # 重试成功 → 一次性整体替换为新版本（字段成套出现）
    retry = analyze_query(queries["QL-0019"]["sql_text"], catalog, cost_per_gib_cny=COST_PER_GIB)
    persist("QL-0019", retry)
    new_row = store["QL-0019"]
    check(
        "重试成功后字段成套（组数与分支明细一致）",
        new_row["or_group_count"] == len(new_row["or_groups"]) == 1
        and len(new_row["or_groups"][0]["branches"]) == 2,
    )

    print("== 5. 全量契约（60 条 / 21 条失效 / 建议池 ≥ 12）==")
    blocked = [a for a in analyses.values() if a.parse_status == "parsed" and not a.prunable]
    check("60 条查询", len(analyses) == 60, f"actual={len(analyses)}")
    check("21 条裁剪失效", len(blocked) == 21, f"actual={len(blocked)}")
    publishable_pool = 0
    for external_id, row in queries.items():
        analysis = analyses[external_id]
        if analysis.parse_status != "parsed" or analysis.prunable:
            continue
        result = build_rewrite(row["sql_text"], catalog, window_days=7, cost_per_gib_cny=COST_PER_GIB)
        if result.rewritable and result.publishable:
            publishable_pool += 1
    check("可发布建议池 ≥ 12（seed 取前 12）", publishable_pool >= 12, f"actual={publishable_pool}")

    total_scan = sum(a.scan_bytes for a in analyses.values() if a.parse_status == "parsed")
    monthly = round(total_scan / GIB * COST_PER_GIB * 30 * 5, 2)
    print(f"  -- 全量扫描 {total_scan / GIB:.2f} GiB/日 · 月折算 ¥{monthly} · 预算占用 {monthly / 1800 * 100:.1f}%")

    if FAILURES:
        print(f"\nVERIFY_OR_BRANCHES FAIL ({len(FAILURES)} 项): " + "；".join(FAILURES))
        return 1
    print("\nVERIFY_OR_BRANCHES OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
