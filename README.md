# DAK Siege Rankings

Public static DAK ranking for Vanilla Game Aden sieges.

## What The Site Shows

- siege tabs by date;
- a monthly `Season` ranking by total Final score;
- a separate monthly `Avg` ranking by average Final score per played siege;
- player search and sortable columns;
- mobile-friendly player cards;
- player profile modal with siege history;
- detailed kill/death summary per siege;
- separate `По своим` accounting for official teamkills;
- clickable best-kill victim names;
- NameMC profile links and Minecraft head previews.

## Data Sources

Preferred source: the official Vanilla Game siege API used by
`https://vanilla-game.ru/lk/gamer/sieges/`.

The updater reads:

- `/lk/sieges`
- `/lk/sieges/kills?siege=<id>`

Those endpoints require an authenticated account that has access to the siege
section. Do not commit passwords or cookies. Prefer the repository secrets
`VANILLA_GAME_EMAIL` and `VANILLA_GAME_PASSWORD`; the updater will create a
fresh session automatically. If the site account uses a nickname login, use
`VANILLA_GAME_LOGIN` instead. `VANILLA_GAME_COOKIE` is still supported as a
manual fallback, but should not be needed.

Local Minecraft logs are still supported as a fallback:

```bash
python scripts/analyze.py latest.log 2026-08-29
```

## Automatic Updates

`.github/workflows/update-rankings.yml` runs every Saturday after the 19:00
Europe/Paris Aden siege window, then retries on Sunday and Monday morning in
case the first official API request fails. It can also be started manually from
GitHub Actions.

For a local official refresh:

```bash
set VANILLA_GAME_EMAIL=your_email
set VANILLA_GAME_PASSWORD=your_password
python scripts/update_from_vanilla.py
```

Optional filters:

```bash
python scripts/update_from_vanilla.py --month 2026-08
python scripts/update_from_vanilla.py --limit 5
```

## Formula

The official API is used as the event source, while this project still computes
its own DAK score:

- `Season`: monthly sum of Final score, highlighting active and consistent
  players.
- `Avg`: separate ranking by average Final score across played sieges.
- Teamkills marked by the official API count as deaths but do not award DAK kill
  value.
- Local-log fallback excludes self, NPC, guard, mob, and environmental kills from
  DAK kill value.

GitHub Pages deploys the static site from the repository after updates are
committed.
