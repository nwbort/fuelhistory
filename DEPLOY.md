# Deploying to Cloudflare Workers + D1

## Prerequisites

Node.js installed, then:

```bash
npm install -g wrangler
wrangler login
```

## One-time setup

### 1. Create the D1 database

```bash
wrangler d1 create fuelhistory
```

Copy the `database_id` from the output and paste it into `worker/wrangler.toml`.

### 2. Import the normalized database

```bash
# Build it first (if not already done)
cd /path/to/fuelhistory
.venv/bin/python3 build_normalized.py

# Import into D1 (~485 MB, takes a few minutes)
wrangler d1 import fuelhistory fuelhistory_normalized.db
```

### 3. Deploy the Worker

```bash
cd worker
npm install
wrangler deploy
```

Your dashboard is live at `https://fuelhistory.<your-subdomain>.workers.dev`

## Updating with new monthly data

```bash
# 1. Download and ingest new file(s)
.venv/bin/python3 build_db.py

# 2. Rebuild normalized DB
.venv/bin/python3 build_normalized.py

# 3. Re-import to D1
wrangler d1 import fuelhistory fuelhistory_normalized.db
```

Or once you wire up the live NSW API, write directly to D1 via the REST API
and update the daily_avg / brand_daily_avg summary tables incrementally.

## Custom domain

In the Cloudflare dashboard: Workers & Pages → fuelhistory → Settings → Domains & Routes → Add Custom Domain.
