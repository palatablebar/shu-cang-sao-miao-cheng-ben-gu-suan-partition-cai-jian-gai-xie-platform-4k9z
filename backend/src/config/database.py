"""Engine / session factory shared by the FastAPI app and the Celery worker."""
import time

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config.settings import settings


class Base(DeclarativeBase):
    pass


# 幂等列级升级：create_all 不会给已存在的表补新列，这里用 IF NOT EXISTS 兜底，
# 与 migrations/0002_or_branches.sql 保持一致。
COLUMN_UPGRADES = [
    "ALTER TABLE query_log ADD COLUMN IF NOT EXISTS or_groups JSONB NOT NULL DEFAULT '[]'",
    "ALTER TABLE query_log ADD COLUMN IF NOT EXISTS or_group_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE rewrite_suggestion ADD COLUMN IF NOT EXISTS publishable BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE rewrite_suggestion ADD COLUMN IF NOT EXISTS branch_checks JSONB NOT NULL DEFAULT '[]'",
    "ALTER TABLE rewrite_suggestion ADD COLUMN IF NOT EXISTS publish_blockers JSONB NOT NULL DEFAULT '[]'",
]


engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=8,
    max_overflow=4,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(max_attempts: int = 30, delay: float = 2.0) -> None:
    """Create all tables, waiting for PostgreSQL to finish its first boot."""
    import src.models  # noqa: F401

    last_error = None
    for _ in range(max_attempts):
        try:
            Base.metadata.create_all(bind=engine)
            with engine.begin() as connection:
                for statement in COLUMN_UPGRADES:
                    connection.execute(text(statement))
            return
        except Exception as exc:  # pragma: no cover - infra retry
            last_error = exc
            time.sleep(delay)
    raise RuntimeError(f"database bootstrap failed: {last_error}")
