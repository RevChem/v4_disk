from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

# SQLite: в uvicorn несколько потоков — иначе «SQLite objects created in a thread...»
# timeout (сек.) — сколько ждать при «database is locked» (генерации + запросы API в одном файле).
_engine_kwargs = {}
if settings.database_url.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}

engine = create_engine(settings.database_url, future=True, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def _add_column_if_missing(table: str, column: str, ddl_sqlite: str, ddl_other: str) -> None:
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table(table):
        return
    cols = {c["name"] for c in insp.get_columns(table)}
    if column in cols:
        return
    ddl = ddl_sqlite if settings.database_url.startswith("sqlite") else ddl_other
    with engine.begin() as conn:
        conn.execute(text(ddl))


def run_startup_schema_fixes() -> None:
    """Добавляет столбцы к существующей SQLite/БД без Alembic (create_all не меняет таблицы)."""
    try:
        _add_column_if_missing(
            "users",
            "full_name",
            "ALTER TABLE users ADD COLUMN full_name VARCHAR(255)",
            "ALTER TABLE users ADD COLUMN full_name VARCHAR(255)",
        )
        _add_column_if_missing(
            "summaries",
            "visible",
            "ALTER TABLE summaries ADD COLUMN visible BOOLEAN NOT NULL DEFAULT 1",
            "ALTER TABLE summaries ADD COLUMN visible BOOLEAN NOT NULL DEFAULT true",
        )
        _add_column_if_missing(
            "generation_jobs",
            "cancel_requested",
            "ALTER TABLE generation_jobs ADD COLUMN cancel_requested BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE generation_jobs ADD COLUMN cancel_requested BOOLEAN NOT NULL DEFAULT false",
        )
        _add_column_if_missing(
            "generation_jobs",
            "cancel_report_ids",
            "ALTER TABLE generation_jobs ADD COLUMN cancel_report_ids TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE generation_jobs ADD COLUMN cancel_report_ids TEXT NOT NULL DEFAULT '[]'",
        )
    except Exception:
        # Не валим приложение: при другом движке/правах админ добавит столбец вручную.
        pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
