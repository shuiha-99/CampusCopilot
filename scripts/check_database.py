import asyncio

from sqlalchemy import text

from app.db.session import engine


async def main() -> None:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT DATABASE(), CURRENT_USER(), 1"))
        row = result.one()

        print(
            {
                "database": row[0],
                "current_user": row[1],
                "probe": row[2],
            }
        )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
