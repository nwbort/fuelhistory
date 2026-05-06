#!/usr/bin/env python3
"""
Load fuelhistory data into Cloudflare D1 via REST API.

What goes into D1:
  brands, fuels, stations        — full lookup data
  daily_avg, brand_daily_avg     — full price history (pre-aggregated, for charts)
  prices                         — last RECENT_MONTHS only (for movers/cheapest)

Usage:
  export CLOUDFLARE_ACCOUNT_ID=...
  export CLOUDFLARE_D1_ID=...
  export CLOUDFLARE_API_TOKEN=...
  python3 load_d1.py
"""

import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DB = Path("fuelhistory_normalized.db")
RECENT_MONTHS = 6
BATCH = 500
WORKERS = 6

ACCOUNT_ID = os.environ["CLOUDFLARE_ACCOUNT_ID"]
DB_ID = os.environ["CLOUDFLARE_D1_ID"]
TOKEN = os.environ["CLOUDFLARE_API_TOKEN"]
API = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/d1/database/{DB_ID}/query"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def escape(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def exec_sql(sql, retries=3):
    data = json.dumps({"sql": sql}).encode()
    req = Request(API, data=data, headers=HEADERS)
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=60) as r:
                result = json.loads(r.read())
            if not result.get("success"):
                raise RuntimeError(f"D1 error: {result.get('errors')}")
            return result
        except HTTPError as e:
            body = e.read().decode()
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body}")


def insert_batch(table, cols, rows):
    val_strs = ["(" + ",".join(escape(v) for v in row) + ")" for row in rows]
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES {','.join(val_strs)}"
    exec_sql(sql)


def load_table(con, table, cols, where=None, label=None):
    label = label or table
    q = f"SELECT {','.join(cols)} FROM {table}" + (f" WHERE {where}" if where else "")
    rows = con.execute(q).fetchall()
    total = len(rows)
    print(f"\n  {label}: {total:,} rows")
    if not rows:
        return

    batches = [rows[i : i + BATCH] for i in range(0, total, BATCH)]
    done = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(insert_batch, table, cols, b): len(b) for b in batches}
        for fut in as_completed(futs):
            try:
                fut.result()
                done += futs[fut]
            except Exception as e:
                errors += 1
                print(f"\n  [err] {e}")
            print(f"  {done:,}/{total:,}", end="\r")

    status = f"{done:,} inserted" + (f", {errors} errors" if errors else "")
    print(f"  {status}          ")


def create_schema():
    print("Creating schema in D1...")
    for sql in [
        "DROP TABLE IF EXISTS brand_daily_avg",
        "DROP TABLE IF EXISTS daily_avg",
        "DROP TABLE IF EXISTS prices",
        "DROP TABLE IF EXISTS stations",
        "DROP TABLE IF EXISTS brands",
        "DROP TABLE IF EXISTS fuels",
        "CREATE TABLE brands (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)",
        "CREATE TABLE fuels  (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE)",
        """CREATE TABLE stations (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            address TEXT, suburb TEXT, postcode TEXT, brand_id INTEGER
        )""",
        """CREATE TABLE prices (
            station_id INTEGER NOT NULL, fuel_id INTEGER NOT NULL,
            price_date TEXT NOT NULL, price_cents10 INTEGER NOT NULL
        )""",
        """CREATE TABLE daily_avg (
            fuel_id INTEGER NOT NULL, price_date TEXT NOT NULL,
            avg_price REAL NOT NULL, num_prices INTEGER NOT NULL,
            PRIMARY KEY (fuel_id, price_date)
        )""",
        """CREATE TABLE brand_daily_avg (
            fuel_id INTEGER NOT NULL, brand_id INTEGER NOT NULL,
            price_date TEXT NOT NULL, avg_price REAL NOT NULL, num_prices INTEGER NOT NULL,
            PRIMARY KEY (fuel_id, brand_id, price_date)
        )""",
        "CREATE INDEX idx_prices_fuel_date ON prices(fuel_id, price_date)",
        "CREATE INDEX idx_prices_station   ON prices(station_id)",
    ]:
        exec_sql(sql)
    print("  Done")


def main():
    if not all([ACCOUNT_ID, DB_ID, TOKEN]):
        print("Set CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_D1_ID, CLOUDFLARE_API_TOKEN")
        raise SystemExit(1)

    con = sqlite3.connect(DB)
    max_date = con.execute("SELECT MAX(price_date) FROM prices").fetchone()[0]
    cutoff = (datetime.strptime(max_date, "%Y-%m-%d") - timedelta(days=RECENT_MONTHS * 30)).strftime("%Y-%m-%d")

    print(f"Source: {DB}  (prices cutoff: {cutoff})")

    create_schema()

    load_table(con, "brands",          ["id", "name"])
    load_table(con, "fuels",           ["id", "code"])
    load_table(con, "stations",        ["id", "name", "address", "suburb", "postcode", "brand_id"])
    load_table(con, "daily_avg",       ["fuel_id", "price_date", "avg_price", "num_prices"])
    load_table(con, "brand_daily_avg", ["fuel_id", "brand_id", "price_date", "avg_price", "num_prices"])
    load_table(con, "prices",          ["station_id", "fuel_id", "price_date", "price_cents10"],
               where=f"price_date >= '{cutoff}'",
               label=f"prices (last {RECENT_MONTHS} months)")

    con.close()
    print("\nAll done.")


if __name__ == "__main__":
    main()
