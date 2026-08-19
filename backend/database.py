import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from models import Base

DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./sbami_platform.db"
# Render/Railway/Heroku-style providers hand out "postgres://" URLs;
# SQLAlchemy 1.4+ requires the "postgresql://" scheme.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db():
    Base.metadata.create_all(bind=engine)
    _auto_migrate()


def _auto_migrate():
    """Adds any columns present in the models but missing from the
    actual database - the common case after pulling a code update that
    added a field to an existing table. Only ever ADDs columns, never
    drops or alters existing ones, so it's safe to run on every
    startup rather than something that needs to be remembered and run
    by hand each time a model changes.

    The ALTER TABLE itself never carries a DEFAULT clause (only the
    column's SQL type), so every EXISTING row gets NULL in the new
    column regardless of the model's Python-side `default=`. That's
    silently wrong for a column like ActionConnector.requires_approval,
    whose whole point is "safe by default" - NULL reads as falsy, so
    without this backfill every connector that already existed before
    this column was added would flip to ungated, the opposite of the
    intent. Backfilling NULL -> the model's own default keeps old rows
    behaving the way a freshly-created row would."""
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=engine.dialect)
            with engine.connect() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
                conn.commit()
                if column.default is not None and column.default.is_scalar:
                    conn.execute(
                        text(f'UPDATE "{table.name}" SET "{column.name}" = :v WHERE "{column.name}" IS NULL'),
                        {"v": column.default.arg},
                    )
                    conn.commit()
