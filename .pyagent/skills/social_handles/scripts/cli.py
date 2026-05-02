#!/usr/bin/env python3
"""Social handle search CLI — name → candidate handles per network.

Hits only public, auth-free search endpoints. Pairs with the
`social_profile` skill: this returns leads, that confirms them.

Subcommands:
    search <name> [--limit N] [--timeout S]
    candidates <name>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import requests

_UA = "pyagent-social-handles/0.1 (https://github.com/derekwisong/pyagent)"
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}


def _get(url: str, timeout: float) -> tuple[int, Any]:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
    except requests.RequestException as e:
        return 0, f"<request failed: {e}>"
    try:
        return r.status_code, r.json()
    except json.JSONDecodeError:
        return r.status_code, None


def _match(handle: str, name: str | None = None, url: str | None = None,
           bio: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"handle": handle}
    if name:
        out["name"] = name
    if url:
        out["url"] = url
    if bio:
        out["bio"] = bio
    return out


def _result(site: str, matches: list[dict[str, Any]],
            error: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"site": site, "matches": matches}
    if error:
        out["error"] = error
    return out


def search_github(q: str, limit: int, timeout: float) -> dict[str, Any]:
    url = (
        "https://api.github.com/search/users"
        f"?q={urllib.parse.quote(q)}&per_page={limit}"
    )
    status, data = _get(url, timeout)
    if status != 200 or not isinstance(data, dict):
        return _result("GitHub", [], f"status={status}")
    items = data.get("items", [])[:limit]
    matches = [
        _match(it.get("login"), url=it.get("html_url"))
        for it in items
        if it.get("login")
    ]
    return _result("GitHub", matches)


def search_gitlab(q: str, limit: int, timeout: float) -> dict[str, Any]:
    url = (
        "https://gitlab.com/api/v4/users"
        f"?search={urllib.parse.quote(q)}&per_page={limit}"
    )
    status, data = _get(url, timeout)
    if status != 200 or not isinstance(data, list):
        return _result("GitLab", [], f"status={status}")
    matches = [
        _match(u.get("username"), name=u.get("name"), url=u.get("web_url"),
               bio=u.get("bio"))
        for u in data[:limit]
        if u.get("username")
    ]
    return _result("GitLab", matches)


def search_reddit(q: str, limit: int, timeout: float) -> dict[str, Any]:
    url = (
        "https://www.reddit.com/users/search.json"
        f"?q={urllib.parse.quote(q)}&limit={limit}"
    )
    status, data = _get(url, timeout)
    if status != 200 or not isinstance(data, dict):
        return _result("Reddit", [], f"status={status}")
    children = (data.get("data") or {}).get("children", [])
    matches = []
    for c in children[:limit]:
        d = c.get("data") or {}
        handle = d.get("name")
        if not handle:
            continue
        matches.append(_match(
            handle,
            url=f"https://www.reddit.com/user/{handle}",
            bio=d.get("subreddit", {}).get("public_description"),
        ))
    return _result("Reddit", matches)


def search_mastodon(q: str, limit: int, timeout: float) -> dict[str, Any]:
    url = (
        "https://mastodon.social/api/v2/search"
        f"?q={urllib.parse.quote(q)}&type=accounts&limit={limit}&resolve=false"
    )
    status, data = _get(url, timeout)
    if status != 200 or not isinstance(data, dict):
        return _result("Mastodon", [], f"status={status}")
    accounts = data.get("accounts", [])[:limit]
    matches = [
        _match(a.get("acct"), name=a.get("display_name"),
               url=a.get("url"), bio=a.get("note"))
        for a in accounts
        if a.get("acct")
    ]
    return _result("Mastodon", matches)


def search_bluesky(q: str, limit: int, timeout: float) -> dict[str, Any]:
    url = (
        "https://public.api.bsky.app/xrpc/app.bsky.actor.searchActors"
        f"?q={urllib.parse.quote(q)}&limit={limit}"
    )
    status, data = _get(url, timeout)
    if status != 200 or not isinstance(data, dict):
        return _result("Bluesky", [], f"status={status}")
    actors = data.get("actors", [])[:limit]
    matches = [
        _match(a.get("handle"), name=a.get("displayName"),
               url=f"https://bsky.app/profile/{a.get('handle')}",
               bio=a.get("description"))
        for a in actors
        if a.get("handle")
    ]
    return _result("Bluesky", matches)


def search_keybase(q: str, limit: int, timeout: float) -> dict[str, Any]:
    url = (
        "https://keybase.io/_/api/1.0/user/user_search.json"
        f"?q={urllib.parse.quote(q)}&num_wanted={limit}"
    )
    status, data = _get(url, timeout)
    if status != 200 or not isinstance(data, dict):
        return _result("Keybase", [], f"status={status}")
    rows = data.get("list", [])[:limit]
    matches = []
    for row in rows:
        kb = row.get("keybase") or {}
        handle = kb.get("username")
        if not handle:
            continue
        matches.append(_match(
            handle,
            name=kb.get("full_name"),
            url=f"https://keybase.io/{handle}",
        ))
    return _result("Keybase", matches)


SEARCHERS: list[Callable[[str, int, float], dict[str, Any]]] = [
    search_github,
    search_gitlab,
    search_reddit,
    search_mastodon,
    search_bluesky,
    search_keybase,
]


def cmd_search(args: argparse.Namespace) -> str:
    name = args.name.strip()
    if not name:
        return "<empty name>\n"
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(SEARCHERS)) as pool:
        futures = [pool.submit(s, name, args.limit, args.timeout)
                   for s in SEARCHERS]
        for f in as_completed(futures):
            results.append(f.result())
    results.sort(key=lambda r: (-len(r["matches"]), r["site"]))
    return json.dumps(results, indent=2) + "\n"


def _generate_candidates(name: str) -> list[str]:
    parts = [p.lower() for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return []
    if len(parts) == 1:
        return [parts[0]]
    first, last = parts[0], parts[-1]
    out = [
        f"{first}{last}",
        f"{first}.{last}",
        f"{first}_{last}",
        f"{first}-{last}",
        f"{first[0]}{last}",
        last,
        first,
        f"{first}{last[0]}",
    ]
    if len(parts) > 2:
        out.extend([
            "".join(parts),
            ".".join(parts),
            "".join(p[0] for p in parts[:-1]) + last,
        ])
    seen: set[str] = set()
    deduped: list[str] = []
    for h in out:
        if h and h not in seen:
            seen.add(h)
            deduped.append(h)
    return deduped


def cmd_candidates(args: argparse.Namespace) -> str:
    cands = _generate_candidates(args.name)
    if not cands:
        return "<empty name>\n"
    return json.dumps(cands, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search public APIs by name")
    s.add_argument("name")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--timeout", type=float, default=5.0)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("candidates", help="generate handle string variants")
    s.add_argument("name")
    s.set_defaults(func=cmd_candidates)

    args = p.parse_args(argv)
    sys.stdout.write(args.func(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
