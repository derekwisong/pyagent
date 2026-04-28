#!/usr/bin/env python3
"""Aviation weather CLI backed by aviationweather.gov.

All endpoints are public (no key) but the operator asks for a custom
User-Agent. Endpoints documented at https://aviationweather.gov/data/api/.

Subcommands:
    brief <airport> [--radius-nm N] [--hours H] [--pirep-age H]
    metars <airport> [--radius-nm N] [--hours H]
    tafs <airport> [--radius-nm N]
    pireps <airport> [--radius-nm N] [--age H]
    afd <airport>
    advisories <airport> [--radius-nm N]
    station-info <airport>
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any

import requests

_BASE = "https://aviationweather.gov/api/data"
_NWS_BASE = "https://api.weather.gov"
_UA = "pyagent-aviation-weather/0.1 (https://github.com/derekwisong/pyagent)"
_TIMEOUT = 15
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}


def _get(url: str) -> tuple[int, str, Any]:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as e:
        return 0, f"<request failed: {e}>", None
    parsed: Any = None
    if r.status_code == 200 and r.content:
        try:
            parsed = r.json()
        except json.JSONDecodeError:
            parsed = None
    return r.status_code, r.text, parsed


def _bbox(lat: float, lon: float, radius_nm: float) -> str:
    dlat = radius_nm / 60.0
    dlon = radius_nm / (60.0 * max(math.cos(math.radians(lat)), 0.01))
    return f"{lat - dlat:.4f},{lon - dlon:.4f},{lat + dlat:.4f},{lon + dlon:.4f}"


def _resolve_station(airport: str) -> dict[str, Any] | str:
    ident = airport.strip().upper()
    if not ident:
        return "<empty airport id>"
    url = f"{_BASE}/stationinfo?ids={ident}&format=json"
    status, _, data = _get(url)
    if status != 200 or not isinstance(data, list) or not data:
        return f"<unknown airport: {ident}; stationinfo returned status={status}>"
    return data[0]


def _wfo_for(lat: float, lon: float) -> str | None:
    url = f"{_NWS_BASE}/points/{lat},{lon}"
    status, _, data = _get(url)
    if status != 200 or not isinstance(data, dict):
        return None
    cwa = data.get("properties", {}).get("cwa")
    return f"K{cwa}" if cwa else None


def cmd_station_info(args: argparse.Namespace) -> str:
    s = _resolve_station(args.airport)
    if isinstance(s, str):
        return s + "\n"
    keep = {
        k: s.get(k)
        for k in ("icaoId", "iataId", "site", "lat", "lon", "elev", "state",
                 "country", "siteType")
    }
    return json.dumps(keep, indent=2) + "\n"


def cmd_metars(args: argparse.Namespace) -> str:
    s = _resolve_station(args.airport)
    if isinstance(s, str):
        return s + "\n"
    bbox = _bbox(s["lat"], s["lon"], args.radius_nm)
    url = f"{_BASE}/metar?bbox={bbox}&format=json&hours={args.hours}"
    status, _, data = _get(url)
    if status == 204 or data is None:
        return f"no METARs returned for area around {args.airport.upper()}.\n"
    if status != 200:
        return f"<metar fetch failed: status={status}>\n"
    return json.dumps(data, indent=2) + "\n"


def cmd_tafs(args: argparse.Namespace) -> str:
    s = _resolve_station(args.airport)
    if isinstance(s, str):
        return s + "\n"
    bbox = _bbox(s["lat"], s["lon"], args.radius_nm)
    url = f"{_BASE}/taf?bbox={bbox}&format=json"
    status, _, data = _get(url)
    if status == 204 or data is None:
        return f"no TAFs returned for area around {args.airport.upper()}.\n"
    if status != 200:
        return f"<taf fetch failed: status={status}>\n"
    return json.dumps(data, indent=2) + "\n"


def cmd_pireps(args: argparse.Namespace) -> str:
    ident = args.airport.strip().upper()
    if not ident:
        return "<empty airport id>\n"
    url = (
        f"{_BASE}/pirep?id={ident}"
        f"&distance={int(args.radius_nm)}&age={args.age}&format=json"
    )
    status, text, data = _get(url)
    if status == 204 or data is None or data == []:
        return (
            f"no PIREPs in the last {args.age}h within "
            f"{int(args.radius_nm)} nm of {ident}.\n"
        )
    if status != 200:
        return f"<pirep fetch failed: status={status}>: {text[:200]}\n"
    return json.dumps(data, indent=2) + "\n"


def cmd_afd(args: argparse.Namespace) -> str:
    s = _resolve_station(args.airport)
    if isinstance(s, str):
        return s + "\n"
    wfo = _wfo_for(s["lat"], s["lon"])
    if not wfo:
        return f"<could not map {args.airport.upper()} to a WFO>\n"
    url = f"{_BASE}/fcstdisc?cwa={wfo}&type=afd"
    status, text, _ = _get(url)
    if status == 204 or not text.strip():
        return f"no current AFD for WFO {wfo} (covering {args.airport.upper()}).\n"
    if status != 200:
        return f"<afd fetch failed: status={status} for {wfo}>\n"
    return text


def cmd_advisories(args: argparse.Namespace) -> str:
    s = _resolve_station(args.airport)
    if isinstance(s, str):
        return s + "\n"
    bbox = _bbox(s["lat"], s["lon"], args.radius_nm)
    out: dict[str, Any] = {}
    for kind, params in (
        ("airsigmet", f"bbox={bbox}&format=json"),
        ("gairmet", f"bbox={bbox}&format=json"),
        ("cwa", "format=json"),
    ):
        _, _, data = _get(f"{_BASE}/{kind}?{params}")
        out[kind] = data if isinstance(data, list) else []
    return json.dumps(out, indent=2) + "\n"


def cmd_brief(args: argparse.Namespace) -> str:
    s = _resolve_station(args.airport)
    if isinstance(s, str):
        return s + "\n"
    bbox = _bbox(s["lat"], s["lon"], args.radius_nm)
    pirep_radius = max(args.radius_nm * 4, 200)
    sections: dict[str, Any] = {
        "station": {
            k: s.get(k)
            for k in ("icaoId", "site", "lat", "lon", "elev", "siteType")
        },
        "bbox": bbox,
        "radius_nm": args.radius_nm,
    }

    _, _, metars = _get(
        f"{_BASE}/metar?bbox={bbox}&format=json&hours={args.hours}"
    )
    sections["metars"] = metars if isinstance(metars, list) else []

    _, _, tafs = _get(f"{_BASE}/taf?bbox={bbox}&format=json")
    sections["tafs"] = tafs if isinstance(tafs, list) else []

    _, _, pireps = _get(
        f"{_BASE}/pirep?id={args.airport.strip().upper()}"
        f"&distance={int(pirep_radius)}&age={args.pirep_age}&format=json"
    )
    sections["pireps"] = pireps if isinstance(pireps, list) else []
    sections["pirep_radius_nm"] = int(pirep_radius)

    advisories: dict[str, Any] = {}
    for kind, params in (
        ("airsigmet", f"bbox={bbox}&format=json"),
        ("gairmet", f"bbox={bbox}&format=json"),
        ("cwa", "format=json"),
    ):
        _, _, data = _get(f"{_BASE}/{kind}?{params}")
        advisories[kind] = data if isinstance(data, list) else []
    sections["advisories"] = advisories

    wfo = _wfo_for(s["lat"], s["lon"])
    if wfo:
        st, text, _ = _get(f"{_BASE}/fcstdisc?cwa={wfo}&type=afd")
        sections["afd"] = (
            text if st == 200 and text.strip() else f"no current AFD for {wfo}"
        )
    else:
        sections["afd"] = "<no WFO mapping>"

    return json.dumps(sections, indent=2) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(prog="aviation-weather")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("brief", help="one-call summary around an airport")
    p.add_argument("airport")
    p.add_argument("--radius-nm", type=float, default=50)
    p.add_argument("--hours", type=float, default=2)
    p.add_argument("--pirep-age", type=float, default=2)
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("metars")
    p.add_argument("airport")
    p.add_argument("--radius-nm", type=float, default=50)
    p.add_argument("--hours", type=float, default=2)
    p.set_defaults(func=cmd_metars)

    p = sub.add_parser("tafs")
    p.add_argument("airport")
    p.add_argument("--radius-nm", type=float, default=50)
    p.set_defaults(func=cmd_tafs)

    p = sub.add_parser("pireps")
    p.add_argument("airport")
    p.add_argument("--radius-nm", type=float, default=200)
    p.add_argument("--age", type=float, default=2)
    p.set_defaults(func=cmd_pireps)

    p = sub.add_parser("afd")
    p.add_argument("airport")
    p.set_defaults(func=cmd_afd)

    p = sub.add_parser("advisories")
    p.add_argument("airport")
    p.add_argument("--radius-nm", type=float, default=200)
    p.set_defaults(func=cmd_advisories)

    p = sub.add_parser("station-info")
    p.add_argument("airport")
    p.set_defaults(func=cmd_station_info)

    args = ap.parse_args()
    sys.stdout.write(args.func(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
