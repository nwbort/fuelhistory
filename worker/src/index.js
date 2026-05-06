// NSW Fuel Price Intelligence — Cloudflare Worker
// Serves static assets from ./public and handles /api/* routes against D1.

let cachedMaxDate = null;

async function getMaxDate(db) {
  if (!cachedMaxDate) {
    const r = await db.prepare("SELECT MAX(price_date) AS d FROM prices").first();
    cachedMaxDate = r.d;
  }
  return cachedMaxDate;
}

function addDays(dateStr, days) {
  const d = new Date(dateStr + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
}

// ── /api/stats ──────────────────────────────────────────────────────────────
async function handleStats(db, fuel) {
  const maxDate = await getMaxDate(db);

  // Latest date with data for this fuel
  const latestRow = await db
    .prepare(
      `SELECT MAX(da.price_date) AS d
       FROM daily_avg da JOIN fuels f ON f.id = da.fuel_id
       WHERE f.code = ?`
    )
    .bind(fuel)
    .first();
  const latestDate = latestRow.d;

  // Current average + station count
  const cur = await db
    .prepare(
      `SELECT da.avg_price AS avg,
              (SELECT COUNT(DISTINCT station_id) FROM prices p2
               JOIN fuels f2 ON f2.id = p2.fuel_id
               WHERE f2.code = ? AND p2.price_date = ?) AS stations
       FROM daily_avg da JOIN fuels f ON f.id = da.fuel_id
       WHERE f.code = ? AND da.price_date = ?`
    )
    .bind(fuel, latestDate, fuel, latestDate)
    .first();

  // Week-ago average (±2 day window)
  const weekAgo = addDays(latestDate, -7);
  const weekAgoEnd = addDays(weekAgo, 2);
  const waRow = await db
    .prepare(
      `SELECT AVG(da.avg_price) AS avg
       FROM daily_avg da JOIN fuels f ON f.id = da.fuel_id
       WHERE f.code = ? AND da.price_date BETWEEN ? AND ?`
    )
    .bind(fuel, weekAgo, weekAgoEnd)
    .first();

  const weekChange = waRow.avg ? +((cur.avg - waRow.avg).toFixed(1)) : 0;

  // Diesel average + week change
  const dlLatest = await db
    .prepare(`SELECT MAX(da.price_date) AS d FROM daily_avg da JOIN fuels f ON f.id = da.fuel_id WHERE f.code = 'DL'`)
    .first();
  const dlRow = await db
    .prepare(`SELECT da.avg_price AS avg FROM daily_avg da JOIN fuels f ON f.id = da.fuel_id WHERE f.code = 'DL' AND da.price_date = ?`)
    .bind(dlLatest.d)
    .first();
  const dlWaRow = await db
    .prepare(`SELECT AVG(da.avg_price) AS avg FROM daily_avg da JOIN fuels f ON f.id = da.fuel_id WHERE f.code = 'DL' AND da.price_date BETWEEN ? AND ?`)
    .bind(weekAgo, weekAgoEnd)
    .first();
  const dieselChange = dlWaRow.avg ? +((dlRow.avg - dlWaRow.avg).toFixed(1)) : 0;

  // Premium: P98 vs selected fuel
  const p98Latest = await db
    .prepare(`SELECT MAX(da.price_date) AS d FROM daily_avg da JOIN fuels f ON f.id = da.fuel_id WHERE f.code = 'P98'`)
    .first();
  const p98Row = await db
    .prepare(`SELECT da.avg_price AS avg FROM daily_avg da JOIN fuels f ON f.id = da.fuel_id WHERE f.code = 'P98' AND da.price_date = ?`)
    .bind(p98Latest.d)
    .first();
  const premium = p98Row.avg ? +((p98Row.avg - cur.avg).toFixed(1)) : 0;
  const premPct = cur.avg ? +((premium / cur.avg * 100).toFixed(1)) : 0;

  // Brand saving: cheapest vs most expensive major brand last 7 days
  const brandsRows = await db
    .prepare(
      `SELECT b.name, AVG(bda.avg_price) AS avg
       FROM brand_daily_avg bda
       JOIN fuels f ON f.id = bda.fuel_id
       JOIN brands b ON b.id = bda.brand_id
       WHERE f.code = ? AND bda.price_date >= ?
       GROUP BY b.name
       HAVING SUM(bda.num_prices) > 20
       ORDER BY avg ASC`
    )
    .bind(fuel, weekAgo)
    .all();
  const bRows = brandsRows.results;
  const brandSaving = bRows.length >= 2 ? +((bRows[bRows.length - 1].avg - bRows[0].avg).toFixed(1)) : 0;
  const cheapestBrand = bRows.length ? bRows[0].name : "";

  // Price pulse: latest day vs day before, per station
  const prevDate = addDays(latestDate, -1);
  const pulse = await db
    .prepare(
      `WITH cur AS (
         SELECT p.station_id, AVG(p.price_cents10) / 10.0 AS price
         FROM prices p JOIN fuels f ON f.id = p.fuel_id
         WHERE f.code = ? AND p.price_date = ?
         GROUP BY p.station_id
       ), prev AS (
         SELECT p.station_id, AVG(p.price_cents10) / 10.0 AS price
         FROM prices p JOIN fuels f ON f.id = p.fuel_id
         WHERE f.code = ? AND p.price_date = ?
         GROUP BY p.station_id
       )
       SELECT
         SUM(CASE WHEN c.price > pv.price THEN 1 ELSE 0 END) AS up,
         SUM(CASE WHEN c.price < pv.price THEN 1 ELSE 0 END) AS dn,
         AVG(CASE WHEN c.price > pv.price THEN c.price - pv.price END) AS avg_up,
         AVG(CASE WHEN c.price < pv.price THEN pv.price - c.price END) AS avg_dn
       FROM cur c JOIN prev pv ON c.station_id = pv.station_id`
    )
    .bind(fuel, latestDate, fuel, prevDate)
    .first();

  return json({
    fuel,
    max_date: maxDate,
    latest_fuel_date: latestDate,
    current_avg: +cur.avg.toFixed(1),
    stations: cur.stations,
    week_change: weekChange,
    diesel_avg: dlRow.avg ? +dlRow.avg.toFixed(1) : null,
    diesel_change: dieselChange,
    premium,
    prem_pct: premPct,
    brand_saving: brandSaving,
    cheapest_brand: cheapestBrand,
    pulse_up: pulse.up || 0,
    pulse_dn: pulse.dn || 0,
    pulse_avg_up: pulse.avg_up ? +pulse.avg_up.toFixed(1) : 0,
    pulse_avg_dn: pulse.avg_dn ? +pulse.avg_dn.toFixed(1) : 0,
  });
}

// ── /api/national-average ────────────────────────────────────────────────────
async function handleNationalAverage(db, days) {
  const maxDate = await getMaxDate(db);
  const cutoff = days === 0 ? "2016-01-01" : addDays(maxDate, -days);

  const rows = await db
    .prepare(
      `SELECT da.price_date AS day, f.code AS fuel, da.avg_price AS avg
       FROM daily_avg da
       JOIN fuels f ON f.id = da.fuel_id
       WHERE f.code IN ('E10', 'U91', 'DL') AND da.price_date >= ?
       ORDER BY day`
    )
    .bind(cutoff)
    .all();

  const result = {};
  for (const r of rows.results) {
    if (!result[r.fuel]) result[r.fuel] = { dates: [], prices: [] };
    result[r.fuel].dates.push(r.day);
    result[r.fuel].prices.push(r.avg);
  }
  return json(result);
}

// ── /api/brand-comparison ────────────────────────────────────────────────────
async function handleBrandComparison(db, fuel, days) {
  const maxDate = await getMaxDate(db);
  const cutoff = days === 0 ? "2016-01-01" : addDays(maxDate, -days);

  const rows = await db
    .prepare(
      `WITH top_brands AS (
         SELECT bda.brand_id, SUM(bda.num_prices) AS cnt
         FROM brand_daily_avg bda JOIN fuels f ON f.id = bda.fuel_id
         WHERE f.code = ? AND bda.price_date >= ?
         GROUP BY bda.brand_id ORDER BY cnt DESC LIMIT 7
       )
       SELECT bda.price_date AS day, b.name AS brand, bda.avg_price AS avg
       FROM brand_daily_avg bda
       JOIN fuels f ON f.id = bda.fuel_id
       JOIN brands b ON b.id = bda.brand_id
       WHERE f.code = ? AND bda.price_date >= ? AND bda.brand_id IN (SELECT brand_id FROM top_brands)
       ORDER BY day`
    )
    .bind(fuel, cutoff, fuel, cutoff)
    .all();

  const result = {};
  for (const r of rows.results) {
    if (!result[r.brand]) result[r.brand] = { dates: [], prices: [] };
    result[r.brand].dates.push(r.day);
    result[r.brand].prices.push(r.avg);
  }
  return json(result);
}

// ── /api/biggest-movers ──────────────────────────────────────────────────────
async function handleBiggestMovers(db, fuel) {
  const maxDate = await getMaxDate(db);
  const latestRow = await db
    .prepare(`SELECT MAX(da.price_date) AS d FROM daily_avg da JOIN fuels f ON f.id = da.fuel_id WHERE f.code = ?`)
    .bind(fuel)
    .first();
  const latestDate = latestRow.d;
  const weekAgo = addDays(latestDate, -7);
  const weekAgoEnd = addDays(latestDate, -5);

  const rows = await db
    .prepare(
      `WITH cur AS (
         SELECT p.station_id, AVG(p.price_cents10) / 10.0 AS price
         FROM prices p JOIN fuels f ON f.id = p.fuel_id
         WHERE f.code = ? AND p.price_date = ?
         GROUP BY p.station_id
       ), prev AS (
         SELECT p.station_id, AVG(p.price_cents10) / 10.0 AS price
         FROM prices p JOIN fuels f ON f.id = p.fuel_id
         WHERE f.code = ? AND p.price_date BETWEEN ? AND ?
         GROUP BY p.station_id
       )
       SELECT s.name AS ServiceStationName, s.suburb AS Suburb, b.name AS Brand,
              ROUND(c.price, 1) AS current_price,
              ROUND(c.price - pv.price, 1) AS change
       FROM cur c
       JOIN prev pv ON c.station_id = pv.station_id
       JOIN stations s ON s.id = c.station_id
       JOIN brands b ON b.id = s.brand_id
       WHERE c.price != pv.price
       ORDER BY ABS(c.price - pv.price) DESC
       LIMIT 10`
    )
    .bind(fuel, latestDate, fuel, weekAgo, weekAgoEnd)
    .all();

  return json(rows.results);
}

// ── /api/cheapest ────────────────────────────────────────────────────────────
async function handleCheapest(db, fuel) {
  const maxDate = await getMaxDate(db);
  const cutoff = addDays(maxDate, -7);

  const rows = await db
    .prepare(
      `SELECT s.name AS ServiceStationName, s.address AS Address,
              s.suburb AS Suburb, b.name AS Brand,
              MIN(p.price_cents10) / 10.0 AS price,
              MAX(p.price_date) AS last_seen
       FROM prices p
       JOIN stations s ON s.id = p.station_id
       JOIN brands b ON b.id = s.brand_id
       JOIN fuels f ON f.id = p.fuel_id
       WHERE f.code = ? AND p.price_date >= ?
       GROUP BY p.station_id
       ORDER BY price ASC
       LIMIT 15`
    )
    .bind(fuel, cutoff)
    .all();

  const maxDateObj = new Date(maxDate + "T00:00:00Z");
  const result = rows.results.map((r) => ({
    ...r,
    days_ago: Math.round((maxDateObj - new Date(r.last_seen + "T00:00:00Z")) / 86400000),
  }));

  return json(result);
}

// ── Router ───────────────────────────────────────────────────────────────────
export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (!url.pathname.startsWith("/api/")) {
      return env.ASSETS.fetch(request);
    }

    const fuel = url.searchParams.get("fuel") || "E10";
    const days = parseInt(url.searchParams.get("days") || "30", 10);

    try {
      switch (url.pathname) {
        case "/api/stats":             return await handleStats(env.DB, fuel);
        case "/api/national-average":  return await handleNationalAverage(env.DB, days);
        case "/api/brand-comparison":  return await handleBrandComparison(env.DB, fuel, days);
        case "/api/biggest-movers":    return await handleBiggestMovers(env.DB, fuel);
        case "/api/cheapest":          return await handleCheapest(env.DB, fuel);
        default:                       return json({ error: "Not found" }, 404);
      }
    } catch (err) {
      return json({ error: err.message }, 500);
    }
  },
};
