# Search API Gateway

A self-hosted, authenticated, cached search API — a drop-in replacement for the
Google Custom Search JSON API, backed by a private [SearXNG](https://docs.searxng.org/)
instance. No Docker.

```
your apps ──key──▶  gateway :8080  ──▶  SearXNG :8888  ──▶  Google / DDG / Brave / Startpage
                         │                (loopback only)
                         ├── auth · rate limit · quota   (Supabase Postgres)
                         └── cache · single-flight       (memory local / Redis on a VPS)
```

SearXNG on its own **cannot** do this job: it has no API keys, no authentication,
no quotas, and no result cache, and its request/response contract looks nothing
like Google's. This gateway supplies all four.

---

## Quick start

Everything below runs from **this** `gateway/` directory. The SearXNG backend it
talks to is the repo one level up.

First, one-time setup of the SearXNG backend (repo root):

```bash
cd ..
uv venv --python 3.13 .venv                 # Python 3.13; 3.14 not yet supported by SearXNG
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-server.txt
uv pip install --python .venv/bin/python setuptools wheel
uv pip install --python .venv/bin/python --no-build-isolation -e .
cd gateway
```

Then the gateway itself:

```bash
make install     # create the gateway venv + copy .env.example -> .env
make searxng     # terminal 1 — private backend on 127.0.0.1:8888
make gateway     # terminal 2 — your API on 127.0.0.1:8080
```

For local testing set `DEV_API_KEY` in `.env` and leave `DATABASE_URL` unset.

```bash
KEY=$(grep DEV_API_KEY .env | cut -d= -f2)

# Google-compatible
curl "http://127.0.0.1:8080/customsearch/v1?q=claude&cx=web&num=10&key=$KEY"

# Native
curl -H "Authorization: Bearer $KEY" "http://127.0.0.1:8080/v1/search?q=claude&limit=10"
```

## Migrating an app off a paid Google Search API

Change the **base URL** and the **key**. Nothing else.

```diff
- https://www.googleapis.com/customsearch/v1?key=AIza...&cx=0123:abc&q=...
+ https://your-gateway/customsearch/v1?key=sk_live_...&cx=web&q=...
```

Keep your existing `cx` values if you like — register them in
[`gateway/profiles.yml`](gateway/profiles.yml) under `aliases:` and map each to a
profile. A profile decides which SearXNG categories/engines that app searches, so
you can retune an app's engine mix without touching the app.

Supported Google parameters: `q`, `num`, `start`, `cx`, `lr`, `gl`, `safe`,
`dateRestrict`, `siteSearch`, `siteSearchFilter`, `fileType`, `exactTerms`,
`excludeTerms`, `orTerms`, `sort=date`, `searchType=image`.

## API keys

```bash
cd gateway
python -m app.cli keys init                              # apply the schema
python -m app.cli keys create --name blog-app --rpm 60 --quota 10000
python -m app.cli keys list
python -m app.cli keys revoke sk_live_a1b2c3
```

The raw key is displayed **once**; only its SHA-256 hash is stored. Keys are
accepted as `?key=`, `Authorization: Bearer`, or `X-API-Key`.

> Revocation takes effect within `KEY_CACHE_TTL` (30s) because key lookups are
> cached. `POST /admin/purge-key-cache` makes it immediate.

## The cache is load-bearing, not an optimization

SearXNG **scrapes** Google/DDG/Brave under your IP. It is not an index you own.

Google clients paginate with `start`/`num`; SearXNG has neither — only a
page-based `pageno` returning a variable number of results (25 and 35 observed on
consecutive pages of the same query). So the gateway accumulates results into a
cache entry keyed on the query **without** `start`/`num`, and slices any window
out of it.

Measured on this setup, with an independent counter between the gateway and
SearXNG:

| Scenario | Real upstream scrapes |
|---|---|
| A client paging through 4 result pages | **2** |
| 8 simultaneous requests for the same cold query | **2** (7 coalesced to zero) |

A naive pass-through would have made 4–12 and 8 respectively. That difference is
the difference between working and having your IP captcha'd. During development
we already tripped a real `SearxEngineTooManyRequestsException` from an upstream
engine, which is exactly the failure this prevents.

**Watch `X-Search-Degraded`.** It surfaces SearXNG's `unresponsive_engines`. A
rising count is your early warning that upstream engines are blocking this host —
*before* results silently go to zero.

## What this CANNOT faithfully emulate

Stated plainly, because a search API that lies to you is worse than one that
tells you its limits.

- **`totalResults` is an estimate.** SearXNG has no result-count field anywhere,
  so there is nothing to report. The default (`lower_bound`) returns what we
  actually hold. Use `queries.nextPage` — which *is* exact — to decide whether to
  paginate. `TOTAL_RESULTS_MODE=inflated` will fabricate a Google-looking number
  if a client demands one; it is off by default because fake numbers end up in
  someone's analytics as though they were real.
- **Deep pagination past ~50–100 results is impossible**, not merely slow.
  Upstream engines cap how far their own pages go (Brave 10, Qwant 5).
- **Ranking is per-page, not global.** SearXNG scores results per search, so
  page-1 and page-2 scores aren't comparable. Results are kept in fetch order;
  re-sorting would make the item at `start=1` change as you page forward.
- **`dateRestrict` loses its count.** `d3`, `w2`, `m6` collapse into SearXNG's
  four buckets (day/week/month/year), rounded up. `y2+` becomes no filter.
- **`sort` by structured fields, `cr` boolean expressions, `filter=0`, `hl`** —
  no equivalent exists. Rejected or reported in `X-Unsupported-Params`.
- **`pagemap.metatags` and rich structured data are absent.** SearXNG never
  fetches the target page, so there is nothing to extract. Omitted, not faked.
- **Snippet bolding matches literals.** Google also bolds stems and synonyms.
- **No SLA.** You are scraping. Under heavy load you will be rate-limited.

## Deploying to a VPS

Only config changes.

```bash
CACHE_BACKEND=redis          # REQUIRED if you run >1 uvicorn worker: with the
RATE_LIMIT_BACKEND=redis     # memory backends each worker gets its own cache and
REDIS_URL=redis://...        # its own buckets -> N caches, N x the rate limit
DATABASE_URL=postgresql://...
ADMIN_TOKEN=$(openssl rand -hex 32)
PUBLIC_BASE_URL=https://search.example.com
```

Use [`deploy/Caddyfile`](deploy/Caddyfile) for TLS. **SearXNG must stay bound to
loopback and must never be exposed** — it has no auth, so anyone who can reach it
gets unlimited searches through your IP. The gateway is the only public door.

Before deploying, set a real SearXNG secret (env var, keeps it out of git):
`export SEARXNG_SECRET=$(openssl rand -hex 32)` — it overrides the `secret_key`
placeholder in [`deploy/settings-local.yml`](deploy/settings-local.yml).

See [`DEPLOY.md`](DEPLOY.md) for a full step-by-step VPS guide.

## Layout

This whole gateway lives inside a fork of SearXNG. The repo root is SearXNG
itself (untouched, so upstream merges stay clean); everything here is under
`gateway/`.

```
<repo root>/          ← SearXNG (fork of searxng/searxng), left untouched
  searx/  docs/ ...
  gateway/            ← this project
    app/core/service.py     ← the accumulator; the heart of the system
    app/core/cache.py       ← cache backends
    app/searxng/qbuilder.py ← operator rewriting + the !/:/< guard
    app/google/             ← CSE param mapping, response shape, bolding
    app/auth/               ← keys, rate limit, quota, usage log
    deploy/                 ← SearXNG settings-local.yml + Caddyfile
```

## Tests

```bash
make test    # 58 tests
```

The pagination tests replay **real recorded SearXNG pages** and assert on the
number of upstream fetches — they are what stop a refactor from quietly
reintroducing one-scrape-per-client-page.
