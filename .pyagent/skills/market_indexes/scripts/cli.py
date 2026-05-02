#!/usr/bin/env python3
"""Global market-index CLI backed by Yahoo Finance.

Public unofficial endpoint, no key. One HTTP call per symbol pulls
5y of daily closes; all requested periods derive from that single
series, so an N-symbol request is N parallel requests total.

Subcommands:
    quote <symbol> [<symbol> ...] [--periods ...]
    globe   [--periods ...]
    sectors [--periods ...]
    themes  [--periods ...]
    symbols
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}
_TIMEOUT = 15
_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

GLOBE = [
    "^GSPC",     # S&P 500 — US large-cap
    "^DJI",      # Dow Jones Industrial Average
    "^IXIC",     # NASDAQ Composite
    "^RUT",      # Russell 2000 — US small-cap
    "^GSPTSE",   # TSX — Canada
    "^MXX",      # IPC — Mexico
    "^BVSP",     # Bovespa — Brazil
    "^FTSE",     # FTSE 100 — UK
    "^GDAXI",    # DAX — Germany
    "^FCHI",     # CAC 40 — France
    "^STOXX50E", # Euro Stoxx 50
    "^N225",     # Nikkei 225 — Japan
    "^HSI",      # Hang Seng — Hong Kong
    "000001.SS", # Shanghai Composite
    "^BSESN",    # Sensex — India
    "^KS11",     # KOSPI — South Korea
    "^AXJO",     # ASX 200 — Australia
]

SECTORS = [
    "XLK",   # Technology
    "XLC",   # Communications
    "XLY",   # Consumer Discretionary
    "XLP",   # Consumer Staples
    "XLE",   # Energy
    "XLF",   # Financials
    "XLV",   # Health Care
    "XLI",   # Industrials
    "XLB",   # Materials
    "XLRE",  # Real Estate
    "XLU",   # Utilities
]

THEMES = [
    "^VIX",      # Volatility index
    "DX-Y.NYB",  # US Dollar Index
    "^TNX",      # 10-year Treasury yield
    "GC=F",      # Gold futures
    "CL=F",      # Crude oil futures
    "BTC-USD",   # Bitcoin
]

PRESET = {"globe": GLOBE, "sectors": SECTORS, "themes": THEMES}

# Approx trading days for each period token.
_PERIOD_DAYS = {
    "1d": 1,
    "5d": 5,
    "1m": 21,
    "3m": 63,
    "6m": 126,
    "1y": 252,
    "5y": 1260,
}
_DEFAULT_PERIODS = ["1d", "5d", "1m", "3m", "ytd", "1y"]


def _fetch(symbol: str) -> dict[str, Any]:
    url = _CHART.format(symbol=urllib.parse.quote(symbol, safe="^=.-"))
    params = {"range": "5y", "interval": "1d"}
    # Yahoo rate-limits per-IP. One retry with backoff covers transient 429s
    # that hit when several symbols launch at once.
    last_status = None
    for attempt in range(2):
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        except requests.RequestException as e:
            return {"symbol": symbol, "error": f"request failed: {e}"}
        last_status = r.status_code
        if r.status_code == 429 and attempt == 0:
            time.sleep(1.5 + random.random())
            continue
        break
    if last_status != 200:
        return {"symbol": symbol, "error": f"status={last_status}"}
    try:
        data = r.json()
    except json.JSONDecodeError:
        return {"symbol": symbol, "error": "non-JSON response"}
    result = (data.get("chart") or {}).get("result")
    if not result:
        err = (data.get("chart") or {}).get("error") or {}
        return {"symbol": symbol, "error": err.get("description", "no result")}
    return {"symbol": symbol, "raw": result[0]}


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return round((new - old) / old * 100, 2)


def _summarize(symbol: str, raw: dict[str, Any], periods: list[str]) -> dict[str, Any]:
    meta = raw.get("meta") or {}
    timestamps = raw.get("timestamp") or []
    indicators = (raw.get("indicators") or {}).get("quote") or [{}]
    closes_raw = indicators[0].get("close") or []

    pairs = [
        (ts, c) for ts, c in zip(timestamps, closes_raw) if c is not None
    ]
    if not pairs:
        return {"symbol": symbol, "error": "no close data"}
    timestamps = [p[0] for p in pairs]
    closes = [p[1] for p in pairs]

    last = closes[-1]
    last_ts = timestamps[-1]
    last_date = dt.datetime.fromtimestamp(last_ts, tz=dt.UTC).date().isoformat()

    out: dict[str, Any] = {
        "symbol": symbol,
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "currency": meta.get("currency"),
        "as_of": last_date,
        "price": round(last, 4),
    }

    changes: dict[str, float | None] = {}
    for token in periods:
        if token == "ytd":
            year = dt.datetime.fromtimestamp(last_ts, tz=dt.UTC).year
            anchor = None
            for ts, c in zip(timestamps, closes):
                if dt.datetime.fromtimestamp(ts, tz=dt.UTC).year == year:
                    anchor = c
                    break
            changes["ytd"] = _pct(last, anchor)
        else:
            n = _PERIOD_DAYS.get(token)
            if n is None:
                changes[token] = None
                continue
            idx = len(closes) - 1 - n
            changes[token] = _pct(last, closes[idx]) if idx >= 0 else None
    out["change_pct"] = changes
    return out


def _quote_many(symbols: list[str], periods: list[str]) -> list[dict[str, Any]]:
    if not symbols:
        return []
    # Cap concurrency low — Yahoo returns 429 on bursts above ~5 rps.
    with ThreadPoolExecutor(max_workers=min(len(symbols), 4)) as pool:
        fetched = list(pool.map(_fetch, symbols))
    out: list[dict[str, Any]] = []
    by_symbol = {f["symbol"]: f for f in fetched}
    for s in symbols:  # preserve input order
        f = by_symbol[s]
        if f.get("error"):
            out.append({"symbol": s, "error": f["error"]})
        else:
            out.append(_summarize(s, f["raw"], periods))
    return out


def _parse_periods(s: str) -> list[str]:
    return [t.strip() for t in s.split(",") if t.strip()]


def cmd_quote(args: argparse.Namespace) -> str:
    if not args.symbols:
        return "<no symbols>\n"
    periods = _parse_periods(args.periods)
    return json.dumps(_quote_many(args.symbols, periods), indent=2) + "\n"


def cmd_preset(name: str, args: argparse.Namespace) -> str:
    periods = _parse_periods(args.periods)
    return json.dumps(_quote_many(PRESET[name], periods), indent=2) + "\n"


def cmd_symbols(args: argparse.Namespace) -> str:
    return json.dumps(PRESET, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    default_periods = ",".join(_DEFAULT_PERIODS)

    s = sub.add_parser("quote", help="multi-period change for given symbols")
    s.add_argument("symbols", nargs="+")
    s.add_argument("--periods", default=default_periods)
    s.set_defaults(func=cmd_quote)

    for name, help_text in [
        ("globe", "major global indexes"),
        ("sectors", "US sector ETFs"),
        ("themes", "VIX, DXY, 10y, gold, oil, BTC"),
    ]:
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--periods", default=default_periods)
        s.set_defaults(func=lambda a, n=name: cmd_preset(n, a))

    s = sub.add_parser("symbols", help="list preset symbol groups")
    s.set_defaults(func=cmd_symbols)

    args = p.parse_args(argv)
    sys.stdout.write(args.func(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
