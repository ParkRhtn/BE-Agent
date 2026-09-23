from pathlib import Path

from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from be_agent.db.models import Base


def create_engine(database_url: str) -> AsyncEngine:
    if database_url.startswith("sqlite"):
        path = database_url.split("///", 1)[-1]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(database_url)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


def _add_missing_columns(conn: Connection) -> None:
    """create_all 은 기존 테이블에 컬럼을 추가하지 않으므로, 새로 생긴 nullable 컬럼만 보충한다."""
    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing and column.nullable:
                col_type = column.type.compile(conn.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {col_type}'))


async def init_db(engine: AsyncEngine) -> None:
    # TODO: 스키마가 안정되면 Alembic 마이그레이션으로 전환
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
