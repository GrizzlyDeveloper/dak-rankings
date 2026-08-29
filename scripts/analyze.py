#!/usr/bin/env python3
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

MC_NAME = re.compile(r"^[A-Za-z0-9_]{2,16}$")
CHAT_LINE = re.compile(r"^\[(?P<time>\d\d:\d\d:\d\d)\].*\[System\] \[CHAT\] (?P<message>.*)$")
SIEGE_START = re.compile(r"\[Осада\] Осада замка Aden началась! Продолжительность: (?P<minutes>\d+) минут")
SIEGE_END = re.compile(r"\[Осада\] Осада замка Aden завершена! Владелец: (?P<owner>\S+)")
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


def extract_siege(log_path, siege_date):
    start = None
    end = None
    duration = None
    owner = None
    death_events = []

    for line in Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines():
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
    return build_report(siege_date, start, end, duration, owner, death_events)


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


def build_report(siege_date, start, end, duration, owner, death_events):
    scored_kills = [
        event for event in death_events
        if event["killer_type"] == "player" and event["killer"] != event["victim"]
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
        kills_detail[event["killer"]].append({
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
        })

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
        "start_time": start,
        "end_time": end,
        "duration_minutes": duration,
        "owner": owner,
        "event_count": len(scored_kills),
        "death_event_count": len(death_events),
        "ignored_self_kills": sum(1 for event in death_events if event["killer_type"] == "self"),
        "ignored_non_player_deaths": sum(1 for event in death_events if event["killer_type"] in {"environment", "non_player"}),
        "median_adjusted_kd": median_adjusted,
        "median_akd": median_akd,
        "ranking": ranking,
    }


def best_kill(kill):
    if not kill:
        return None
    return {
        "victim": kill["victim"],
        "score": kill["kill_score"],
        "base_value": kill["base_kill_value"],
        "upset_multiplier": kill["upset_multiplier"],
        "farm_multiplier": kill["farm_multiplier"],
        "time": kill["time"],
        "message": kill["message"],
    }


def build_season(reports):
    players = sorted({row["player"] for report in reports for row in report["ranking"]})
    season = []
    for player in players:
        rows = [
            row for report in reports
            for row in report["ranking"]
            if row["player"] == player
        ]
        appearances = len(rows)
        total_dak = sum(row["dak"] for row in rows)
        total_final = sum(row["final_score"] for row in rows)
        season.append({
            "player": player,
            "appearances": appearances,
            "total_dak": total_dak,
            "average_dak_per_siege": total_dak / appearances if appearances else 0.0,
            "average_final_score": total_final / appearances if appearances else 0.0,
            "kills": sum(row["kills"] for row in rows),
            "deaths": sum(row["deaths"] for row in rows),
            "_season_score": total_final,
        })
    season.sort(key=lambda row: (-row["_season_score"], -row["average_final_score"], row["player"]))
    for index, row in enumerate(season, 1):
        row["rank"] = index
        del row["_season_score"]
    return season


def rankings_document(reports):
    return {
        "schema_version": "2.2",
        "system": "DAK — Difficulty Adjusted Kills",
        "generated_at": reports[0]["date"] if reports else "",
        "scope": "Aden siege only; all vanilla death messages inside the official siege window are recorded.",
        "parsing": {
            "counted": [
                "player-attributed vanilla deaths, excluding self-kills, count as DAK kills",
                "all vanilla deaths inside the Aden siege window count as deaths"
            ],
            "excluded_from_dak_kills": [
                "self-kills",
                "NPC/guard/skeleton/golem/environmental kills",
                "deaths without an identifiable player killer"
            ]
        },
        "formula": {
            "version": "DAK v2.2",
            "target_strength": {
                "adjusted_kd": "(DAK-counted kills + 1) / (all vanilla deaths + 2)",
                "reliability": "N / (N + 10), where N = DAK-counted kills + all vanilla deaths",
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
            "design_note": "Deaths include self, NPC, guard, mob, fall, crystal, and other vanilla death messages. Only player-attributed non-self deaths earn DAK kill value."
        },
        "sieges": reports,
        "season_ranking": build_season(reports),
    }


def main():
    if len(sys.argv) < 3 or len(sys.argv[1:]) % 2 != 0:
        usage()
        return 2

    reports = [
        extract_siege(log_path, siege_date)
        for log_path, siege_date in zip(sys.argv[1::2], sys.argv[2::2])
    ]
    reports.sort(key=lambda report: report["date"], reverse=True)
    for index, report in enumerate(reports, 1):
        report["name"] = f"Осада №{index}"

    root = Path(__file__).resolve().parents[1]
    reports_dir = root / "reports"
    reports_dir.mkdir(exist_ok=True)
    for report in reports:
        (reports_dir / f"{report['id']}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    (root / "data" / "rankings.json").write_text(
        json.dumps(rankings_document(reports), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
