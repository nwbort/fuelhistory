"""
NSW Fuel Price Intelligence Dashboard
Flask backend serving historical fuel price data from fuelhistory.db
"""

import sqlite3
from datetime import datetime, timedelta
from functools import lru_cache
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

DB_PATH = "fuelhistory.db"

FUEL_LABELS = {
    "E10": "Unleaded (E10)",
    "U91": "Regular (U91)",
    "P95": "Premium 95",
    "P98": "Premium 98",
    "DL": "Diesel",
    "LPG": "LPG",
    "E85": "E85",
    "B20": "B20",
    "PDL": "Premium Diesel",
    "EV": "EV",
    "CNG": "CNG",
    "LNG": "LNG",
}


def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def ensure_index():
    con = get_db()
    con.execute("CREATE INDEX IF NOT EXISTS idx_fuel_date ON prices(FuelCode, PriceUpdatedDate)")
    con.commit()
    con.close()


@lru_cache(maxsize=1)
def max_date():
    """Return the maximum PriceUpdatedDate in the DB as a date string YYYY-MM-DD."""
    con = get_db()
    row = con.execute("SELECT MAX(substr(PriceUpdatedDate,1,10)) FROM prices").fetchone()
    con.close()
    return row[0]


def date_offset(base_date_str: str, days: int) -> str:
    """Return base_date_str minus `days` days, as YYYY-MM-DD."""
    d = datetime.strptime(base_date_str, "%Y-%m-%d") - timedelta(days=days)
    return d.strftime("%Y-%m-%d")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    fuel = request.args.get("fuel", "E10")
    con = get_db()
    md = max_date()

    # Latest date for this fuel
    row = con.execute(
        "SELECT MAX(substr(PriceUpdatedDate,1,10)) FROM prices WHERE FuelCode = ?",
        (fuel,)
    ).fetchone()
    latest_fuel_date = row[0] if row else md

    # Current avg for selected fuel
    row = con.execute(
        """SELECT AVG(Price), COUNT(DISTINCT ServiceStationName)
           FROM prices
           WHERE FuelCode = ? AND substr(PriceUpdatedDate,1,10) = ?""",
        (fuel, latest_fuel_date)
    ).fetchone()
    current_avg = row[0]
    stations = row[1]

    # Avg 7 days prior
    prior_date = date_offset(latest_fuel_date, 7)
    row = con.execute(
        """SELECT AVG(Price)
           FROM prices
           WHERE FuelCode = ? AND substr(PriceUpdatedDate,1,10) = ?""",
        (fuel, prior_date)
    ).fetchone()
    prior_avg = row[0]
    week_change = round(current_avg - prior_avg, 2) if (current_avg and prior_avg) else None

    # Diesel stats
    row = con.execute(
        """SELECT MAX(substr(PriceUpdatedDate,1,10)) FROM prices WHERE FuelCode = 'DL'"""
    ).fetchone()
    latest_dl_date = row[0] if row else md

    row = con.execute(
        """SELECT AVG(Price) FROM prices WHERE FuelCode = 'DL' AND substr(PriceUpdatedDate,1,10) = ?""",
        (latest_dl_date,)
    ).fetchone()
    diesel_avg = row[0]

    prior_dl_date = date_offset(latest_dl_date, 7)
    row = con.execute(
        """SELECT AVG(Price) FROM prices WHERE FuelCode = 'DL' AND substr(PriceUpdatedDate,1,10) = ?""",
        (prior_dl_date,)
    ).fetchone()
    prior_dl_avg = row[0]
    diesel_change = round(diesel_avg - prior_dl_avg, 2) if (diesel_avg and prior_dl_avg) else None

    # Premium: P98 avg minus selected fuel avg on their latest dates
    row = con.execute(
        """SELECT MAX(substr(PriceUpdatedDate,1,10)) FROM prices WHERE FuelCode = 'P98'"""
    ).fetchone()
    latest_p98_date = row[0] if row else md

    row = con.execute(
        """SELECT AVG(Price) FROM prices WHERE FuelCode = 'P98' AND substr(PriceUpdatedDate,1,10) = ?""",
        (latest_p98_date,)
    ).fetchone()
    p98_avg = row[0]
    premium = round(p98_avg - current_avg, 2) if (p98_avg and current_avg) else None
    prem_pct = round(premium / current_avg * 100, 1) if (premium and current_avg) else None

    # Brand saving: max brand avg minus min brand avg in last 7 days (brands with >20 records)
    cutoff_7 = date_offset(latest_fuel_date, 7)
    rows = con.execute(
        """SELECT Brand, AVG(Price) as avg_price, COUNT(*) as cnt
           FROM prices
           WHERE FuelCode = ? AND substr(PriceUpdatedDate,1,10) >= ?
           GROUP BY Brand
           HAVING cnt > 20
           ORDER BY avg_price""",
        (fuel, cutoff_7)
    ).fetchall()

    brand_saving = None
    cheapest_brand = None
    if rows and len(rows) >= 2:
        min_brand_avg = rows[0]["avg_price"]
        max_brand_avg = rows[-1]["avg_price"]
        cheapest_brand = rows[0]["Brand"]
        brand_saving = round(max_brand_avg - min_brand_avg, 2)

    # Price pulse: latest day vs day before
    row = con.execute(
        """SELECT substr(PriceUpdatedDate,1,10) as day
           FROM prices WHERE FuelCode = ?
           GROUP BY day
           ORDER BY day DESC
           LIMIT 2""",
        (fuel,)
    ).fetchall()
    pulse_up = pulse_dn = pulse_avg_up = pulse_avg_dn = None
    if len(row) == 2:
        today_d = row[0]["day"]
        yest_d = row[1]["day"]

        rows_today = con.execute(
            """SELECT ServiceStationName, AVG(Price) as p
               FROM prices WHERE FuelCode = ? AND substr(PriceUpdatedDate,1,10) = ?
               GROUP BY ServiceStationName""",
            (fuel, today_d)
        ).fetchall()
        rows_yest = con.execute(
            """SELECT ServiceStationName, AVG(Price) as p
               FROM prices WHERE FuelCode = ? AND substr(PriceUpdatedDate,1,10) = ?
               GROUP BY ServiceStationName""",
            (fuel, yest_d)
        ).fetchall()

        today_map = {r["ServiceStationName"]: r["p"] for r in rows_today}
        yest_map = {r["ServiceStationName"]: r["p"] for r in rows_yest}
        common = set(today_map) & set(yest_map)

        ups = [today_map[s] - yest_map[s] for s in common if today_map[s] > yest_map[s]]
        dns = [today_map[s] - yest_map[s] for s in common if today_map[s] < yest_map[s]]
        pulse_up = len(ups)
        pulse_dn = len(dns)
        pulse_avg_up = round(sum(ups) / len(ups), 2) if ups else 0
        pulse_avg_dn = round(sum(dns) / len(dns), 2) if dns else 0

    con.close()
    return jsonify({
        "current_avg": round(current_avg, 2) if current_avg else None,
        "stations": stations,
        "week_change": week_change,
        "diesel_avg": round(diesel_avg, 2) if diesel_avg else None,
        "diesel_change": diesel_change,
        "premium": premium,
        "prem_pct": prem_pct,
        "brand_saving": brand_saving,
        "cheapest_brand": cheapest_brand,
        "pulse_up": pulse_up,
        "pulse_dn": pulse_dn,
        "pulse_avg_up": pulse_avg_up,
        "pulse_avg_dn": pulse_avg_dn,
        "latest_fuel_date": latest_fuel_date,
        "max_date": md,
    })


@app.route("/api/national-average")
def api_national_average():
    days = int(request.args.get("days", 30))
    md = max_date()
    cutoff = "2016-01-01" if days == 0 else date_offset(md, days)

    con = get_db()
    result = {}
    for fuel_code in ("E10", "U91", "DL"):
        rows = con.execute(
            """SELECT substr(PriceUpdatedDate,1,10) as day, AVG(Price) as avg_price
               FROM prices
               WHERE FuelCode = ? AND substr(PriceUpdatedDate,1,10) >= ?
               GROUP BY day
               ORDER BY day""",
            (fuel_code, cutoff)
        ).fetchall()
        result[fuel_code] = {
            "dates": [r["day"] for r in rows],
            "prices": [round(r["avg_price"], 2) for r in rows],
        }
    con.close()
    return jsonify(result)


@app.route("/api/brand-comparison")
def api_brand_comparison():
    fuel = request.args.get("fuel", "E10")
    days = int(request.args.get("days", 30))
    md = max_date()
    cutoff = date_offset(md, days)

    con = get_db()

    # Top 7 brands by volume in this period
    top_brands = con.execute(
        """SELECT Brand, COUNT(*) as cnt
           FROM prices
           WHERE FuelCode = ? AND substr(PriceUpdatedDate,1,10) >= ?
           GROUP BY Brand
           ORDER BY cnt DESC
           LIMIT 7""",
        (fuel, cutoff)
    ).fetchall()

    result = {}
    for brand_row in top_brands:
        brand = brand_row["Brand"]
        rows = con.execute(
            """SELECT substr(PriceUpdatedDate,1,10) as day, AVG(Price) as avg_price
               FROM prices
               WHERE FuelCode = ? AND Brand = ? AND substr(PriceUpdatedDate,1,10) >= ?
               GROUP BY day
               ORDER BY day""",
            (fuel, brand, cutoff)
        ).fetchall()
        result[brand] = {
            "dates": [r["day"] for r in rows],
            "prices": [round(r["avg_price"], 2) for r in rows],
        }
    con.close()
    return jsonify(result)


@app.route("/api/biggest-movers")
def api_biggest_movers():
    fuel = request.args.get("fuel", "E10")
    con = get_db()
    md = max_date()

    # Latest date for this fuel
    row = con.execute(
        "SELECT MAX(substr(PriceUpdatedDate,1,10)) FROM prices WHERE FuelCode = ?",
        (fuel,)
    ).fetchone()
    latest_fuel_date = row[0] if row else md

    # Prior window: 5–9 days before latest
    prior_start = date_offset(latest_fuel_date, 9)
    prior_end = date_offset(latest_fuel_date, 5)

    # Current: avg per station on latest day
    current_rows = con.execute(
        """SELECT ServiceStationName, Suburb, Brand, AVG(Price) as p
           FROM prices
           WHERE FuelCode = ? AND substr(PriceUpdatedDate,1,10) = ?
           GROUP BY ServiceStationName""",
        (fuel, latest_fuel_date)
    ).fetchall()

    # Prior: avg per station over prior window
    prior_rows = con.execute(
        """SELECT ServiceStationName, AVG(Price) as p
           FROM prices
           WHERE FuelCode = ? AND substr(PriceUpdatedDate,1,10) BETWEEN ? AND ?
           GROUP BY ServiceStationName""",
        (fuel, prior_start, prior_end)
    ).fetchall()

    prior_map = {r["ServiceStationName"]: r["p"] for r in prior_rows}

    movers = []
    for r in current_rows:
        name = r["ServiceStationName"]
        if name in prior_map:
            change = round(r["p"] - prior_map[name], 2)
            movers.append({
                "ServiceStationName": name,
                "Suburb": r["Suburb"],
                "Brand": r["Brand"],
                "current_price": round(r["p"], 2),
                "change": change,
            })

    movers.sort(key=lambda x: abs(x["change"]), reverse=True)
    con.close()
    return jsonify(movers[:10])


@app.route("/api/cheapest")
def api_cheapest():
    fuel = request.args.get("fuel", "E10")
    con = get_db()
    md = max_date()
    cutoff = date_offset(md, 7)

    rows = con.execute(
        """SELECT ServiceStationName, Address, Suburb, Brand,
                  MIN(Price) as price,
                  MAX(substr(PriceUpdatedDate,1,10)) as last_seen
           FROM prices
           WHERE FuelCode = ? AND substr(PriceUpdatedDate,1,10) >= ?
           GROUP BY ServiceStationName
           ORDER BY price ASC
           LIMIT 15""",
        (fuel, cutoff)
    ).fetchall()

    latest_d = datetime.strptime(md, "%Y-%m-%d")
    result = []
    for r in rows:
        last_d = datetime.strptime(r["last_seen"], "%Y-%m-%d")
        days_ago = (latest_d - last_d).days
        result.append({
            "ServiceStationName": r["ServiceStationName"],
            "Address": r["Address"],
            "Suburb": r["Suburb"],
            "Brand": r["Brand"],
            "price": round(r["price"], 2),
            "last_seen": r["last_seen"],
            "days_ago": days_ago,
        })
    con.close()
    return jsonify(result)


if __name__ == "__main__":
    ensure_index()
    app.run(host="0.0.0.0", port=5000, debug=True)
else:
    ensure_index()
