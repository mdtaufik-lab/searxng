# Deploying to a VPS

A step-by-step guide to run this on a public server so your apps (and n8n Cloud)
can reach it. Written for **Ubuntu 22.04/24.04**. Commands assume you are `root`
or using `sudo`.

The end state:

```
internet ──HTTPS──▶ Caddy (:443) ──▶ gateway (:8080) ──▶ SearXNG (:8888, localhost only)
                     TLS, public       your API, needs        the scraper,
                                        an API key             never public
```

**The golden rule:** SearXNG (`:8888`) must never be reachable from the internet.
It has no password. The gateway is the only public door, and it requires an API
key. We enforce this by binding SearXNG to `127.0.0.1` and only exposing the
gateway through Caddy.

---

## 0. What you need

- A VPS (any provider — Hetzner, DigitalOcean, etc.). 1 GB RAM is enough to start.
- A domain name pointed at the VPS IP (an `A` record). Optional but needed for
  HTTPS; without it you can still test over `http://<vps-ip>:8080`.
- A **Supabase** project (free tier is fine) for API keys — its `DATABASE_URL`.

---

## 1. Install system packages

```bash
apt update && apt install -y git python3.12 python3.12-venv curl
# uv (fast Python installer)
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

> SearXNG supports Python 3.11–3.13. Ubuntu 24.04 ships 3.12, which is perfect.

## 2. Get the code (your fork, which now contains everything)

```bash
cd /opt
git clone https://github.com/mdtaufik-lab/searxng.git
cd searxng
```

## 3. Set up the SearXNG backend

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-server.txt
uv pip install --python .venv/bin/python setuptools wheel
uv pip install --python .venv/bin/python --no-build-isolation -e .
```

## 4. Set up the gateway

```bash
cd gateway
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
cp .env.example .env
```

Edit `.env` — the production values:

```ini
SEARXNG_BASE_URL=http://127.0.0.1:8888
DATABASE_URL=postgresql://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres
ADMIN_TOKEN=            # paste: openssl rand -hex 32
PUBLIC_BASE_URL=https://search.yourdomain.com
LOG_RAW_QUERIES=false
```

Create the key tables and your first key:

```bash
.venv/bin/python -m app.cli keys init
.venv/bin/python -m app.cli keys create --name n8n --rpm 60 --quota 10000
# copy the sk_live_... it prints ONCE
```

## 5. Give SearXNG a real secret

```bash
# Put this in the systemd unit below, or export it before starting SearXNG.
openssl rand -hex 32     # copy the output -> SEARXNG_SECRET
```

## 6. Run both as services (systemd)

Create `/etc/systemd/system/searxng.service`:

```ini
[Unit]
Description=SearXNG backend (private)
After=network.target

[Service]
WorkingDirectory=/opt/searxng
Environment=SEARXNG_SETTINGS_PATH=gateway/deploy/settings-local.yml
Environment=SEARXNG_SECRET=PASTE_THE_HEX_FROM_STEP_5
Environment=SEARXNG_BIND_ADDRESS=127.0.0.1
Environment=SEARXNG_PORT=8888
ExecStart=/opt/searxng/.venv/bin/python -m searx.webapp
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

Create `/etc/systemd/system/search-gateway.service`:

```ini
[Unit]
Description=Search API gateway (public via Caddy)
After=network.target searxng.service

[Service]
WorkingDirectory=/opt/searxng/gateway
EnvironmentFile=/opt/searxng/gateway/.env
ExecStart=/opt/searxng/gateway/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

Both bind to `127.0.0.1` — nothing is public yet; Caddy handles that next.

```bash
chown -R www-data:www-data /opt/searxng
systemctl daemon-reload
systemctl enable --now searxng search-gateway
systemctl status searxng search-gateway --no-pager
```

## 7. Put Caddy in front for HTTPS

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

Put your domain into the provided Caddyfile and install it:

```bash
sed -i 's/search.example.com/search.yourdomain.com/' gateway/deploy/Caddyfile
cp gateway/deploy/Caddyfile /etc/caddy/Caddyfile
systemctl reload caddy
```

Caddy fetches a Let's Encrypt certificate automatically. Done.

## 8. Test it

```bash
curl "https://search.yourdomain.com/customsearch/v1?q=hello&cx=web&num=3&key=sk_live_..."
```

In **n8n**, use `https://search.yourdomain.com/customsearch/v1` as the URL and put
the key in an `Authorization: Bearer sk_live_...` header. Because it's now a real
public HTTPS endpoint, **n8n Cloud can reach it** (unlike localhost).

---

## Optional: Redis, for scale

Only needed if you run more than one gateway worker. With the default in-memory
backends, a second worker gets its **own** cache and rate-limit counters — which
means double the scraping and double the effective rate limit. If you scale up:

```bash
apt install -y redis-server
```

In `.env`:

```ini
CACHE_BACKEND=redis
RATE_LIMIT_BACKEND=redis
REDIS_URL=redis://127.0.0.1:6379/0
```

Then run uvicorn with `--workers 4`.

## Security checklist

- [ ] `ufw allow 80,443/tcp` and `ufw allow OpenSSH`, then `ufw enable`. Do **not**
      open 8080 or 8888.
- [ ] Confirm SearXNG is private: from your laptop, `curl http://<vps-ip>:8888`
      must **fail/time out**. If it responds, stop and fix the bind address.
- [ ] `ADMIN_TOKEN` set, so `/admin/*` is protected.
- [ ] The GitHub PAT you used to push is **rotated** (see the main chat).
- [ ] `secret_key` in `settings-local.yml` is overridden by `SEARXNG_SECRET`.

## Updating later

To pull new SearXNG releases into your fork and redeploy:

```bash
cd /opt/searxng
git fetch upstream && git merge upstream/master   # your gateway/ never conflicts
systemctl restart searxng search-gateway
```

(One-time: `git remote add upstream https://github.com/searxng/searxng.git`.)
