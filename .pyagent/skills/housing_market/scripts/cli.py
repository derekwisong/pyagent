#!/usr/bin/env python3
"""US housing market CLI — Zillow Research + FRED.

Public endpoints, no key required. ZHVI metro CSV is cached for 6h
to keep repeat calls fast.

Subcommands:
    metro <pattern> [--limit N]
    rates
    national
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

_UA = "pyagent-housing-market/0.1 (https://github.com/derekwisong/pyagent)"
_HEADERS = {"User-Agent": _UA}
_TIMEOUT = 60

_ZHVI_URL = (
    "https://files.zillowstatic.com/research/public_csvs/zhvi/"
    "Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv"
)
_FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

_CACHE_DIR = Path(tempfile.gettempdir()) / "pyagent-housing-market"
_CACHE_TTL = 6 * 60 * 60  # 6h

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _cached_download(url: str, name: str) -> str | None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / name
    if path.exists() and (time.time() - path.stat().st_mtime) < _CACHE_TTL:
        return path.read_text()
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException:
        return path.read_text() if path.exists() else None
    if r.status_code != 200:
        return path.read_text() if path.exists() else None
    path.write_text(r.text)
    return r.text


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return round((new - old) / old * 100, 2)


def cmd_metro(args: argparse.Namespace) -> str:
    text = _cached_download(_ZHVI_URL, "zhvi_metro.csv")
    if not text:
        return "<failed to load Zillow ZHVI metro CSV>\n"
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return "<empty ZHVI dataset>\n"
    header = rows[0]
    try:
        name_idx = header.index("RegionName")
    except ValueError:
        return "<unexpected ZHVI header layout>\n"
    date_indices = [(i, h) for i, h in enumerate(header) if _DATE_RE.match(h)]
    if not date_indices:
        return "<no date columns in ZHVI dataset>\n"

    pattern = args.pattern.strip().lower()
    if not pattern:
        return "<empty pattern>\n"

    def cell(row: list[str], idx: int) -> float | None:
        if idx >= len(row):
            return None
        v = row[idx].strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    matches = [r for r in rows[1:] if pattern in r[name_idx].lower()]
    if not matches:
        return f"<no metros matching {args.pattern!r}>\n"

    latest_idx, latest_date = date_indices[-1]
    mom_idx = date_indices[-2][0] if len(date_indices) >= 2 else None
    yoy_idx = date_indices[-13][0] if len(date_indices) >= 13 else None
    five_y_idx = date_indices[-61][0] if len(date_indices) >= 61 else None

    out: list[dict[str, Any]] = []
    for r in matches[: args.limit]:
        latest = cell(r, latest_idx)
        out.append({
            "metro": r[name_idx],
            "as_of": latest_date,
            "zhvi": round(latest, 0) if latest else None,
            "mom_pct": _pct(latest, cell(r, mom_idx)) if mom_idx else None,
            "yoy_pct": _pct(latest, cell(r, yoy_idx)) if yoy_idx else None,
            "five_year_pct": _pct(latest, cell(r, five_y_idx)) if five_y_idx else None,
        })
    return json.dumps(out, indent=2) + "\n"


def _fred_latest(series: str) -> dict[str, Any] | None:
    text = _cached_download(_FRED_URL.format(series=series), f"fred_{series}.csv")
    if not text:
        return None
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        return None
    # Walk back from end to find the last row with a numeric value.
    for row in reversed(rows[1:]):
        if len(row) < 2:
            continue
        date, value = row[0].strip(), row[1].strip()
        if not value or value == ".":
            continue
        try:
            return {"as_of": date, "value": float(value)}
        except ValueError:
            continue
    return None


def cmd_rates(args: argparse.Namespace) -> str:
    out: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            "30y_fixed": pool.submit(_fred_latest, "MORTGAGE30US"),
            "15y_fixed": pool.submit(_fred_latest, "MORTGAGE15US"),
        }
        for k, f in futures.items():
            r = f.result()
            out[k] = r if r else {"error": "fetch failed"}
    return json.dumps(out, indent=2) + "\n"


_NATIONAL_SERIES = [
    ("30y_mortgage_rate_pct", "MORTGAGE30US"),
    ("15y_mortgage_rate_pct", "MORTGAGE15US"),
    ("case_shiller_national", "CSUSHPINSA"),
    ("housing_starts_thousands", "HOUST"),
    ("existing_home_sales_thousands", "EXHOSLUSM495S"),
    ("median_sale_price_usd", "MSPUS"),
    ("homeownership_rate_pct", "RHORUSQ156N"),
]


def cmd_national(args: argparse.Namespace) -> str:
    out: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(_NATIONAL_SERIES)) as pool:
        futures = {
            label: pool.submit(_fred_latest, series)
            for label, series in _NATIONAL_SERIES
        }
        for label, f in futures.items():
            r = f.result()
            out[label] = r if r else {"error": "fetch failed"}
    return json.dumps(out, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("metro", help="metro home-value trends from Zillow ZHVI")
    s.add_argument("pattern", help="case-insensitive substring (e.g. 'Boston')")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_metro)

    s = sub.add_parser("rates", help="current Freddie Mac mortgage rates")
    s.set_defaults(func=cmd_rates)

    s = sub.add_parser("national", help="national housing indicators from FRED")
    s.set_defaults(func=cmd_national)

    args = p.parse_args(argv)
    sys.stdout.write(args.func(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
