#!/usr/bin/env python3
"""
Normalize ESPN league payloads into a platform-neutral shape.

Phase 1b of the waiver pickup simulator. Everything downstream of this
file -- valuation, waiver rules, opponent model, UI -- reads the output
of this script and never touches ESPN's own field names. When Yahoo and
CBS arrive, they get their own reader and emit this same shape.

Writes two things:

  data/leagues.json
      current state of every configured league, overwritten each run

  data/snapshots/<key>/<UTC timestamp>.json
      compact point-in-time record of rosters and waiver order,
      appended to and never overwritten

The snapshots are the important half. ESPN does not expose per-claim
transaction history at any reachable endpoint, so diffing consecutive
snapshots is how this project builds its own transaction log: a player
whose owning team changes between two snapshots is an add and a drop,
and whether the acquiring team's waiver rank fell to last distinguishes
a waiver claim from a free agent pickup.

Standard library only.
"""

import datetime
import json
import os
import pathlib
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# League configuration
# ---------------------------------------------------------------------------
#
# Stays in the script while there are two leagues on one platform. It
# moves to a config file when Yahoo and CBS join and the shape needs a
# platform discriminator.

LEAGUES = [
    {"key": "dtown", "espn_id": "65142363", "label": "D-Town Boogie"},
    {"key": "alpine", "espn_id": "854943363", "label": "ALPINE 2026"},
]

SEASON = int(os.environ.get("ESPN_SEASON", "2026"))

# The live season. Anything else is a backtest and goes to its own file so
# a historical pull can never overwrite current league state.
LIVE_SEASON = int(os.environ.get("ESPN_LIVE_SEASON", "2026"))
IS_BACKTEST = SEASON != LIVE_SEASON

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
VIEWS = ["mSettings", "mTeam", "mRoster"]

DATA = pathlib.Path("data")
SNAPSHOTS = DATA / "snapshots"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; alpine-waiver-sim/1.0)",
    "Accept": "application/json",
}

ESPN_S2 = os.environ.get("ESPN_S2", "")
SWID = os.environ.get("SWID", "")


# ---------------------------------------------------------------------------
# ESPN's integer vocabularies
# ---------------------------------------------------------------------------
#
# Confirmed against live payloads from both leagues. Unknown ids are kept
# as-is rather than dropped, so an ESPN change shows up as an odd slot
# name instead of silently vanishing data.

SLOT_NAMES = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE",
    6: "TE", 7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL",
    12: "CB", 13: "S", 14: "DB", 15: "DP", 16: "DST", 17: "K",
    18: "P", 19: "HC", 20: "BENCH", 21: "IR", 22: "UNKNOWN22",
    23: "FLEX", 24: "EDR",
}

POSITION_NAMES = {
    1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 7: "P", 9: "DT",
    10: "DE", 11: "LB", 12: "CB", 13: "S", 14: "DB", 16: "DST",
}

# The single scoring item that decides PPR flavour.
RECEPTIONS_STAT_ID = 53

# Slots a player can occupy that do not count as a starting spot.
NON_STARTING_SLOTS = {"BENCH", "IR"}


def slot_name(slot_id):
    return SLOT_NAMES.get(slot_id, "SLOT{}".format(slot_id))


def position_name(position_id):
    return POSITION_NAMES.get(position_id, "POS{}".format(position_id))


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_league(espn_id, season):
    query = "&".join("view=" + v for v in VIEWS)
    url = "{base}/seasons/{season}/segments/0/leagues/{league}?{query}".format(
        base=BASE, season=season, league=espn_id, query=query)

    headers = dict(HEADERS)
    if ESPN_S2 and SWID:
        headers["Cookie"] = "espn_s2={}; SWID={}".format(ESPN_S2, SWID)

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response), None
    except urllib.error.HTTPError as err:
        hint = ""
        if err.code in (401, 403):
            hint = ("  <-- authentication rejected. The espn_s2 secret has "
                    "probably expired; log into ESPN and refresh it.")
        return None, "HTTP {} {}{}".format(err.code, err.reason, hint)
    except urllib.error.URLError as err:
        return None, "connection failed -- {}".format(err.reason)


# ---------------------------------------------------------------------------
# Normalize
# ---------------------------------------------------------------------------


def normalize_scoring(settings):
    scoring = settings.get("scoringSettings") or {}
    items = {}
    reception_value = None
    for item in scoring.get("scoringItems", []):
        stat_id = item.get("statId")
        points = item.get("points")
        items[str(stat_id)] = points
        if stat_id == RECEPTIONS_STAT_ID:
            reception_value = points

    if reception_value is None:
        flavour = "unknown"
    elif reception_value >= 1:
        flavour = "full_ppr"
    elif reception_value > 0:
        flavour = "half_ppr"
    else:
        flavour = "standard"

    return {
        "flavour": flavour,
        "reception_value": reception_value,
        "rank_type": scoring.get("playerRankType"),
        "scoring_type": scoring.get("scoringType"),
        "items": items,
    }


def normalize_lineup(settings):
    roster = settings.get("rosterSettings") or {}
    counts = roster.get("lineupSlotCounts") or {}

    slots, starters, bench, ir = {}, 0, 0, 0
    for raw_id, count in counts.items():
        if not count:
            continue
        name = slot_name(int(raw_id))
        slots[name] = count
        if name == "BENCH":
            bench = count
        elif name == "IR":
            ir = count
        else:
            starters += count

    limits = {}
    for raw_id, cap in (roster.get("positionLimits") or {}).items():
        if cap and cap > 0:
            limits[position_name(int(raw_id))] = cap

    return {
        "slots": slots,
        "starting_spots": starters,
        "bench_spots": bench,
        "ir_spots": ir,
        "roster_size": starters + bench,
        "position_limits": limits,
    }


def normalize_waivers(settings):
    acquisition = settings.get("acquisitionSettings") or {}
    resets = acquisition.get("waiverOrderReset")

    if acquisition.get("isUsingAcquisitionBudget"):
        mode = "faab"
    elif resets:
        mode = "weekly_reset"
    else:
        mode = "rolling"

    return {
        "mode": mode,
        "order_resets": resets,
        "uses_budget": acquisition.get("isUsingAcquisitionBudget"),
        "process_days": acquisition.get("waiverProcessDays") or [],
        "process_hour": acquisition.get("waiverProcessHour"),
        "waiver_hours": acquisition.get("waiverHours"),
        "acquisition_limit": acquisition.get("acquisitionLimit"),
    }


def normalize_owner(value):
    """Compare owner GUIDs without caring about braces or case."""
    if not value:
        return None
    return str(value).strip().strip("{}").upper()


MY_OWNER_ID = normalize_owner(SWID)


def normalize_team(team):
    entries = (team.get("roster") or {}).get("entries") or []
    roster = []
    for entry in entries:
        pool = entry.get("playerPoolEntry") or {}
        player = pool.get("player") or {}
        roster.append({
            "player_id": player.get("id") or entry.get("playerId"),
            "name": player.get("fullName"),
            "position": position_name(player.get("defaultPositionId")),
            "slot": slot_name(entry.get("lineupSlotId")),
            "injury_status": player.get("injuryStatus"),
            "eligible_slots": [slot_name(s)
                               for s in (player.get("eligibleSlots") or [])],
        })

    record = ((team.get("record") or {}).get("overall")) or {}
    counter = team.get("transactionCounter") or {}
    owners = team.get("owners") or []

    name = team.get("name") or " ".join(
        p for p in (team.get("location", ""), team.get("nickname", "")) if p)

    owner_id = owners[0] if owners else team.get("primaryOwner")

    return {
        "team_id": team.get("id"),
        "name": (name or "").strip() or "(unnamed)",
        "abbrev": team.get("abbrev"),
        "owner_id": owner_id,
        # Your SWID cookie is your owner GUID, so the team you manage
        # identifies itself. No config field to keep in sync.
        "is_me": bool(MY_OWNER_ID)
                 and normalize_owner(owner_id) == MY_OWNER_ID,
        "waiver_rank": team.get("waiverRank"),
        "playoff_seed": team.get("playoffSeed"),
        "record": {
            "wins": record.get("wins"),
            "losses": record.get("losses"),
            "ties": record.get("ties"),
            "points_for": record.get("pointsFor"),
        },
        "activity": {
            "acquisitions": counter.get("acquisitions"),
            "drops": counter.get("drops"),
            "trades": counter.get("trades"),
        },
        "roster": roster,
    }


def normalize(config, season, payload):
    settings = payload.get("settings") or {}
    teams = [normalize_team(t) for t in (payload.get("teams") or [])]
    teams.sort(key=lambda t: t["team_id"])

    return {
        "key": config["key"],
        "platform": "espn",
        "platform_league_id": config["espn_id"],
        "season": season,
        "name": settings.get("name") or config["label"],
        "size": settings.get("size"),
        "is_public": settings.get("isPublic"),
        "current_week": payload.get("scoringPeriodId"),
        "scoring": normalize_scoring(settings),
        "lineup": normalize_lineup(settings),
        "waivers": normalize_waivers(settings),
        "teams": teams,
    }


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def snapshot_of(league):
    """
    The minimum needed to reconstruct a transaction by diffing: who owns
    which player, and where every team sits in the waiver order.
    """
    return {
        "captured_at": league["captured_at"],
        "season": league["season"],
        "week": league["current_week"],
        "waiver_order": {str(t["team_id"]): t["waiver_rank"]
                         for t in league["teams"]},
        "rosters": {str(t["team_id"]): [p["player_id"] for p in t["roster"]]
                    for t in league["teams"]},
    }


def write_snapshot(league):
    directory = SNAPSHOTS / league["key"]
    directory.mkdir(parents=True, exist_ok=True)

    stamp = league["captured_at"].replace(":", "").replace("-", "")[:13]
    path = directory / "{}.json".format(stamp)

    if path.exists():
        return path, False
    path.write_text(json.dumps(snapshot_of(league), indent=2))
    return path, True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    captured_at = datetime.datetime.now(
        datetime.timezone.utc).replace(microsecond=0).isoformat()

    print("season {}   captured_at {}{}".format(
        SEASON, captured_at, "   [BACKTEST]" if IS_BACKTEST else ""))
    print("auth: {}\n".format(
        "cookies present" if (ESPN_S2 and SWID) else "ANONYMOUS"))

    leagues, failures = [], []

    for config in LEAGUES:
        print("-" * 68)
        print("{}  (espn {})".format(config["label"], config["espn_id"]))

        payload, error = fetch_league(config["espn_id"], SEASON)
        if error:
            print("  FAILED: {}".format(error))
            failures.append(config["key"])
            continue

        league = normalize(config, SEASON, payload)
        league["captured_at"] = captured_at
        leagues.append(league)

        scoring = league["scoring"]
        lineup = league["lineup"]
        waivers = league["waivers"]

        print("  scoring:  {} (reception {})".format(
            scoring["flavour"], scoring["reception_value"]))
        print("  lineup:   {} starting, {} bench, {} IR  ->  {} spots".format(
            lineup["starting_spots"], lineup["bench_spots"],
            lineup["ir_spots"], lineup["roster_size"]))
        print("  slots:    " + ", ".join(
            "{}x{}".format(k, v) for k, v in sorted(lineup["slots"].items())))
        print("  limits:   " + (", ".join(
            "{}<={}".format(k, v)
            for k, v in sorted(lineup["position_limits"].items())) or "none"))
        print("  waivers:  mode={}  {} days/week at hour {}  ({}h period)".format(
            waivers["mode"], len(waivers["process_days"]),
            waivers["process_hour"], waivers["waiver_hours"]))
        print("  teams:    {} normalized, {} rostered players".format(
            len(league["teams"]),
            sum(len(t["roster"]) for t in league["teams"])))
        mine = [t for t in league["teams"] if t.get("is_me")]
        if mine:
            print("  your team: {} (id {})".format(
                mine[0]["name"], mine[0]["team_id"]))
        elif MY_OWNER_ID:
            print("  your team: NOT FOUND -- no team owner matches the SWID "
                  "secret")
        else:
            print("  your team: unknown (no SWID configured)")

        if IS_BACKTEST:
            print("  snapshot: skipped (backtest season)")
        else:
            path, created = write_snapshot(league)
            print("  snapshot: {} {}".format(
                path, "written" if created else "(already exists this hour)"))

    if not leagues:
        print("\nNo leagues normalized. Nothing written.")
        raise SystemExit(1)

    DATA.mkdir(exist_ok=True)
    output = {
        "generated_at": captured_at,
        "season": SEASON,
        "leagues": leagues,
    }
    filename = ("leagues_{}.json".format(SEASON) if IS_BACKTEST
                else "leagues.json")
    path = DATA / filename
    path.write_text(json.dumps(output, indent=2))

    print("\n" + "=" * 68)
    print("wrote {} ({:,} bytes, {} league(s))".format(
        path, path.stat().st_size, len(leagues)))

    total = sum(1 for _ in SNAPSHOTS.rglob("*.json")) if SNAPSHOTS.exists() else 0
    print("snapshot history: {} file(s) on disk".format(total))

    if failures:
        # A partial result is still worth publishing -- a league that
        # doesn't exist in a backtest season is expected, not an outage.
        # Only a total failure is fatal, and that already exited above.
        print()
        print("!" * 68)
        print("WARNING: {} league(s) failed: {}".format(
            len(failures), ", ".join(failures)))
        if IS_BACKTEST:
            print("(backtest season -- a league that did not exist that year")
            print(" will always fail here, which is fine)")
        else:
            print("On a live season this needs investigating -- check the")
            print("error above for an auth failure or a changed endpoint.")
        print("!" * 68)


if __name__ == "__main__":
    main()
