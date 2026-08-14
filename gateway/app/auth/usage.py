from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import astuple, dataclass

log = logging.getLogger(__name__)


@dataclass
class UsageRow:
    api_key_id: str
    endpoint: str
    cx: str | None
    query_hash: str | None
    query_text: str | None
    status: int
    cache_hit: bool
    upstream_pages: int
    latency_ms: int
    degraded: bool


class UsageLogger:
    """Bounded queue + a single batching drainer.

    Logging must never be on the response path and must never apply backpressure
    to a user's request. Two things follow from that:

      - On overflow we DROP the row and count it. A full queue means the database
        is slow; making users wait for our analytics would be the wrong trade.

      - We do NOT spawn a task per request. That is an unbounded task explosion
        under load, and tasks without a strong reference can be garbage-collected
        mid-flight, so the rows quietly vanish anyway.
    """

    def __init__(self, pool, maxsize: int, flush_seconds: float, flush_rows: int) -> None:
        self._pool = pool
        self._q: asyncio.Queue[UsageRow] = asyncio.Queue(maxsize=maxsize)
        self._flush_seconds = flush_seconds
        self._flush_rows = flush_rows
        self._task: asyncio.Task | None = None
        self.dropped = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._drain())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._flush(self._take_all())

    def record(self, row: UsageRow) -> None:
        try:
            self._q.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped % 100 == 1:
                log.warning("usage log saturated; dropped %d rows", self.dropped)

    def _take_all(self) -> list[UsageRow]:
        rows = []
        while not self._q.empty():
            rows.append(self._q.get_nowait())
        return rows

    async def _drain(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._flush_seconds)
                rows = []
                while not self._q.empty() and len(rows) < self._flush_rows:
                    rows.append(self._q.get_nowait())
                if rows:
                    await self._flush(rows)
            except asyncio.CancelledError:
                raise
            except Exception:  # a logging failure must never kill the drainer
                log.exception("usage flush failed")

    async def _flush(self, rows: list[UsageRow]) -> None:
        if not rows or self._pool is None:
            return
        await self._pool.executemany(
            """
            insert into api_usage (api_key_id, endpoint, cx, query_hash, query_text,
                                   status, cache_hit, upstream_pages, latency_ms, degraded)
            values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            [astuple(r) for r in rows],
        )
