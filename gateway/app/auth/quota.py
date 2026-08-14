from __future__ import annotations

from datetime import date


def _period_start(period: str, today: date) -> date:
    return today.replace(day=1) if period == "month" else today


class Quota:
    """Durable, billing-grade counting in Postgres.

    Kept out of Redis on purpose: billing state should be durable, and the extra
    round trip (2-5ms) is irrelevant next to a 500-3000ms SearXNG fetch.
    """

    def __init__(self, pool) -> None:
        self._pool = pool

    async def consume(self, key_id: str, period: str, limit: int | None) -> tuple[bool, int]:
        """-> (allowed, used_after). Returns (True, 0) when the key is unlimited."""
        if limit is None:
            return True, 0

        ps = _period_start(period, date.today())

        # One statement, fully race-free. The `used < limit` predicate is
        # evaluated under the row lock taken by the UPDATE, so concurrent
        # requests serialize and the counter can NEVER overshoot the limit.
        #
        # The obvious alternative -- read the counter, compare, then increment --
        # lets every concurrent request read the same pre-increment value and
        # sail past the limit together.
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    insert into api_quota_counters (api_key_id, period_start, used)
                    values ($1::uuid, $2, 0)
                    on conflict (api_key_id, period_start) do nothing
                    """,
                    key_id,
                    ps,
                )
                row = await conn.fetchrow(
                    """
                    update api_quota_counters
                       set used = used + 1
                     where api_key_id = $1::uuid
                       and period_start = $2
                       and used < $3
                    returning used
                    """,
                    key_id,
                    ps,
                    limit,
                )

        if row is None:  # zero rows updated == the predicate failed == over quota
            return False, limit
        return True, row["used"]
