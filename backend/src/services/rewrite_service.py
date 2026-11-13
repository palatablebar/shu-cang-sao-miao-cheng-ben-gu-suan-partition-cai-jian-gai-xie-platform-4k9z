"""RewriteSuggestion service：算法 2 的落库、重估、采纳与 CSV 导出。"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from src.analysis.rewriter import RewriteResult, build_rewrite
from src.config.settings import settings
from src.constants.enums import SuggestionStatus
from src.constants.error_codes import AppError, ErrorCode
from src.constants.cost import GIB as _GIB, monthly_projection_cny
from src.constants.messages import MSG_QUERY_NOT_FOUND, MSG_SUGGESTION_NOT_FOUND
from src.models import QueryLog, RewriteSuggestion
from src.schemas.rewrite import RewriteDetail, RewriteOut, RewriteReevaluateOut
from src.services import serializers, table_service
from src.utils.csv_export import suggestions_csv
from src.utils.pagination import PageParams

GIB = 1024 ** 3

RISK_WEIGHTS = {
    "low": settings.RISK_WEIGHT_LOW,
    "medium": settings.RISK_WEIGHT_MEDIUM,
    "high": settings.RISK_WEIGHT_HIGH,
}


def suggestion_no_for(query: QueryLog) -> str:
    return f"RW-{query.external_id}"


def run_algorithm(db: Session, query: QueryLog, catalog=None) -> RewriteResult:
    catalog = catalog if catalog is not None else table_service.build_catalog(db)
    return build_rewrite(
        query.sql_text,
        catalog,
        window_days=settings.REWRITE_WINDOW_DAYS,
        cost_per_gib_cny=settings.COST_PER_GIB_CNY,
        risk_weights=RISK_WEIGHTS,
    )


def _apply(suggestion: RewriteSuggestion, query: QueryLog, result: RewriteResult) -> None:
    """整体写入一版完整改写结果（含逐支等价核对），不做半套更新。"""
    suggestion.query_log_id = query.id
    suggestion.anti_patterns = result.anti_patterns
    suggestion.risk_level = result.risk_level
    suggestion.confidence = result.confidence
    suggestion.before_scan_bytes = result.before_scan_bytes
    suggestion.after_scan_bytes = result.after_scan_bytes
    suggestion.saving_bytes = result.saving_bytes
    suggestion.saving_cost_cny = result.saving_cost_cny
    suggestion.saving_pct = result.saving_pct
    suggestion.score = result.score
    suggestion.rewritten_sql = result.rewritten_sql
    suggestion.rationale = result.rationale
    suggestion.publishable = result.publishable
    suggestion.branch_checks = result.branch_checks
    suggestion.publish_blockers = result.publish_blockers


def get_suggestion(db: Session, suggestion_id: int) -> RewriteSuggestion:
    suggestion = db.scalar(
        select(RewriteSuggestion)
        .options(selectinload(RewriteSuggestion.query))
        .where(RewriteSuggestion.id == suggestion_id)
    )
    if suggestion is None:
        raise AppError(ErrorCode.NOT_FOUND, MSG_SUGGESTION_NOT_FOUND)
    return suggestion


def detail(db: Session, suggestion_id: int) -> RewriteDetail:
    suggestion = get_suggestion(db, suggestion_id)
    query = suggestion.query
    result = run_algorithm(db, query)
    extra = {
        "fixes": result.fixes,
        "before_rationale": result.before.rationale if result.before else "",
        "after_rationale": result.after.rationale if result.after else "",
        "after_prunable": bool(result.after and result.after.prunable),
        "after_hit_partition_count": result.after.hit_partition_count if result.after else 0,
        "after_total_partition_count": result.after.total_partition_count if result.after else 0,
        "branch_checks": result.branch_checks or (suggestion.branch_checks or []),
        "publish_blockers": result.publish_blockers or (suggestion.publish_blockers or []),
    }
    # 元数据变化可能让旧建议的最新核对转为不可发布：实时结果优先展示。
    if result.publish_blockers:
        extra["publishable"] = False
    return serializers.rewrite_detail(suggestion, extra)


def generate_for_query(db: Session, query_id: int, actor: str) -> RewriteDetail:
    query = db.get(QueryLog, query_id)
    if query is None:
        raise AppError(ErrorCode.NOT_FOUND, MSG_QUERY_NOT_FOUND)
    catalog = table_service.build_catalog(db)
    result = run_algorithm(db, query, catalog)
    if result.publish_blockers:
        # 任一支无法证明等价 → 整条建议不予发布；已存在的旧版本建议原样保留。
        raise AppError(
            ErrorCode.CONFLICT,
            f"{query.external_id} 存在无法证明等价的 OR 分支，整条建议不予发布："
            + "；".join(result.publish_blockers),
            {"publish_blockers": result.publish_blockers, "branch_checks": result.branch_checks},
        )
    if not result.rewritable:
        raise AppError(
            ErrorCode.CONFLICT,
            f"{query.external_id} 当前命中不到可改写项：{result.note or '节省比例不足 5%'}",
            {"anti_patterns": result.anti_patterns, "saving_pct": result.saving_pct},
        )
    suggestion = db.scalar(
        select(RewriteSuggestion).where(RewriteSuggestion.suggestion_no == suggestion_no_for(query))
    )
    if suggestion is None:
        suggestion = RewriteSuggestion(
            suggestion_no=suggestion_no_for(query),
            query_log_id=query.id,
            status=SuggestionStatus.PENDING.value,
        )
        db.add(suggestion)
    else:
        suggestion.reviewed_by = None
        suggestion.reviewed_at = None
    _apply(suggestion, query, result)
    suggestion.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(suggestion)
    return detail(db, suggestion.id)


def rebuild_suggestions(db: Session, limit: int = 12) -> int:
    """幂等：按分数取前 limit 条写入（自然键 suggestion_no），已存在的不覆盖。

    只收纳 publishable 的建议 —— 任一支无法证明等价的改写整条不予发布。
    """
    catalog = table_service.build_catalog(db)
    queries = list(
        db.scalars(select(QueryLog).where(QueryLog.prunable.is_(False), QueryLog.parse_status == "parsed"))
    )
    scored = []
    for query in queries:
        result = run_algorithm(db, query, catalog)
        if result.rewritable and result.publishable:
            scored.append((query, result))
    scored.sort(key=lambda item: (-item[1].score, item[0].external_id))
    created = 0
    for query, result in scored[:limit]:
        key = suggestion_no_for(query)
        if db.scalar(select(RewriteSuggestion).where(RewriteSuggestion.suggestion_no == key)) is not None:
            continue
        suggestion = RewriteSuggestion(
            suggestion_no=key,
            query_log_id=query.id,
            status=SuggestionStatus.PENDING.value,
        )
        _apply(suggestion, query, result)
        db.add(suggestion)
        created += 1
    db.commit()
    return created


def top_suggestions(db: Session, limit: int = 3) -> list[RewriteOut]:
    return list_suggestions(db, PageParams(page=1, page_size=limit))[0]


def list_suggestions(
    db: Session,
    params: PageParams,
    status: str | None = None,
    sort: str = "score",
) -> tuple[list[RewriteOut], int]:
    stmt = select(RewriteSuggestion).options(selectinload(RewriteSuggestion.query))
    count_stmt = select(func.count(RewriteSuggestion.id))
    if status:
        stmt = stmt.where(RewriteSuggestion.status == status)
        count_stmt = count_stmt.where(RewriteSuggestion.status == status)
    column = {
        "score": RewriteSuggestion.score,
        "saving_bytes": RewriteSuggestion.saving_bytes,
        "saving_pct": RewriteSuggestion.saving_pct,
    }.get(sort, RewriteSuggestion.score)
    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(
        stmt.order_by(column.desc(), RewriteSuggestion.id.asc()).offset(params.offset).limit(params.limit)
    )
    return [serializers.rewrite_item(row) for row in rows], total


def reevaluate(db: Session, suggestion_id: int) -> RewriteReevaluateOut:
    """对改写后的 SQL 重跑算法 1，用真实重估替代拍脑袋的收益数字。

    重估失败（不再可改写 / 出现无法证明等价的支）时直接抛错回滚，
    旧版本的完整分析（含逐支核对结果）原样保留。
    """
    suggestion = get_suggestion(db, suggestion_id)
    query = suggestion.query
    catalog = table_service.build_catalog(db)
    result = run_algorithm(db, query, catalog)
    if result.publish_blockers:
        raise AppError(
            ErrorCode.CONFLICT,
            "重估失败：改写后出现无法证明等价的 OR 分支，旧版本建议保持不变："
            + "；".join(result.publish_blockers),
            {"publish_blockers": result.publish_blockers, "branch_checks": result.branch_checks},
        )
    if not result.rewritable:
        raise AppError(ErrorCode.CONFLICT, f"重估失败：{result.note or '改写后不再命中反模式'}")
    _apply(suggestion, query, result)
    suggestion.updated_at = datetime.now(timezone.utc)
    db.commit()
    return RewriteReevaluateOut(
        suggestion_no=suggestion.suggestion_no,
        before_scan_bytes=result.before_scan_bytes,
        after_scan_bytes=result.after_scan_bytes,
        saving_bytes=result.saving_bytes,
        saving_pct=result.saving_pct,
        saving_cost_cny=result.saving_cost_cny,
        monthly_saving_cny=monthly_projection_cny(
            result.saving_cost_cny, settings.MONTHLY_QUERY_DAYS, settings.QUERY_VOLUME_SCALE
        ),
        after_prunable=bool(result.after and result.after.prunable),
        after_hit_partition_count=result.after.hit_partition_count if result.after else 0,
        after_total_partition_count=result.after.total_partition_count if result.after else 0,
        rationale=result.rationale,
    )


def adopt(db: Session, suggestion_id: int, actor: str) -> RewriteOut:
    suggestion = get_suggestion(db, suggestion_id)
    if suggestion.status == SuggestionStatus.ADOPTED.value:
        return serializers.rewrite_item(suggestion)
    # 发布闸：采纳前重新逐支核对（元数据可能已变化）——
    # 任一支无法证明等价 → 整条建议不能发布/采纳。
    result = run_algorithm(db, suggestion.query)
    if result.publish_blockers:
        raise AppError(
            ErrorCode.CONFLICT,
            "任一支无法证明等价，整条建议不能发布/采纳：" + "；".join(result.publish_blockers),
            {"publish_blockers": result.publish_blockers, "branch_checks": result.branch_checks},
        )
    if not result.rewritable:
        raise AppError(
            ErrorCode.CONFLICT,
            f"改写已不再命中反模式或节省不足，建议不能采纳：{result.note or '请重估后再试'}",
        )
    suggestion.status = SuggestionStatus.ADOPTED.value
    suggestion.reviewed_by = actor
    suggestion.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(suggestion)
    return serializers.rewrite_item(suggestion)


def export_rows(db: Session) -> list[dict]:
    rows, _ = list_suggestions(db, PageParams(page=1, page_size=200))
    return [row.model_dump() for row in rows]


def export_csv(db: Session) -> tuple[str, int]:
    rows = export_rows(db)
    return suggestions_csv(rows), len(rows)
