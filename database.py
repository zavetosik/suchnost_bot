import asyncio

from psycopg_pool import AsyncConnectionPool

from config import BASE_DIR, DATABASE_URL


pool = AsyncConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=5,
    open=False,
    kwargs={
        "autocommit": True,
        "connect_timeout": 15,
        "prepare_threshold": None,
    },
    check=AsyncConnectionPool.check_connection,
)


async def init_db():
    if not DATABASE_URL:
        raise RuntimeError("Укажи DATABASE_URL из Neon в файле suchnost_bot/.env")

    await pool.open(wait=True, timeout=30)
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute((BASE_DIR / "schema.sql").read_text(encoding="utf-8"))


async def close_db():
    await pool.close()


async def execute(query, params=()):
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(query, params)
            return cursor.rowcount


async def fetchone(query, params=()):
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchone()


async def fetchall(query, params=()):
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchall()


async def main():
    try:
        await init_db()
        print("Подключение к PostgreSQL успешно. Таблицы готовы.")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
