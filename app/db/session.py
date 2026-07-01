from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool
from app.core.config import get_settings

settings = get_settings()

_is_sqlite = "sqlite" in settings.database_url
_is_sqlite_memory = _is_sqlite and ":memory:" in settings.database_url

_engine_kwargs = {"echo": settings.database_echo}
if _is_sqlite:
    # For SQLite (dev), for Postgres use pool_size, max_overflow etc.
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
if _is_sqlite_memory:
    # A plain in-memory sqlite db is per-connection; StaticPool keeps every
    # session on the same connection so tables created in one request are
    # visible to the next (used by the test suite).
    _engine_kwargs["poolclass"] = StaticPool

engine = create_async_engine(settings.database_url, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def create_all_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
