#!/usr/bin/env python3
"""Real estate listings URL builder — no scraping, no data scraping.

Emits clean URLs for Zillow / Redfin / Realtor.com / Trulia (search
and address lookups) and area-research sites (Census, GreatSchools,
WalkScore, Niche, FEMA, etc.). The user opens them in a browser.

Subcommands:
    search <city> --state ST [filters]
    address <full address>
    area <city> --state ST
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from typing import Any

_TYPE_MAP_ZILLOW = {
    "for_sale": "for_sale",
    "for_rent": "for_rent",
    "sold": "recently_sold",
}


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _qq(s: str) -> str:
    return urllib.parse.quote(f'"{s}"', safe="")


def _zillow_search(city: str, state: str, args: argparse.Namespace) -> str:
    type_seg = _TYPE_MAP_ZILLOW.get(args.type, "for_sale")
    slug = f"{city.replace(' ', '-')},-{state.upper()}"
    parts = [f"https://www.zillow.com/homes/{type_seg}/{_q(slug)}_rb/"]
    # Path-style filters — most reliable on for_sale pages.
    filters: list[str] = []
    if args.beds:
        filters.append(f"{args.beds}-_beds")
    if args.baths:
        filters.append(f"{args.baths}-_baths")
    if args.price_min or args.price_max:
        lo = args.price_min or 0
        hi = args.price_max or ""
        filters.append(f"{lo}-{hi}_price")
    if filters:
        parts = [
            f"https://www.zillow.com/homes/{type_seg}/{_q(slug)}/"
            + "/".join(filters)
            + "/_rb/"
        ]
    return parts[0]


def _realtor_search(city: str, state: str, args: argparse.Namespace) -> str:
    slug = f"{city.replace(' ', '-')}_{state.upper()}"
    base = f"https://www.realtor.com/realestateandhomes-search/{_q(slug)}"
    if args.type == "for_rent":
        base = f"https://www.realtor.com/apartments/{_q(slug)}"
    elif args.type == "sold":
        base = f"https://www.realtor.com/realestateandhomes-search/{_q(slug)}/show-recently-sold"
    qs: list[str] = []
    if args.beds:
        qs.append(f"beds-{args.beds}")
    if args.price_min or args.price_max:
        lo = args.price_min or 0
        hi = args.price_max or ""
        qs.append(f"price-{lo}-{hi}")
    if qs:
        base += "/" + "/".join(qs)
    return base


def _trulia_search(city: str, state: str, args: argparse.Namespace) -> str:
    slug = city.replace(" ", "_")
    sub = "for_rent" if args.type == "for_rent" else "for_sale"
    if args.type == "sold":
        sub = "sold"
    return f"https://www.trulia.com/{sub}/{state.upper()}/{_q(slug)}/"


def _redfin_search(city: str, state: str, args: argparse.Namespace) -> str:
    # Redfin uses internal location IDs; Google site-search lands
    # the user on the right city page reliably.
    q = f"site:redfin.com {city} {state.upper()}"
    if args.type == "for_rent":
        q += " for rent"
    elif args.type == "sold":
        q += " sold"
    return f"https://www.google.com/search?q={_q(q)}"


def cmd_search(args: argparse.Namespace) -> str:
    if not args.state:
        return "<--state ST is required>\n"
    out = [
        {"site": "Zillow",          "url": _zillow_search(args.city, args.state, args)},
        {"site": "Realtor.com",     "url": _realtor_search(args.city, args.state, args)},
        {"site": "Trulia",          "url": _trulia_search(args.city, args.state, args)},
        {"site": "Redfin (search)", "url": _redfin_search(args.city, args.state, args)},
    ]
    return json.dumps(out, indent=2) + "\n"


def cmd_address(args: argparse.Namespace) -> str:
    addr = args.address.strip()
    if not addr:
        return "<empty address>\n"
    q = _q(addr)
    qq = _qq(addr)
    out = [
        {"site": "Zillow",
         "url": f"https://www.zillow.com/homes/{q}_rb/"},
        {"site": "Realtor.com",
         "url": f"https://www.realtor.com/realestateandhomes-search/{q}"},
        {"site": "Redfin (search)",
         "url": f"https://www.google.com/search?q=site%3Aredfin.com+{qq}"},
        {"site": "Trulia (search)",
         "url": f"https://www.google.com/search?q=site%3Atrulia.com+{qq}"},
        {"site": "County GIS / assessor (search)",
         "url": f"https://www.google.com/search?q={qq}+assessor+OR+%22property+record%22"},
        {"site": "FEMA Flood Map",
         "url": f"https://msc.fema.gov/portal/search?AddressQuery={q}"},
    ]
    return json.dumps(out, indent=2) + "\n"


def cmd_area(args: argparse.Namespace) -> str:
    if not args.state:
        return "<--state ST is required>\n"
    city = args.city
    state = args.state.upper()
    slug_dash = city.replace(" ", "-").lower()
    slug_under = city.replace(" ", "_")
    qq = _qq(f"{city}, {state}")
    out: list[dict[str, Any]] = [
        {"category": "market", "site": "Zillow Home Values (search)",
         "url": f"https://www.google.com/search?q=site%3Azillow.com%2Fhome-values+{qq}"},
        {"category": "demographics", "site": "Census QuickFacts (search)",
         "url": f"https://www.google.com/search?q=site%3Acensus.gov%2Fquickfacts+{qq}"},
        {"category": "schools", "site": "GreatSchools",
         "url": f"https://www.greatschools.org/{state.lower()}/{_q(slug_dash)}/"},
        {"category": "walkability", "site": "WalkScore",
         "url": f"https://www.walkscore.com/{state}/{_q(slug_under)}"},
        {"category": "general", "site": "Niche",
         "url": f"https://www.niche.com/places-to-live/{_q(slug_dash)}-{state.lower()}/"},
        {"category": "general", "site": "BestPlaces",
         "url": f"https://www.bestplaces.net/city/{state.lower()}/{_q(slug_under.lower())}"},
        {"category": "crime", "site": "AreaVibes",
         "url": f"https://www.areavibes.com/{_q(slug_dash)}-{state.lower()}/"},
        {"category": "natural", "site": "FEMA Flood Map",
         "url": f"https://msc.fema.gov/portal/search?AddressQuery={_q(city + ', ' + state)}"},
        {"category": "commute", "site": "Trulia neighborhood",
         "url": f"https://www.trulia.com/{state}/{_q(slug_under)}/"},
    ]
    return json.dumps(out, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="build listing search URLs for a city")
    s.add_argument("city")
    s.add_argument("--state", required=True, help="2-letter state code (e.g. MA)")
    s.add_argument("--type", choices=["for_sale", "for_rent", "sold"],
                   default="for_sale")
    s.add_argument("--beds", type=int)
    s.add_argument("--baths", type=int)
    s.add_argument("--price-min", type=int)
    s.add_argument("--price-max", type=int)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("address", help="deep-link URLs for a specific address")
    s.add_argument("address")
    s.set_defaults(func=cmd_address)

    s = sub.add_parser("area", help="research links for a city / area")
    s.add_argument("city")
    s.add_argument("--state", required=True, help="2-letter state code (e.g. MA)")
    s.set_defaults(func=cmd_area)

    args = p.parse_args(argv)
    sys.stdout.write(args.func(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
