"""数据库引擎与会话管理。"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_size=20)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI 依赖注入：获取异步数据库会话。"""
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def check_db() -> bool:
    """启动时验证数据库连接。"""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
