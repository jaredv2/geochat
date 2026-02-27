import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

db_pool = None

async def init_db():
    """Initialize the asyncpg database connection pool."""
    global db_pool
    db_pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"))

async def get_db():
    """Dependency to yield a database connection for requests."""
    async with db_pool.acquire() as conn:
        yield conn
