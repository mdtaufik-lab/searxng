from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger(__name__)


async def create_pool(dsn: str):
    """asyncpg, not supabase-py: the latter is sync and unpooled, and this whole
    stack is async. Supabase's pooler wants prepared statements disabled."""
    return await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=10,
        statement_cache_size=0,  # required behind PgBouncer in transaction mode
        command_timeout=10,
    )
