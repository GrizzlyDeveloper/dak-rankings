#!/usr/bin/env python3
import argparse
import json
import os
import sys
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, build_opener
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen

from analyze import date_part, report_from_official, write_outputs

DEFAULT_BASE_URL = "https://vanilla-game.ru"


class AccessDeniedError(RuntimeError):
    pass


def cookie_value(cookie, name):
    for part in cookie.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return ""


def request_json(base_url, path, cookie):
    url = base_url.rstrip("/") + path
    xsrf = cookie_value(cookie, "XSRF-TOKEN")
    headers = {
        "Accept": "application/json",
        "Cookie": cookie,
        "Origin": base_url.rstrip("/"),
        "Referer": base_url.rstrip("/") + "/lk/gamer/sieges/",
        "User-Agent": "Mozilla/5.0 dak-rankings-updater/1.0",
        "X-Requested-With": "XMLHttpRequest",
    }
    if xsrf:
        headers["X-XSRF-TOKEN"] = unquote(xsrf)
    request = Request(
        url,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        if error.code in (401, 403):
            raise AccessDeniedError("Vanilla Game API denied access.") from error
        raise RuntimeError(f"GET {url} failed with HTTP {error.code}: {body[:500]}") from error


def cookie_header_from_jar(cookie_jar):
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookie_jar)


def session_request(opener, base_url, path, cookie_jar, method="GET", payload=None):
    url = base_url.rstrip("/") + path
    data = None
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": base_url.rstrip("/"),
        "Referer": base_url.rstrip("/") + "/user/login",
        "User-Agent": "Mozilla/5.0 dak-rankings-updater/1.0",
        "X-Requested-With": "XMLHttpRequest",
    }
    xsrf = next((cookie.value for cookie in cookie_jar if cookie.name == "XSRF-TOKEN"), "")
    if xsrf:
        headers["X-XSRF-TOKEN"] = unquote(xsrf)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {error.code}: {body[:500]}") from error


def login_cookie(base_url, username, password):
    if not username or not password:
        return ""
    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    try:
        session_request(opener, base_url, "/sanctum/csrf-cookie", cookie_jar)
        session_request(
            opener,
            base_url,
            "/login",
            cookie_jar,
            method="POST",
            payload={"email": username, "password": password},
        )
    except RuntimeError as error:
        if "POST " in str(error) and "/login failed with HTTP 422" in str(error):
            raise SystemExit(
                "Vanilla Game login failed. Check VANILLA_GAME_USERNAME and "
                "VANILLA_GAME_PASSWORD secrets."
            ) from error
        raise
    cookie = cookie_header_from_jar(cookie_jar)
    if not cookie_value(cookie, "vanilla_gameru_session"):
        raise RuntimeError("Login succeeded but vanilla_gameru_session cookie was not received.")
    print("Logged in to Vanilla Game with username/password secrets.")
    return cookie


def authenticated_cookie(base_url, cookie=None, username=None, password=None):
    if cookie:
        try:
            request_json(base_url, "/lk/sieges", cookie)
            return cookie
        except AccessDeniedError:
            if not username or not password:
                raise SystemExit(
                    "Vanilla Game API denied access. Refresh VANILLA_GAME_COOKIE or set "
                    "VANILLA_GAME_USERNAME and VANILLA_GAME_PASSWORD secrets for auto-login."
                )
            print("VANILLA_GAME_COOKIE was denied; trying username/password auto-login.")
    fresh_cookie = login_cookie(base_url, username, password)
    if fresh_cookie:
        return fresh_cookie
    raise SystemExit(
        "Set either VANILLA_GAME_COOKIE or VANILLA_GAME_USERNAME + VANILLA_GAME_PASSWORD. "
        "The account must be able to open /lk/gamer/sieges/."
    )


def load_fixture(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        return data
    if "payloads" in data:
        return data["payloads"]
    return [data]


def describe_siege(siege):
    value = siege.get("started_at") or siege.get("date")
    try:
        label = date_part(value) if value is not None else "unknown-date"
    except (TypeError, ValueError, OSError):
        label = str(value or "unknown-date")
    siege_id = siege.get("id")
    if siege_id is None:
        return label
    return f"{label} (id={siege_id})"


def fetch_payloads(base_url, cookie, month=None, limit=None):
    index = request_json(base_url, "/lk/sieges", cookie)
    sieges = index.get("sieges")
    if not isinstance(sieges, list):
        raise RuntimeError("Unexpected /lk/sieges response: missing sieges[]")

    selected = []
    for siege in sieges:
        siege_date = date_part(siege.get("started_at") or siege.get("date"))
        if month and not siege_date.startswith(month):
            continue
        selected.append(siege)

    if limit:
        selected = selected[:limit]

    if selected:
        print(f"Selected {len(selected)} siege(s). Latest selected: {describe_siege(selected[0])}.")
    else:
        print("Selected 0 sieges.")

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
    parser.add_argument(
        "--username",
        default=(
            os.getenv("VANILLA_GAME_USERNAME")
            or os.getenv("VANILLA_GAME_LOGIN")
            or os.getenv("VANILLA_GAME_EMAIL")
        ),
    )
    parser.add_argument("--password", default=os.getenv("VANILLA_GAME_PASSWORD"))
    parser.add_argument("--month", help="Only import sieges from YYYY-MM.")
    parser.add_argument("--limit", type=int, help="Maximum number of sieges to import.")
    parser.add_argument("--input", help="Offline fixture with one payload, payloads[], or a list of payloads.")
    parser.add_argument("--check-auth", action="store_true", help="Only check access to /lk/sieges.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if args.input:
        payloads = load_fixture(args.input)
    else:
        cookie = authenticated_cookie(args.base_url, args.cookie, args.username, args.password)
        if args.check_auth:
            index = request_json(args.base_url, "/lk/sieges", cookie)
            sieges = index.get("sieges")
            count = len(sieges) if isinstance(sieges, list) else 0
            print(f"Authenticated. /lk/sieges returned {count} sieges.")
            if count:
                print(f"Latest listed siege: {describe_siege(sieges[0])}.")
            return 0
        payloads = fetch_payloads(args.base_url, cookie, args.month, args.limit)

    reports = [report_from_official(payload) for payload in payloads]
    if not reports:
        raise SystemExit("No siege payloads were imported.")
    write_outputs(reports)
    dates = ", ".join(report["date"] for report in reports[:5])
    suffix = "..." if len(reports) > 5 else ""
    print(f"Imported {len(reports)} siege reports from Vanilla Game: {dates}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
