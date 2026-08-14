"""Admin CLI:  python -m app.cli --help"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import typer

from .auth.keys import generate_key
from .config import get_settings
from .db.pool import create_pool

app = typer.Typer(help="Manage search-gateway API keys.", no_args_is_help=True)
keys = typer.Typer(help="Create, list and revoke API keys.", no_args_is_help=True)
app.add_typer(keys, name="keys")


def _pool():
    s = get_settings()
    if not s.database_url:
        typer.secho("DATABASE_URL is not set. Key management needs the database.", fg="red")
        raise typer.Exit(1)
    return create_pool(s.database_url)


@keys.command("init")
def init_db() -> None:
    """Apply the schema (safe to re-run)."""

    async def go():
        from pathlib import Path

        sql = (Path(__file__).parent / "db" / "migrations" / "001_init.sql").read_text()
        pool = await _pool()
        await pool.execute(sql)
        await pool.close()
        typer.secho("schema applied", fg="green")

    asyncio.run(go())


@keys.command("create")
def create(
    name: str = typer.Option(..., help="Which app this key is for, e.g. 'blog-frontend'"),
    rpm: int = typer.Option(60, help="Requests per minute"),
    burst: int = typer.Option(20, help="Burst allowance"),
    quota: int = typer.Option(None, help="Max requests per period (omit = unlimited)"),
    period: str = typer.Option("month", help="day | month"),
    expires_days: int = typer.Option(None, help="Expire after N days"),
    cx: list[str] = typer.Option(None, "--cx", help="Restrict to these profiles (repeatable)"),
) -> None:
    async def go():
        raw, key_hash, prefix = generate_key()
        expires = (
            datetime.now(timezone.utc) + timedelta(days=expires_days) if expires_days else None
        )
        pool = await _pool()
        await pool.execute(
            """
            insert into api_keys (key_hash, key_prefix, name, cx_allowlist, rate_limit_rpm,
                                  rate_limit_burst, quota_period, quota_limit, expires_at)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            key_hash, prefix, name, list(cx) if cx else None, rpm, burst, period, quota, expires,
        )
        await pool.close()

        typer.secho("\n  API key created. It is shown ONCE and never stored in full.\n", fg="green")
        typer.secho(f"  {raw}\n", fg="yellow", bold=True)
        typer.echo(f"  name={name}  rpm={rpm}  quota={quota or 'unlimited'}/{period}")
        typer.echo("\n  Use it as:  ?key=<KEY>   or   Authorization: Bearer <KEY>\n")

    asyncio.run(go())


@keys.command("list")
def list_keys() -> None:
    async def go():
        pool = await _pool()
        rows = await pool.fetch(
            """
            select k.key_prefix, k.name, k.rate_limit_rpm, k.quota_limit, k.quota_period,
                   k.active, k.expires_at, k.last_used_at,
                   coalesce(c.used, 0) as used
              from api_keys k
              left join api_quota_counters c
                on c.api_key_id = k.id
               and c.period_start = case when k.quota_period = 'month'
                                         then date_trunc('month', current_date)::date
                                         else current_date end
             order by k.created_at desc
            """
        )
        await pool.close()
        if not rows:
            typer.echo("no keys yet -- create one with: python -m app.cli keys create --name my-app")
            return
        typer.echo(f"\n{'PREFIX':<18}{'NAME':<22}{'RPM':>5}{'USED/QUOTA':>16}  {'STATUS':<9}LAST USED")
        for r in rows:
            quota = f"{r['used']}/{r['quota_limit'] or '∞'}"
            status = "active" if r["active"] else "revoked"
            last = r["last_used_at"].strftime("%Y-%m-%d %H:%M") if r["last_used_at"] else "never"
            typer.echo(
                f"{r['key_prefix']:<18}{(r['name'] or ''):<22}{r['rate_limit_rpm']:>5}"
                f"{quota:>16}  {status:<9}{last}"
            )
        typer.echo()

    asyncio.run(go())


@keys.command("revoke")
def revoke(prefix: str = typer.Argument(..., help="Key prefix from `keys list`")) -> None:
    async def go():
        pool = await _pool()
        n = await pool.execute(
            "update api_keys set active = false where key_prefix = $1", prefix
        )
        await pool.close()
        if n.endswith("0"):
            typer.secho(f"no key with prefix {prefix!r}", fg="red")
            raise typer.Exit(1)
        typer.secho(f"revoked {prefix}", fg="green")
        typer.echo(
            "\nNote: the running gateway caches key lookups for KEY_CACHE_TTL seconds "
            "(default 30), so this takes effect within that window.\n"
            "To revoke immediately:\n"
            "  curl -X POST -H 'X-Admin-Token: $ADMIN_TOKEN' "
            "http://127.0.0.1:8080/admin/purge-key-cache\n"
        )

    asyncio.run(go())


if __name__ == "__main__":
    app()
