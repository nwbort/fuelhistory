#!/usr/bin/env python3
"""
Build a normalized, compact SQLite database from fuelhistory.db.

Schema:
  brands(id, name)
  fuels(id, code)
  stations(id, name, address, suburb, postcode, brand_id)
  prices(station_id, fuel_id, price_date, price_cents10)

price_cents10 stores price * 10 as INTEGER (e.g. 185.9 -> 1859)
price_date stores "YYYY-MM-DD" only (10 chars)
"""

import sqlite3
from pathlib import Path

SRC = Path("fuelhistory.db")
DST = Path("fuelhistory_normalized.db")
CHUNK = 200_000


def build():
    DST.unlink(missing_ok=True)

    src = sqlite3.connect(SRC)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(DST)

    dst.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;

        CREATE TABLE brands (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE fuels (
            id   INTEGER PRIMARY KEY,
            code TEXT NOT NULL UNIQUE
        );

        CREATE TABLE stations (
            id       INTEGER PRIMARY KEY,
            name     TEXT NOT NULL,
            address  TEXT,
            suburb   TEXT,
            postcode TEXT,
            brand_id INTEGER REFERENCES brands(id)
        );

        CREATE TABLE prices (
            station_id   INTEGER NOT NULL REFERENCES stations(id),
            fuel_id      INTEGER NOT NULL REFERENCES fuels(id),
            price_date   TEXT NOT NULL,
            price_cents10 INTEGER NOT NULL
        );
    """)

    # --- Build lookup tables ---
    print("Building brands...")
    brands = src.execute("SELECT DISTINCT Brand FROM prices WHERE Brand IS NOT NULL AND Brand != ''").fetchall()
    dst.executemany("INSERT INTO brands(name) VALUES(?)", [(r[0],) for r in brands])
    dst.commit()
    brand_map = {r[0]: r[1] for r in dst.execute("SELECT name, id FROM brands")}

    print("Building fuels...")
    fuels = src.execute("SELECT DISTINCT FuelCode FROM prices WHERE FuelCode IS NOT NULL AND FuelCode != ''").fetchall()
    dst.executemany("INSERT INTO fuels(code) VALUES(?)", [(r[0],) for r in fuels])
    dst.commit()
    fuel_map = {r[0]: r[1] for r in dst.execute("SELECT code, id FROM fuels")}

    print("Building stations...")
    station_rows = src.execute("""
        SELECT ServiceStationName, Address, Suburb, Postcode, Brand
        FROM prices
        WHERE ServiceStationName IS NOT NULL
        GROUP BY ServiceStationName
    """).fetchall()

    dst.executemany(
        "INSERT INTO stations(name, address, suburb, postcode, brand_id) VALUES(?,?,?,?,?)",
        [
            (r["ServiceStationName"], r["Address"], r["Suburb"], r["Postcode"],
             brand_map.get(r["Brand"]))
            for r in station_rows
        ]
    )
    dst.commit()
    station_map = {r[0]: r[1] for r in dst.execute("SELECT name, id FROM stations")}
    print(f"  {len(station_map):,} unique stations")

    # --- Stream prices ---
    print("Streaming prices...")
    total = src.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    print(f"  {total:,} rows to process")

    offset = 0
    inserted = 0
    while True:
        rows = src.execute(
            "SELECT ServiceStationName, FuelCode, PriceUpdatedDate, Price FROM prices LIMIT ? OFFSET ?",
            (CHUNK, offset)
        ).fetchall()
        if not rows:
            break

        batch = []
        for r in rows:
            sid = station_map.get(r["ServiceStationName"])
            fid = fuel_map.get(r["FuelCode"])
            if sid is None or fid is None:
                continue
            date = r["PriceUpdatedDate"][:10] if r["PriceUpdatedDate"] else None
            price = r["Price"]
            if date is None or price is None:
                continue
            batch.append((sid, fid, date, round(price * 10)))

        dst.executemany(
            "INSERT INTO prices(station_id, fuel_id, price_date, price_cents10) VALUES(?,?,?,?)",
            batch
        )
        dst.commit()
        inserted += len(batch)
        offset += CHUNK
        print(f"  {inserted:,} / {total:,}", end="\r")

    print(f"\n  Done: {inserted:,} rows inserted")

    # --- Pre-aggregated summary tables (fast chart queries) ---
    print("Building daily_avg...")
    dst.executescript("""
        CREATE TABLE daily_avg (
            fuel_id    INTEGER NOT NULL,
            price_date TEXT NOT NULL,
            avg_price  REAL NOT NULL,
            num_prices INTEGER NOT NULL,
            PRIMARY KEY (fuel_id, price_date)
        );
        INSERT INTO daily_avg(fuel_id, price_date, avg_price, num_prices)
        SELECT fuel_id, price_date,
               ROUND(AVG(price_cents10) / 10.0, 2),
               COUNT(*)
        FROM prices
        GROUP BY fuel_id, price_date;
    """)
    dst.commit()

    print("Building brand_daily_avg...")
    dst.executescript("""
        CREATE TABLE brand_daily_avg (
            fuel_id    INTEGER NOT NULL,
            brand_id   INTEGER NOT NULL,
            price_date TEXT NOT NULL,
            avg_price  REAL NOT NULL,
            num_prices INTEGER NOT NULL,
            PRIMARY KEY (fuel_id, brand_id, price_date)
        );
        INSERT INTO brand_daily_avg(fuel_id, brand_id, price_date, avg_price, num_prices)
        SELECT p.fuel_id, s.brand_id, p.price_date,
               ROUND(AVG(p.price_cents10) / 10.0, 2),
               COUNT(*)
        FROM prices p
        JOIN stations s ON s.id = p.station_id
        WHERE s.brand_id IS NOT NULL
        GROUP BY p.fuel_id, s.brand_id, p.price_date;
    """)
    dst.commit()

    # --- Indexes ---
    print("Building indexes...")
    dst.executescript("""
        CREATE INDEX idx_prices_fuel_date ON prices(fuel_id, price_date);
        CREATE INDEX idx_prices_station   ON prices(station_id);
        CREATE INDEX idx_stations_name    ON stations(name);
    """)
    dst.commit()

    src.close()
    dst.close()

    src_mb = SRC.stat().st_size / 1_048_576
    dst_mb = DST.stat().st_size / 1_048_576
    print(f"\nOriginal:   {src_mb:,.0f} MB")
    print(f"Normalized: {dst_mb:,.0f} MB")
    print(f"Reduction:  {(1 - dst_mb/src_mb)*100:.0f}%")


if __name__ == "__main__":
    build()
