#!/usr/bin/env python3
import json
import gzip
import math
import re
import statistics
import sys
from datetime import datetime
from collections import Counter, defaultdict
from pathlib import Path

MC_NAME = re.compile(r"^[A-Za-z0-9_]{2,16}$")
SIEGE_DEATH_LIMIT = 10
CHAT_LINE = re.compile(r"^\[(?P<time>\d\d:\d\d:\d\d)\].*\[System\] \[CHAT\] (?P<message>.*)$")
SIEGE_START = re.compile(r"\[.*?\].*Aden.*:\s*(?P<minutes>\d+)\D*$")
SIEGE_END = re.compile(r"\[.*?\].*Aden.*:\s*(?P<owner>\S+)\s*$")
BY_PLAYERISH = re.compile(
    r"^(?P<victim>[A-Za-z0-9_]{2,16}) was "
    r"(?P<verb>killed|smashed|blown up|slain|shot|doomed to fall) by (?P<killer>.+)$"
)
KINETIC_ESCAPE = re.compile(
    r"^(?P<victim>[A-Za-z0-9_]{2,16}) experienced kinetic energy while trying to escape (?P<killer>[A-Za-z0-9_]{2,16})$"
)
FIGHTING_DEATH = re.compile(
    r"^(?P<victim>[A-Za-z0-9_]{2,16}) "
    r"(?:was burned to a crisp|hit the ground too hard) while fighting (?P<killer>[A-Za-z0-9_]{2,16})$"
)
HURT_DEATH = re.compile(
    r"^(?P<victim>[A-Za-z0-9_]{2,16}) was killed while trying to hurt (?P<killer>[A-Za-z0-9_]{2,16})$"
)
PLAIN_DEATH = re.compile(
    r"^(?P<victim>[A-Za-z0-9_]{2,16}) "
    r"(?:died|fell from a high place|hit the ground too hard|experienced kinetic energy|"
    r"was impaled on a stalagmite|was doomed to fall|was killed by \[Intentional Game Design\]|"
    r"drowned|suffocated in a wall|starved to death|froze to death|withered away|"
    r"went up in flames|burned to death|walked into fire)"
)


def usage():
    print("Usage: python scripts/analyze.py <log> <date> [<log> <date> ...]", file=sys.stderr)
    print('Example: python scripts/analyze.py latest.log 2026-08-29 "2026-08-22-2.log" 2026-08-22', file=sys.stderr)


def seconds(value):
    hours, minutes, secs = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + secs


def strip_killer_suffix(value):
    value = value.strip()
    return re.split(r"\s+(?:with|using)\s+\[", value, 1)[0].strip()


def classify_killer(victim, killer):
    if not killer:
        return None, "environment"
    killer = strip_killer_suffix(killer)
    if killer == victim:
        return killer, "self"
    if MC_NAME.match(killer):
        return killer, "player"
    return killer, "non_player"


def parse_death(time, message):
    if " » " in message or message.startswith("["):
        return None

    match = BY_PLAYERISH.match(message)
    if match:
        victim = match.group("victim")
        killer, killer_type = classify_killer(victim, match.group("killer"))
        return death_event(time, victim, killer, killer_type, message)

    for pattern in (KINETIC_ESCAPE, FIGHTING_DEATH, HURT_DEATH):
        match = pattern.match(message)
        if match:
            victim = match.group("victim")
            killer, killer_type = classify_killer(victim, match.group("killer"))
            return death_event(time, victim, killer, killer_type, message)

    match = PLAIN_DEATH.match(message)
    if match:
        return death_event(time, match.group("victim"), None, "environment", message)

    return None


def death_event(time, victim, killer, killer_type, message):
    return {
        "time": time,
        "victim": victim,
        "killer": killer,
        "killer_type": killer_type,
        "message": message,
    }


def read_log_lines(log_path):
    path = Path(log_path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def extract_siege(log_path, siege_date):
    start = None
    end = None
    duration = None
    owner = None
    death_events = []

    for line in read_log_lines(log_path):
        match = CHAT_LINE.match(line)
        if not match:
            continue
        time = match.group("time")
        message = match.group("message")

        if start is None:
            start_match = SIEGE_START.search(message)
            if start_match:
                start = time
                duration = int(start_match.group("minutes"))
            continue

        end_match = SIEGE_END.search(message)
        if end_match:
            end = time
            owner = end_match.group("owner")
            break

        death = parse_death(time, message)
        if death:
            death_events.append(death)

    if start is None or end is None:
        raise ValueError(f"Aden siege start/end was not found in {log_path}")

    death_events = [
        event for event in death_events
        if seconds(start) <= seconds(event["time"]) <= seconds(end)
    ]
    raw_death_event_count = len(death_events)
    death_events, ignored_after_limit = apply_death_limit(death_events)
    return build_report(
        siege_date,
        start,
        end,
        duration,
        owner,
        death_events,
        raw_death_event_count,
        ignored_after_limit,
    )


def apply_death_limit(death_events):
    counts = Counter()
    kept = []
    ignored = []
    for event in death_events:
        counts[event["victim"]] += 1
        if counts[event["victim"]] <= SIEGE_DEATH_LIMIT:
            kept.append(event)
        else:
            ignored.append(event)
    return kept, ignored


def percentile_map(values):
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    if not ordered:
        return {}
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0}
    return {name: index / (len(ordered) - 1) for index, (name, _) in enumerate(ordered)}


def farm_multiplier(total_repeats, victim_percentile):
    if victim_percentile >= 0.65:
        if total_repeats <= 4:
            return 1.0
        if total_repeats == 5:
            return 0.85
        return 0.75
    if total_repeats <= 2:
        return 1.0
    if total_repeats == 3:
        return 0.90
    if total_repeats == 4:
        return 0.75
    if total_repeats == 5:
        return 0.60
    return 0.50


def upset_multiplier(victim_percentile, killer_percentile):
    advantage = victim_percentile - killer_percentile
    if advantage >= 0.50:
        return 1.35
    if advantage >= 0.30:
        return 1.20
    if advantage >= 0.20:
        return 1.10
    return 1.0


def median(values, default=0.0):
    return statistics.median(values) if values else default


def is_scored_kill(event):
    return (
        event["killer_type"] == "player"
        and event["killer"] != event["victim"]
        and not event.get("teamkill")
    )


def build_report(
    siege_date,
    start,
    end,
    duration,
    owner,
    death_events,
    raw_death_event_count,
    ignored_after_limit,
    source="minecraft_log",
    source_meta=None,
):
    scored_kills = [
        event for event in death_events
        if is_scored_kill(event)
    ]
    players = set()
    for event in death_events:
        players.add(event["victim"])
    for event in scored_kills:
        players.add(event["killer"])

    kills = Counter(event["killer"] for event in scored_kills)
    deaths = Counter(event["victim"] for event in death_events)
    adjusted = {player: (kills[player] + 1) / (deaths[player] + 2) for player in players}
    median_adjusted = median(list(adjusted.values()), 0.25)

    strength = {}
    for player in players:
        sample_size = kills[player] + deaths[player]
        reliability = sample_size / (sample_size + 10)
        strength[player] = reliability * adjusted[player] + (1 - reliability) * median_adjusted
    strength_percentile = percentile_map(strength)

    repeat_counts = Counter((event["killer"], event["victim"]) for event in scored_kills)
    kills_detail = defaultdict(list)
    death_details = defaultdict(list)
    upset_counts = Counter()
    farm_counts = Counter()

    for event in death_events:
        death_details[event["victim"]].append(dict(event))

    for event in scored_kills:
        victim_percentile = strength_percentile.get(event["victim"], 0.0)
        killer_percentile = strength_percentile.get(event["killer"], 0.0)
        base_value = 1 + 2 * math.pow(victim_percentile, 1.4)
        upset = upset_multiplier(victim_percentile, killer_percentile)
        repeats = repeat_counts[(event["killer"], event["victim"])]
        farm = farm_multiplier(repeats, victim_percentile)
        score = base_value * upset * farm
        if upset > 1:
            upset_counts[event["killer"]] += 1
        if farm < 1:
            farm_counts[event["killer"]] += 1
        detail = {
            "time": event["time"],
            "victim": event["victim"],
            "killer": event["killer"],
            "message": event["message"],
            "victim_strength": strength[event["victim"]],
            "victim_percentile": victim_percentile,
            "base_kill_value": base_value,
            "upset_multiplier": upset,
            "farm_multiplier": farm,
            "target_repeat_count": repeats,
            "kill_score": score,
        }
        for key in (
            "cause",
            "weapon",
            "distance",
            "killer_clan",
            "victim_clan",
            "teamkill",
            "official_final_score",
            "official_base_score",
            "official_repeat_mult",
            "official_resistance_mult",
            "official_id",
            "ts",
        ):
            if key in event:
                detail[key] = event[key]
        kills_detail[event["killer"]].append(detail)

    dak = {player: sum(kill["kill_score"] for kill in kills_detail[player]) for player in players}
    akd_values = [dak[player] / kills[player] for player in players if kills[player] > 0]
    median_akd = median(akd_values, 0.0)

    ranking = []
    for player in players:
        player_kills = kills[player]
        player_deaths = deaths[player]
        player_dak = dak[player]
        akd = player_dak / player_kills if player_kills else 0.0
        quality_index = 0.0
        final_score = 0.0
        if player_kills and median_akd > 0:
            quality_index = 1 + (player_kills / (player_kills + 10)) * (akd / median_akd - 1)
            quality_index = max(0.01, quality_index)
            final_score = player_dak * math.pow(quality_index, 0.35)
        best = max(kills_detail[player], key=lambda kill: kill["kill_score"], default=None)
        ranking.append({
            "player": player,
            "kills": player_kills,
            "deaths": player_deaths,
            "kd": player_kills / player_deaths if player_deaths else None,
            "adjusted_kd": adjusted[player],
            "strength": strength[player],
            "strength_percentile": strength_percentile.get(player, 0.0),
            "dak": player_dak,
            "akd": akd,
            "quality_index": quality_index,
            "final_score": final_score,
            "upset_kills": upset_counts[player],
            "farm_affected_kills": farm_counts[player],
            "best_kill": best_kill(best),
            "kills_detail": sorted(kills_detail[player], key=lambda kill: kill["time"]),
            "death_details": sorted(death_details[player], key=lambda death: death["time"]),
        })

    ranking.sort(key=lambda row: (-row["final_score"], -row["dak"], -row["kills"], row["player"]))
    for index, row in enumerate(ranking, 1):
        row["rank"] = index

    return {
        "id": siege_date,
        "date": siege_date,
        "name": f"Осада {siege_date}",
        "source": source,
        "source_meta": source_meta or {},
        "start_time": start,
        "end_time": end,
        "duration_minutes": duration,
        "owner": owner,
        "event_count": len(scored_kills),
        "death_event_count": len(death_events),
        "raw_death_event_count": raw_death_event_count,
        "death_limit_per_player": SIEGE_DEATH_LIMIT,
        "ignored_deaths_after_limit": len(ignored_after_limit),
        "ignored_deaths_after_limit_detail": ignored_after_limit,
        "ignored_self_kills": sum(1 for event in death_events if event["killer_type"] == "self"),
        "ignored_team_kills": sum(1 for event in death_events if event.get("teamkill")),
        "ignored_non_player_deaths": sum(1 for event in death_events if event["killer_type"] in {"environment", "non_player"}),
        "median_adjusted_kd": median_adjusted,
        "median_akd": median_akd,
        "ranking": ranking,
    }


def best_kill(kill):
    if not kill:
        return None
    result = {
        "victim": kill["victim"],
        "score": kill["kill_score"],
        "base_value": kill["base_kill_value"],
        "upset_multiplier": kill["upset_multiplier"],
        "farm_multiplier": kill["farm_multiplier"],
        "time": kill["time"],
        "message": kill["message"],
    }
    for key in ("cause", "weapon", "distance", "victim_clan", "teamkill", "official_final_score"):
        if key in kill:
            result[key] = kill[key]
    return result


def parse_datetime(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:len(fmt)], fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def date_part(value, fallback=""):
    parsed = parse_datetime(value)
    if parsed:
        return parsed.date().isoformat()
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or fallback))
    return match.group(0) if match else fallback


def time_part(value, fallback="00:00:00"):
    parsed = parse_datetime(value)
    if parsed:
        return parsed.strftime("%H:%M:%S")
    match = re.search(r"\b\d{2}:\d{2}:\d{2}\b", str(value or ""))
    return match.group(0) if match else fallback


def month_id(report):
    return report["date"][:7]


def month_label(month):
    names = {
        "01": "January", "02": "February", "03": "March", "04": "April",
        "05": "May", "06": "June", "07": "July", "08": "August",
        "09": "September", "10": "October", "11": "November", "12": "December",
    }
    year, mon = month.split("-", 1)
    return f"{names.get(mon, mon)} {year}"


def build_player_summary(reports):
    players = sorted({row["player"] for report in reports for row in report["ranking"]})
    summaries = []
    for player in players:
        rows = [
            row for report in reports
            for row in report["ranking"]
            if row["player"] == player
        ]
        appearances = len(rows)
        total_dak = sum(row["dak"] for row in rows)
        total_final = sum(row["final_score"] for row in rows)
        summaries.append({
            "player": player,
            "appearances": appearances,
            "total_dak": total_dak,
            "average_dak_per_siege": total_dak / appearances if appearances else 0.0,
            "average_final_score": total_final / appearances if appearances else 0.0,
            "kills": sum(row["kills"] for row in rows),
            "deaths": sum(row["deaths"] for row in rows),
            "_season_score": total_final,
        })
    return summaries


def rank_summary_rows(rows, mode):
    if mode == "average":
        rows.sort(key=lambda row: (-row["average_final_score"], -row["_season_score"], row["player"]))
    else:
        rows.sort(key=lambda row: (-row["_season_score"], -row["average_final_score"], row["player"]))
    for index, row in enumerate(rows, 1):
        row["rank"] = index
        del row["_season_score"]
    return rows


def build_season(reports):
    return rank_summary_rows(build_player_summary(reports), "season")


def build_average(reports):
    return rank_summary_rows(build_player_summary(reports), "average")


def build_seasons(reports):
    seasons = []
    for month in sorted({month_id(report) for report in reports}, reverse=True):
        month_reports = [report for report in reports if month_id(report) == month]
        seasons.append({
            "id": month,
            "label": month_label(month),
            "siege_ids": [report["id"] for report in month_reports],
            "siege_count": len(month_reports),
            "event_count": sum(report["event_count"] for report in month_reports),
            "death_event_count": sum(report["death_event_count"] for report in month_reports),
            "season_ranking": build_season(month_reports),
            "average_ranking": build_average(month_reports),
        })
    return seasons


def official_message(row):
    killer = row.get("killer") or "unknown"
    victim = row.get("victim") or "unknown"
    details = [str(row[key]) for key in ("cause", "weapon") if row.get(key)]
    suffix = f" ({', '.join(details)})" if details else ""
    return f"{victim} was killed by {killer}{suffix}"


def official_event(row):
    victim = row.get("victim")
    killer = row.get("killer")
    if not victim or not killer:
        return None
    event = {
        "time": time_part(row.get("ts") or row.get("created_at") or row.get("time")),
        "victim": victim,
        "killer": killer,
        "killer_type": "self" if killer == victim else "player",
        "message": row.get("message") or official_message(row),
        "source": "vanilla_game",
    }
    mapping = {
        "id": "official_id",
        "ts": "ts",
        "cause": "cause",
        "weapon": "weapon",
        "distance": "distance",
        "killer_clan": "killer_clan",
        "victim_clan": "victim_clan",
        "teamkill": "teamkill",
        "final_score": "official_final_score",
        "base_score": "official_base_score",
        "repeat_mult": "official_repeat_mult",
        "resistance_mult": "official_resistance_mult",
    }
    for source_key, target_key in mapping.items():
        if source_key in row:
            event[target_key] = row[source_key]
    return event


def report_from_official(payload):
    siege = payload.get("siege") or {}
    rows = payload.get("rows") or []
    siege_id = str(siege.get("id") or payload.get("id") or date_part(siege.get("started_at")))
    siege_date = date_part(siege.get("started_at") or siege.get("date") or siege_id, siege_id[:10])
    start = time_part(siege.get("started_at"))
    end = time_part(siege.get("finished_at") or siege.get("ended_at"), start)
    events = [event for event in (official_event(row) for row in rows) if event]
    report = build_report(
        siege_date,
        start,
        end,
        None,
        payload.get("winner") or siege.get("winner") or siege.get("owner"),
        events,
        len(events),
        [],
        source="vanilla_game_api",
        source_meta={
            "siege_id": siege_id,
            "castle_id": siege.get("castle_id"),
            "castle_name": siege.get("castle_name"),
            "official_totals": payload.get("totals"),
            "official_top_count": len(payload.get("top") or []),
        },
    )
    report["id"] = siege_date if siege_id == siege_date else f"{siege_date}-{siege_id}"
    return report


def rankings_document(reports):
    seasons = build_seasons(reports)
    current_season = seasons[0]["id"] if seasons else ""
    return {
        "schema_version": "2.3",
        "system": "DAK — Difficulty Adjusted Kills",
        "generated_at": reports[0]["date"] if reports else "",
        "scope": "Aden siege only. Official Vanilla Game siege API is preferred; local logs are supported as a fallback.",
        "parsing": {
            "counted": [
                "official siege API kill rows count as participant deaths",
                "player-attributed non-teamkill deaths count as DAK kills",
                "local-log fallback counts vanilla deaths inside the Aden siege window until the per-player siege death limit"
            ],
            "excluded_from_dak_kills": [
                "self-kills",
                "same-clan teamkills when the official API marks teamkill=true",
                "NPC/guard/skeleton/golem/environmental kills",
                "deaths without an identifiable player killer"
            ]
        },
        "formula": {
            "version": "DAK v2.3",
            "target_strength": {
                "adjusted_kd": "(DAK-counted kills + 1) / (counted siege deaths + 2)",
                "reliability": "N / (N + 10), where N = DAK-counted kills + counted siege deaths",
                "strength": "reliability * AdjustedKD + (1 - reliability) * siege_median_AdjustedKD",
                "percentile": "rank percentile within the siege"
            },
            "kill_value": "1 + 2 * percentile^1.4",
            "upset_bonus": {
                "<20 percentage points": "x1.00",
                "20-29 points": "x1.10",
                "30-49 points": "x1.20",
                "50+ points": "x1.35"
            },
            "farm_penalty": {
                "weak_or_average_target": "kills 1-2 x1.00; 3 x0.90; 4 x0.75; 5 x0.60; 6+ x0.50",
                "strong_target": "kills 1-4 x1.00; 5 x0.85; 6+ x0.75"
            },
            "dak": "sum(KillValue * UpsetMultiplier * FarmMultiplier)",
            "quality_index": "1 + (K / (K + 10)) * (AKD / median_AKD - 1)",
            "final_score": "DAK * QualityIndex^0.35",
            "season": "monthly sum of Final for all sieges in the selected month",
            "average_score": "average Final per played siege in the selected month",
            "design_note": "Season and Avg are separate rankings: Season rewards monthly activity and consistency, Avg shows the best average result per played siege."
        },
        "sieges": reports,
        "current_season": current_season,
        "seasons": seasons,
        "season_ranking": seasons[0]["season_ranking"] if seasons else [],
        "average_ranking": seasons[0]["average_ranking"] if seasons else [],
    }


def write_outputs(reports, root=None):
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    reports.sort(key=lambda report: report["date"], reverse=True)
    for index, report in enumerate(reports, 1):
        report["name"] = f"Осада №{index}"

    reports_dir = root / "reports"
    reports_dir.mkdir(exist_ok=True)
    for old_report in reports_dir.glob("*.json"):
        old_report.unlink()
    for report in reports:
        (reports_dir / f"{report['id']}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    (root / "data" / "rankings.json").write_text(
        json.dumps(rankings_document(reports), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    if len(sys.argv) < 3 or len(sys.argv[1:]) % 2 != 0:
        usage()
        return 2

    reports = [
        extract_siege(log_path, siege_date)
        for log_path, siege_date in zip(sys.argv[1::2], sys.argv[2::2])
    ]
    write_outputs(reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
