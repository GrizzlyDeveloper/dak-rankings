#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from analyze import date_part, report_from_official, write_outputs

DEFAULT_BASE_URL = "https://vanilla-game.ru"


def request_json(base_url, path, cookie):
    url = base_url.rstrip("/") + path
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Cookie": cookie,
            "User-Agent": "dak-rankings-updater/1.0",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        if error.code in (401, 403):
            raise SystemExit(
                "Vanilla Game API denied access. Set VANILLA_GAME_COOKIE from an account "
                "that can open /lk/gamer/sieges/."
            ) from error
        raise RuntimeError(f"GET {url} failed with HTTP {error.code}: {body[:500]}") from error


def load_fixture(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if "payloads" in data:
        return data["payloads"]
    return [data]


def fetch_payloads(base_url, cookie, month=None, limit=None):
    index = request_json(base_url, "/lk/sieges", cookie)
    sieges = index.get("sieges")
    if not isinstance(sieges, list):
        raise RuntimeError("Unexpected /lk/sieges response: missing sieges[]")

    selected = []
    for siege in sieges:
        siege_date = date_part(siege.get("started_at") or siege.get("date") or siege.get("id"))
        if month and not siege_date.startswith(month):
            continue
        selected.append(siege)

    if limit:
        selected = selected[:limit]

    payloads = []
    for siege in selected:
        siege_id = siege.get("id")
        if siege_id is None:
            continue
        query = urlencode({"siege": siege_id})
        payload = request_json(base_url, f"/lk/sieges/kills?{query}", cookie)
        if not payload.get("siege"):
            payload["siege"] = siege
        payloads.append(payload)
    return payloads


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Fetch official Vanilla Game siege logs and regenerate DAK rankings."
    )
    parser.add_argument("--base-url", default=os.getenv("VANILLA_GAME_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--cookie", default=os.getenv("VANILLA_GAME_COOKIE"))
    parser.add_argument("--month", help="Only import sieges from YYYY-MM.")
    parser.add_argument("--limit", type=int, help="Maximum number of sieges to import.")
    parser.add_argument("--input", help="Offline fixture with one payload, payloads[], or a list of payloads.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if args.input:
        payloads = load_fixture(args.input)
    else:
        if not args.cookie:
            raise SystemExit(
                "Set VANILLA_GAME_COOKIE before fetching official siege data. "
                "The cookie must belong to an account that can open /lk/gamer/sieges/."
            )
        payloads = fetch_payloads(args.base_url, args.cookie, args.month, args.limit)

    reports = [report_from_official(payload) for payload in payloads]
    if not reports:
        raise SystemExit("No siege payloads were imported.")
    write_outputs(reports)
    print(f"Imported {len(reports)} siege reports from Vanilla Game.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
