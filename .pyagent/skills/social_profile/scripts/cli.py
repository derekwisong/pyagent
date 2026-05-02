#!/usr/bin/env python3
"""Social profile recon CLI.

Hits only public, auth-free endpoints. Returns a one-line summary
per site — name, bio, canonical URL — never timeline content.

Subcommands:
    find <username> [--timeout N]
    suggest <name>
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import requests

_UA = "pyagent-social-profile/0.1 (https://github.com/derekwisong/pyagent)"
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}


def _get(url: str, timeout: float) -> tuple[int, Any]:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
    except requests.RequestException:
        return 0, None
    try:
        return r.status_code, r.json()
    except json.JSONDecodeError:
        return r.status_code, None


def _hit(site: str, found: bool, url: str, **extra: Any) -> dict[str, Any]:
    out = {"site": site, "found": found, "url": url}
    for k, v in extra.items():
        if v:
            out[k] = v
    return out


def probe_github(u: str, timeout: float) -> dict[str, Any]:
    fallback = f"https://github.com/{u}"
    status, data = _get(f"https://api.github.com/users/{u}", timeout)
    if status == 200 and isinstance(data, dict):
        return _hit(
            "GitHub", True, data.get("html_url") or fallback,
            name=data.get("name"), bio=data.get("bio"),
        )
    return _hit("GitHub", False, fallback)


def probe_gitlab(u: str, timeout: float) -> dict[str, Any]:
    fallback = f"https://gitlab.com/{u}"
    status, data = _get(f"https://gitlab.com/api/v4/users?username={u}", timeout)
    if status == 200 and isinstance(data, list) and data:
        d = data[0]
        return _hit(
            "GitLab", True, d.get("web_url") or fallback,
            name=d.get("name"), bio=d.get("bio"),
        )
    return _hit("GitLab", False, fallback)


def probe_reddit(u: str, timeout: float) -> dict[str, Any]:
    fallback = f"https://www.reddit.com/user/{u}"
    status, data = _get(f"https://www.reddit.com/user/{u}/about.json", timeout)
    if status == 200 and isinstance(data, dict):
        d = data.get("data", {})
        if d:
            return _hit(
                "Reddit", True, fallback,
                name=d.get("name"),
                bio=(d.get("subreddit") or {}).get("public_description"),
            )
    return _hit("Reddit", False, fallback)


def probe_hackernews(u: str, timeout: float) -> dict[str, Any]:
    fallback = f"https://news.ycombinator.com/user?id={u}"
    status, data = _get(f"https://hacker-news.firebaseio.com/v0/user/{u}.json", timeout)
    if status == 200 and isinstance(data, dict):
        return _hit("Hacker News", True, fallback, bio=data.get("about"))
    return _hit("Hacker News", False, fallback)


def probe_keybase(u: str, timeout: float) -> dict[str, Any]:
    fallback = f"https://keybase.io/{u}"
    status, data = _get(
        f"https://keybase.io/_/api/1.0/user/lookup.json?usernames={u}", timeout
    )
    if status == 200 and isinstance(data, dict):
        them = data.get("them") or []
        if them and them[0]:
            d = them[0]
            profile = (d.get("profile") or {})
            return _hit(
                "Keybase", True, fallback,
                name=profile.get("full_name"), bio=profile.get("bio"),
            )
    return _hit("Keybase", False, fallback)


def probe_lobsters(u: str, timeout: float) -> dict[str, Any]:
    fallback = f"https://lobste.rs/~{u}"
    status, data = _get(f"https://lobste.rs/~{u}.json", timeout)
    if status == 200 and isinstance(data, dict) and data.get("username"):
        return _hit("Lobsters", True, fallback, bio=data.get("about"))
    return _hit("Lobsters", False, fallback)


def probe_mastodon(u: str, timeout: float) -> dict[str, Any]:
    fallback = f"https://mastodon.social/@{u}"
    status, data = _get(
        f"https://mastodon.social/api/v1/accounts/lookup?acct={u}", timeout
    )
    if status == 200 and isinstance(data, dict) and data.get("username"):
        return _hit(
            "Mastodon", True, data.get("url") or fallback,
            name=data.get("display_name"), bio=data.get("note"),
        )
    return _hit("Mastodon", False, fallback)


def probe_bluesky(u: str, timeout: float) -> dict[str, Any]:
    handle = f"{u}.bsky.social"
    fallback = f"https://bsky.app/profile/{handle}"
    status, data = _get(
        f"https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor={handle}",
        timeout,
    )
    if status == 200 and isinstance(data, dict) and data.get("handle"):
        return _hit(
            "Bluesky", True, fallback,
            name=data.get("displayName"), bio=data.get("description"),
        )
    return _hit("Bluesky", False, fallback)


PROBES: list[Callable[[str, float], dict[str, Any]]] = [
    probe_github,
    probe_gitlab,
    probe_reddit,
    probe_hackernews,
    probe_keybase,
    probe_lobsters,
    probe_mastodon,
    probe_bluesky,
]


def cmd_find(args: argparse.Namespace) -> str:
    u = args.username.strip()
    if not u:
        return "<empty username>\n"
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(PROBES)) as pool:
        futures = [pool.submit(p, u, args.timeout) for p in PROBES]
        for f in as_completed(futures):
            results.append(f.result())
    results.sort(key=lambda r: (not r["found"], r["site"]))
    return json.dumps(results, indent=2) + "\n"


def cmd_suggest(args: argparse.Namespace) -> str:
    name = args.name.strip()
    if not name:
        return "<empty name>\n"
    q = urllib.parse.quote(name)
    quoted = urllib.parse.quote(f'"{name}"')
    out = [
        {"site": "Google", "url": f"https://www.google.com/search?q={quoted}"},
        {"site": "LinkedIn (via Google)",
         "url": f"https://www.google.com/search?q=site%3Alinkedin.com%2Fin+{quoted}"},
        {"site": "X / Twitter", "url": f"https://x.com/search?q={q}&f=user"},
        {"site": "Bluesky", "url": f"https://bsky.app/search?q={q}"},
        {"site": "Mastodon",
         "url": f"https://mastodon.social/search?q={q}&type=accounts"},
        {"site": "GitHub", "url": f"https://github.com/search?q={q}&type=users"},
        {"site": "Reddit (via Google)",
         "url": f"https://www.google.com/search?q=site%3Areddit.com%2Fuser+{quoted}"},
        {"site": "Facebook (via Google)",
         "url": f"https://www.google.com/search?q=site%3Afacebook.com+{quoted}"},
        {"site": "Instagram (via Google)",
         "url": f"https://www.google.com/search?q=site%3Ainstagram.com+{quoted}"},
    ]
    return json.dumps(out, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("find", help="check a username across public APIs")
    s.add_argument("username")
    s.add_argument("--timeout", type=float, default=5.0)
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("suggest", help="search-URL templates for a name")
    s.add_argument("name")
    s.set_defaults(func=cmd_suggest)

    args = p.parse_args(argv)
    sys.stdout.write(args.func(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
