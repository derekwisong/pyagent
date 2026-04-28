#!/usr/bin/env python3
"""OpenSky Network state-vector and history CLI.

Anonymous calls work for current state vectors; historical and
flights-by-aircraft endpoints require OAuth2 client credentials. Reads
those from the env (`OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET`).

Subcommands:
    states-near <airport_or_latlon> [--radius-nm 50]
    states-bbox <lamin> <lomin> <lamax> <lomax>
    aircraft <icao24> [--hours-back 24]
    setup-credentials
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any

import requests

_BASE = "https://opensky-network.org/api"
_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
_AVIATIONWX = "https://aviationweather.gov/api/data/stationinfo"
_UA = "pyagent-flight-tracker/0.1 (https://github.com/derekwisong/pyagent)"
_TIMEOUT = 15


def _credentials() -> tuple[str, str] | None:
    cid = os.environ.get("OPENSKY_CLIENT_ID")
    secret = os.environ.get("OPENSKY_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    return None


def _bearer() -> str | None:
    creds = _credentials()
    if not creds:
        return None
    cid, secret = creds
    try:
        r = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": secret,
            },
            headers={"User-Agent": _UA},
            timeout=_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    payload = r.json()
    return payload.get("access_token")


def _get(path: str, params: dict[str, Any]) -> tuple[int, Any]:
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    if token := _bearer():
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(
            f"{_BASE}{path}", params=params, headers=headers, timeout=_TIMEOUT
        )
    except requests.RequestException as e:
        return 0, f"<request failed: {e}>"
    if r.status_code != 200:
        return r.status_code, r.text[:300]
    try:
        return 200, r.json()
    except json.JSONDecodeError:
        return 200, r.text


_STATE_FIELDS = [
    "icao24", "callsign", "origin_country", "time_position", "last_contact",
    "longitude", "latitude", "baro_altitude", "on_ground", "velocity",
    "true_track", "vertical_rate", "sensors", "geo_altitude", "squawk",
    "spi", "position_source",
]


def _decode_states(states: list[list[Any]] | None) -> list[dict[str, Any]]:
    if not states:
        return []
    out: list[dict[str, Any]] = []
    for s in states:
        rec = {k: v for k, v in zip(_STATE_FIELDS, s)}
        if isinstance(rec.get("callsign"), str):
            rec["callsign"] = rec["callsign"].strip() or None
        out.append(rec)
    return out


def _bbox_from_radius(
    lat: float, lon: float, radius_nm: float
) -> tuple[float, float, float, float]:
    dlat = radius_nm / 60.0
    dlon = radius_nm / (60.0 * max(math.cos(math.radians(lat)), 0.01))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def _resolve_to_latlon(arg: str) -> tuple[float, float] | str:
    arg = arg.strip()
    if "," in arg:
        try:
            lat_s, lon_s = arg.split(",", 1)
            return float(lat_s.strip()), float(lon_s.strip())
        except ValueError:
            return f"<could not parse lat,lon from {arg!r}>"
    ident = arg.upper()
    try:
        r = requests.get(
            _AVIATIONWX,
            params={"ids": ident, "format": "json"},
            headers={"User-Agent": _UA},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        return f"<could not look up {ident}: {e}>"
    if r.status_code != 200:
        return f"<unknown airport {ident}: status {r.status_code}>"
    data = r.json() if r.content else []
    if not data:
        return f"<unknown airport {ident}>"
    return float(data[0]["lat"]), float(data[0]["lon"])


def _states_in_bbox(
    lamin: float, lomin: float, lamax: float, lomax: float
) -> str:
    status, data = _get(
        "/states/all",
        {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax},
    )
    if status != 200:
        if status == 429 or (isinstance(data, str) and "credit" in data.lower()):
            return (
                "<OpenSky rate limit hit. Anonymous quota is small. "
                "Suggest the user set up credentials with `setup-credentials`.>\n"
            )
        return f"<states fetch failed: status={status}: {data}>\n"
    decoded = _decode_states(
        data.get("states") if isinstance(data, dict) else None
    )
    return json.dumps({"count": len(decoded), "states": decoded}, indent=2) + "\n"


def cmd_states_near(args: argparse.Namespace) -> str:
    resolved = _resolve_to_latlon(args.airport_or_latlon)
    if isinstance(resolved, str):
        return resolved + "\n"
    lat, lon = resolved
    lamin, lomin, lamax, lomax = _bbox_from_radius(lat, lon, args.radius_nm)
    return _states_in_bbox(lamin, lomin, lamax, lomax)


def cmd_states_bbox(args: argparse.Namespace) -> str:
    return _states_in_bbox(args.lamin, args.lomin, args.lamax, args.lomax)


def cmd_aircraft(args: argparse.Namespace) -> str:
    hex_id = args.icao24.strip().lower()
    if not hex_id:
        return "<empty icao24>\n"
    out: dict[str, Any] = {"icao24": hex_id}

    status, data = _get("/states/all", {"icao24": hex_id})
    if status == 200 and isinstance(data, dict):
        decoded = _decode_states(data.get("states"))
        out["current_state"] = decoded[0] if decoded else None
    else:
        out["current_state"] = None
        out["state_lookup_error"] = f"status={status}"

    if _credentials() is None:
        out["history"] = (
            "not fetched — set OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET "
            "to access flight history. Run `setup-credentials` for "
            "step-by-step instructions."
        )
        return json.dumps(out, indent=2) + "\n"

    end = int(time.time())
    begin = end - int(args.hours_back) * 3600
    status, data = _get(
        "/flights/aircraft",
        {"icao24": hex_id, "begin": begin, "end": end},
    )
    if status == 200:
        out["recent_flights"] = data
    else:
        out["recent_flights_error"] = f"status={status}: {data}"
    return json.dumps(out, indent=2) + "\n"


def cmd_setup_credentials(args: argparse.Namespace) -> str:
    has_creds = _credentials() is not None
    status_line = (
        "Status: credentials ARE currently set. If lookups still fail, "
        "double-check that the values are correct and the account is "
        "active.\n"
        if has_creds
        else "Status: no credentials configured. Anonymous queries are "
        "rate-limited and history is unavailable.\n"
    )
    return (
        status_line
        + "\n"
        + "To create OpenSky API credentials:\n"
        + "\n"
        + "1. Sign up (or log in) at https://opensky-network.org/.\n"
        + "2. Go to your account page → 'API Client' tab.\n"
        + "3. Create a new API client; copy the client_id and "
        + "client_secret it shows you (the secret is only shown once).\n"
        + "4. Add to your shell rc (e.g. ~/.bashrc, ~/.zshrc):\n"
        + "\n"
        + "       export OPENSKY_CLIENT_ID=<your-client-id>\n"
        + "       export OPENSKY_CLIENT_SECRET=<your-client-secret>\n"
        + "\n"
        + "5. Restart this terminal (or `source` the rc file) so the "
        + "env vars are visible to pyagent.\n"
        + "\n"
        + "After that, this skill's tools will authenticate "
        + "automatically — no further action needed.\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(prog="flight-tracker")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("states-near", help="live state vectors within a radius")
    p.add_argument(
        "airport_or_latlon",
        help='ICAO airport ID ("KORD") or lat,lon ("41.96,-87.93")',
    )
    p.add_argument("--radius-nm", type=float, default=50)
    p.set_defaults(func=cmd_states_near)

    p = sub.add_parser("states-bbox", help="live state vectors in a bbox")
    p.add_argument("lamin", type=float)
    p.add_argument("lomin", type=float)
    p.add_argument("lamax", type=float)
    p.add_argument("lomax", type=float)
    p.set_defaults(func=cmd_states_bbox)

    p = sub.add_parser("aircraft", help="last-known state + recent flights")
    p.add_argument("icao24", help='24-bit ICAO hex code, lowercase ("ac82ec")')
    p.add_argument("--hours-back", type=int, default=24)
    p.set_defaults(func=cmd_aircraft)

    p = sub.add_parser(
        "setup-credentials",
        help="instructions for adding OpenSky OAuth2 client credentials",
    )
    p.set_defaults(func=cmd_setup_credentials)

    args = ap.parse_args()
    sys.stdout.write(args.func(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
