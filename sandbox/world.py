"""Builds a healthy, noisy order-sync pipeline world from a fixed seed.

The world is held in plain Python structures so fault functions can mutate it
before it is written to disk. Nothing here is random at write time: the same
seed always produces the same world.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

FIRST_DAY = date(2026, 10, 15)  # Thursday
N_DAYS = 7  # run dates 2026-10-15 .. 2026-10-21
WEEKDAY_VOLUME = 3000
WEEKEND_FACTOR = 0.7
PAGE_ROWS = 250
ENDPOINT = "https://vendor.example.com/api/v2/orders"
REGIONS = ["NA", "EU", "APAC", "LATAM"]
CURRENCY = {"NA": "USD", "EU": "EUR", "APAC": "USD", "LATAM": "USD"}
SERVICES = ["scheduler", "downloader", "validator", "loader"]

BASE_CONFIG = {
    "pipeline": "order_sync",
    "schedule": "0 2 * * *",
    "timezone": "UTC",
    "vendor": {
        "endpoint": ENDPOINT,
        "token_expires_at": "2027-03-31T00:00:00Z",
        "max_retries": 5,
        "backoff_seconds": 2,
        "page_size": PAGE_ROWS,
        "files_final_after_hours": 48,
    },
    "scheduler": {"retryable_states": ["FAILED"], "workers": 2},
    "loader": {
        "batch_size": 500,
        "bucket_timezone": "local",
        "on_insert_error": "ignore",
        "dedupe_key": "order_id",
    },
    "alerting": {"volume_drop_threshold_pct": 25, "freshness_sla_hours": 26},
}


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_str(d: date) -> str:
    return d.isoformat()


def file_name(d: date) -> str:
    return f"orders_{d.isoformat()}.csv"


def job_id(d: date) -> str:
    return f"os-{d:%Y%m%d}"


def checksum(*parts: object) -> str:
    raw = ":".join(str(p) for p in parts).encode()
    return "sha1:" + hashlib.sha1(raw).hexdigest()[:16]


def at(d: date, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss)


@dataclass
class World:
    seed: int
    days: list[date]
    customers: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    manifest: list[dict] = field(default_factory=list)
    jobs: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    logs: list[dict] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    config_history: list[tuple[str, dict]] = field(default_factory=list)
    alert: dict = field(default_factory=dict)
    rng: random.Random = field(default_factory=random.Random)

    # ---- helpers used by fault functions -------------------------------------------------
    def log(self, when: datetime, level: str, service: str, msg: str, job: str | None = None,
            **extra: object) -> None:
        line = {"ts": ts(when), "level": level, "service": service, "job_id": job, "msg": msg}
        line.update(extra)
        self.logs.append(line)

    def drop_logs(self, pred) -> int:
        before = len(self.logs)
        self.logs = [ln for ln in self.logs if not pred(ln)]
        return before - len(self.logs)

    def job(self, d: date) -> dict:
        return next(j for j in self.jobs if j["run_date"] == day_str(d))

    def manifest_row(self, d: date) -> dict | None:
        return next((m for m in self.manifest if m["run_date"] == day_str(d)), None)

    def orders_for(self, d: date) -> list[dict]:
        return [o for o in self.orders if o["run_date"] == day_str(d)]

    def drop_orders(self, d: date, keep: int) -> None:
        """Keep only the first `keep` loaded orders for run date d."""
        kept = 0
        out = []
        for o in self.orders:
            if o["run_date"] == day_str(d):
                if kept >= keep:
                    continue
                kept += 1
            out.append(o)
        self.orders = out

    def set_alert(self, raised: datetime, rule: str, message: str) -> None:
        self.alert = {
            "alert_id": f"alr-{raised:%Y%m%d}-{self.seed:04d}",
            "raised_at": ts(raised),
            "rule": rule,
            "message": message,
        }

    def volume_drop_pct(self, d: date) -> int:
        """Percent the loaded volume for d sits below the mean of the other days."""
        counts = {x: len(self.orders_for(x)) for x in self.days}
        others = [c for x, c in counts.items() if x != d]
        avg = sum(others) / len(others)
        return round(100 * (avg - counts[d]) / avg)


# ---- healthy generation ------------------------------------------------------------------


def _volume(rng: random.Random, d: date) -> int:
    base = WEEKDAY_VOLUME * (WEEKEND_FACTOR if d.weekday() >= 5 else 1.0)
    return int(base * rng.uniform(0.96, 1.04))


def generate_day(w: World, d: date, rows_read: int | None = None, rejected: int | None = None,
                 deduped: int | None = None) -> None:
    """Emit one healthy run of order_sync for run date d (runs at d+1 02:00 UTC)."""
    rng = w.rng
    run = d + timedelta(days=1)
    jid = job_id(d)
    fname = file_name(d)
    rows_read = rows_read if rows_read is not None else _volume(rng, d)
    # always drawn, even when overridden, so the rng stream stays stable
    r_rej, r_ded = rng.randint(0, 3), rng.randint(2, 12)
    rejected = r_rej if rejected is None else rejected
    deduped = r_ded if deduped is None else deduped
    loaded = rows_read - rejected - deduped
    pages = max(1, -(-rows_read // PAGE_ROWS))
    cfg = w.config

    t = at(run, 2, 0, 0)
    w.log(t, "INFO", "scheduler", f"triggered order_sync run_date={d} worker=loader-w1", jid)
    w.log(t + timedelta(seconds=1), "INFO", "scheduler",
          f"retry sweep: retryable_states={cfg['scheduler']['retryable_states']}; 0 runs to retry")

    # downloader: one GET per page, one deprecation warning per run
    t = at(run, 2, 0, 5)
    attempts = 1
    for p in range(1, pages + 1):
        t += timedelta(seconds=rng.randint(3, 7))
        n = max(0, min(PAGE_ROWS, rows_read - (p - 1) * PAGE_ROWS))
        w.log(t, "INFO", "downloader", f"GET {ENDPOINT}?date={d}&page={p} -> 200 ({n} rows)", jid)
        w.log(t, "WARN", "downloader",
              "vendor API field 'cust_ref' is deprecated and will be removed 2027-01-01; "
              "use 'customer_id'", jid)
    size = 58 + rows_read * 96
    csum = checksum(fname, rows_read, w.seed)
    t += timedelta(seconds=2)
    header_only = " (header only)" if rows_read == 0 else ""
    w.log(t, "INFO", "downloader",
          f"downloaded {fname} rows={rows_read} bytes={size}{header_only} checksum={csum} "
          "vendor_revision=1", jid)
    prev = d - timedelta(days=1)
    w.log(t + timedelta(seconds=1), "INFO", "downloader",
          f"verify {file_name(prev)}: remote checksum matches ingested", jid)

    # validator
    t += timedelta(seconds=10)
    for _ in range(rejected):
        w.log(t, "WARN", "validator",
              f"rejected row line {rng.randint(2, rows_read)} in {fname}: invalid currency 'XXX'",
              jid)
    w.log(t + timedelta(seconds=1), "INFO", "validator",
          f"validated {fname} rows={rows_read} rejected={rejected}", jid)

    # loader
    t += timedelta(seconds=15)
    bs = cfg["loader"]["batch_size"]
    nb = -(-loaded // bs)
    w.log(t, "INFO", "loader",
          f"load started file={fname} batch_size={bs} bucket_timezone=local resolved to UTC "
          f"on host etl-01", jid)
    w.log(t + timedelta(seconds=1), "INFO", "loader", f"deduped {deduped} rows on order_id", jid)
    for b in range(1, nb + 1):
        t += timedelta(seconds=rng.randint(4, 9))
        n = min(bs, loaded - (b - 1) * bs)
        w.log(t, "INFO", "loader", f"batch {b}/{nb} inserted rows={n}", jid)
    if rng.random() < 0.45:
        w.log(t + timedelta(seconds=2), "WARN", "loader",
              f"slow query: {rng.uniform(2.1, 3.9):.1f}s merging staging into orders "
              f"(threshold 2.0s)", jid)
    t += timedelta(seconds=4)
    w.log(t, "INFO", "loader", f"load complete file={fname} rows_loaded={loaded}", jid)

    w.jobs.append({
        "job_id": jid, "job_name": "order_sync", "run_date": day_str(d), "status": "LOADED",
        "attempts": attempts, "started_at": ts(at(run, 2, 0, 0)), "finished_at": ts(t),
        "error": None,
    })
    w.manifest.append({
        "file_name": fname, "run_date": day_str(d), "rows_read": rows_read,
        "rows_loaded": loaded, "rows_rejected": rejected, "rows_deduped": deduped,
        "checksum": csum,
    })
    for i in range(loaded):
        c = w.customers[rng.randrange(len(w.customers))]
        ots = at(d, 0) + timedelta(seconds=rng.randrange(86400))
        w.orders.append({
            "order_id": f"ORD-{d:%Y%m%d}-{i + 1:05d}",
            "customer_id": c["customer_id"],
            "amount": round(rng.uniform(8, 420), 2),
            "currency": CURRENCY[c["region"]],
            "order_ts": ts(ots),
            "region": c["region"],
            "run_date": day_str(d),
            "loaded_at": ts(t),
        })


def add_retry_noise(w: World, d: date) -> None:
    """A download that fails once with a connection reset and succeeds on attempt 2."""
    jid = job_id(d)
    run = d + timedelta(days=1)
    w.log(at(run, 2, 0, 4), "WARN", "downloader",
          f"attempt 1 failed: connection reset by peer (GET {ENDPOINT}?date={d}&page=1); "
          "retrying in 2s", jid)
    w.job(d)["attempts"] = 2


def build_world(seed: int, first_day: date = FIRST_DAY) -> World:
    w = World(seed=seed, days=[first_day + timedelta(days=i) for i in range(N_DAYS)])
    w.rng = random.Random(seed)
    w.config = copy.deepcopy(BASE_CONFIG)
    w.config_history = [
        ("2026-08-12T09:14:00Z", _cfg_with(batch_size=250, endpoint=ENDPOINT.replace("v2", "v1"))),
        ("2026-09-03T16:40:00Z", _cfg_with(batch_size=250)),
        ("2026-09-29T11:05:00Z", copy.deepcopy(BASE_CONFIG)),
    ]
    for i in range(400):
        region = REGIONS[w.rng.randrange(len(REGIONS))]
        created = at(date(2025, 1, 1), 0) + timedelta(minutes=w.rng.randrange(600_000))
        w.customers.append({"customer_id": f"C{i + 1:05d}", "region": region,
                            "created_at": ts(created)})
    for i, (raised, d) in enumerate([("2026-10-05T06:00:00Z", "2026-10-04"),
                                     ("2026-10-12T06:00:00Z", "2026-10-11")]):
        w.alerts.append({
            "alert_id": f"alr-hist-{i + 1:03d}", "raised_at": raised, "rule": "volume_drop",
            "message": f"Order volume for {d} is 29% below the 7-day average",
            "status": "closed", "resolution": "weekend seasonality; no action needed",
        })
    return w


def generate_healthy(w: World, skip: set[date] | None = None) -> None:
    skip = skip or set()
    for d in w.days:
        if d not in skip:
            generate_day(w, d)
    add_retry_noise(w, w.days[2])


def _cfg_with(batch_size: int | None = None, endpoint: str | None = None) -> dict:
    c = copy.deepcopy(BASE_CONFIG)
    if batch_size is not None:
        c["loader"]["batch_size"] = batch_size
    if endpoint is not None:
        c["vendor"]["endpoint"] = endpoint
    return c


# ---- writing -----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE jobs (job_id TEXT PRIMARY KEY, job_name TEXT NOT NULL, run_date TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL, started_at TEXT, finished_at TEXT, error TEXT);
CREATE TABLE ingest_manifest (file_name TEXT NOT NULL, run_date TEXT NOT NULL,
  rows_read INTEGER NOT NULL, rows_loaded INTEGER NOT NULL, rows_rejected INTEGER NOT NULL,
  rows_deduped INTEGER NOT NULL, checksum TEXT);
CREATE TABLE orders (order_id TEXT NOT NULL, customer_id TEXT, amount REAL, currency TEXT,
  order_ts TEXT, region TEXT, run_date TEXT NOT NULL, loaded_at TEXT);
CREATE INDEX idx_orders_run_date ON orders(run_date);
CREATE TABLE customers (customer_id TEXT PRIMARY KEY, region TEXT, created_at TEXT);
CREATE TABLE alerts (alert_id TEXT PRIMARY KEY, raised_at TEXT, rule TEXT, message TEXT,
  status TEXT, resolution TEXT);
"""


def _insert(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    conn.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [tuple(r[c] for c in cols) for r in rows],
    )


def write_world(w: World, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    db = out / "world.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    _insert(conn, "jobs", sorted(w.jobs, key=lambda j: j["run_date"]))
    _insert(conn, "ingest_manifest", sorted(w.manifest, key=lambda m: m["run_date"]))
    _insert(conn, "orders", w.orders)
    _insert(conn, "customers", w.customers)
    alerts = list(w.alerts)
    if w.alert:
        alerts.append({**w.alert, "status": "open", "resolution": None})
    _insert(conn, "alerts", alerts)
    conn.commit()
    conn.close()

    logs = out / "logs"
    logs.mkdir(exist_ok=True)
    for svc in SERVICES:
        lines = sorted((ln for ln in w.logs if ln["service"] == svc), key=lambda ln: ln["ts"])
        (logs / f"{svc}.jsonl").write_text(
            "".join(json.dumps(ln, sort_keys=False) + "\n" for ln in lines), encoding="utf-8")

    cfg = out / "config"
    hist = cfg / "history"
    hist.mkdir(parents=True, exist_ok=True)
    (cfg / "pipeline.yaml").write_text(yaml.safe_dump(w.config, sort_keys=False),
                                       encoding="utf-8")
    for when, c in w.config_history:
        stamp = when.replace(":", "-")
        (hist / f"pipeline.{stamp}.yaml").write_text(yaml.safe_dump(c, sort_keys=False),
                                                     encoding="utf-8")

    (out / "alert.json").write_text(json.dumps(w.alert, indent=2) + "\n", encoding="utf-8")


def db_digest(db: Path) -> str:
    """Content hash of a SQLite file, independent of the SQLite library version."""
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        for line in conn.iterdump():
            h.update(line.encode())
        return h.hexdigest()
    finally:
        conn.close()
