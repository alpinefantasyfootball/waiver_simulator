#!/usr/bin/env python3
"""
Probe Sleeper's endpoints before building the player pipeline.

Phase 1c of the waiver pickup simulator. ESPN supplies league state;
Sleeper supplies everything about why a player might be worth claiming --
depth chart position, injury detail, weekly stats and projections.

Two things could sink this and both are worth knowing before any pipeline
code gets written:

  1. The crosswalk. ESPN identifies players by its own integer id. If
     Sleeper's espn_id field is sparse, or missing for exactly the fringe
     players who show up on waivers, the join fails where it matters most.

  2. Projections. Weekly projections are assumed to live at a v1 endpoint
     that is not documented anywhere official. If they don't, the
     valuation engine needs another source.

Prints coverage numbers and endpoint shapes. Writes nothing except
optional samples. Standard library only.
"""

import json
import os
import pathlib
import urllib.error
import urllib.request

BASE = "https://api.sleeper.app/v1"
OUT = pathlib.Path("out")

SEASON = os.environ.get("SLEEPER_SEASON", "2025")
WEEK = os.environ.get("SLEEPER_WEEK", "8")

HEADERS = {"User-Agent": "alpine-waiver-sim/1.0", "Accept": "application/json"}

# Positions the tool actually cares about.
FANTASY_POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]

# ESPN player ids observed in the live 2025 pool pull. If the crosswalk
# works, every one of these resolves to a Sleeper player.
KNOWN_ESPN_IDS = {
    "16800": "Davante Adams",
    "4431611": "Caleb Williams",
    "4361741": "Brock Purdy",
    "3116365": "Mark Andrews",
    "4432665": "Brock Bowers",
    "3054850": "Alvin Kamara",
    "4429205": "Jordan Addison",
}


def line(char="-", width=68):
    print(char * width)


def banner(text):
    line("=")
    print(text)
    line("=")


def get(path):
    url = "{}/{}".format(BASE, path.lstrip("/"))
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
            return json.loads(raw), len(raw), None
    except urllib.error.HTTPError as err:
        return None, 0, "HTTP {} -- {}".format(err.code, err.reason)
    except urllib.error.URLError as err:
        return None, 0, "connection failed -- {}".format(err.reason)
    except json.JSONDecodeError as err:
        return None, 0, "not JSON -- {}".format(err)


def pct(part, whole):
    return 0.0 if not whole else round(100.0 * part / whole, 1)


# ---------------------------------------------------------------------------


def probe_state():
    banner("STATE")
    data, size, error = get("state/nfl")
    if error:
        print("  FAILED: {}".format(error))
        return
    print("  {:,} bytes".format(size))
    for key in sorted(data.keys()):
        print("    {}: {}".format(key, data[key]))
    print()


def probe_players():
    banner("PLAYERS  (the big one -- several MB)")
    data, size, error = get("players/nfl")
    if error:
        print("  FAILED: {}".format(error))
        return None
    print("  {:,} bytes, {:,} players".format(size, len(data)))

    sample_key = next(iter(data))
    print("\n  fields on a player record:")
    print("    " + ", ".join(sorted(data[sample_key].keys())))

    # Narrow to players who could plausibly be claimed.
    relevant = {
        pid: p for pid, p in data.items()
        if p.get("position") in FANTASY_POSITIONS
        and p.get("active") is not False
    }
    print("\n  active players at fantasy positions: {:,}".format(len(relevant)))

    print("\n  CROSSWALK COVERAGE -- players carrying an espn_id")
    print("  {:<6} {:>7} {:>9} {:>8}".format("pos", "total", "with id", "pct"))
    overall_total = overall_hit = 0
    for position in FANTASY_POSITIONS:
        group = [p for p in relevant.values() if p.get("position") == position]
        hit = sum(1 for p in group if p.get("espn_id"))
        overall_total += len(group)
        overall_hit += hit
        print("  {:<6} {:>7} {:>9} {:>7}%".format(
            position, len(group), hit, pct(hit, len(group))))
    print("  {:<6} {:>7} {:>9} {:>7}%".format(
        "ALL", overall_total, overall_hit, pct(overall_hit, overall_total)))

    # Coverage among players who are actually rosterable matters more than
    # coverage across every practice-squad name in the database.
    starters = [p for p in relevant.values()
                if p.get("depth_chart_order") in (1, 2)]
    starter_hit = sum(1 for p in starters if p.get("espn_id"))
    print("\n  depth chart 1-2 only: {} of {} have an espn_id ({}%)".format(
        starter_hit, len(starters), pct(starter_hit, len(starters))))

    print("\n  SPOT CHECK -- ESPN ids seen in the live pool pull")
    by_espn = {}
    for pid, player in data.items():
        espn_id = player.get("espn_id")
        if espn_id:
            by_espn[str(espn_id)] = player
    for espn_id, expected in sorted(KNOWN_ESPN_IDS.items()):
        found = by_espn.get(espn_id)
        if found:
            print("    {:>8} -> {:<22} {:<4} {:<4} depth={}".format(
                espn_id, found.get("full_name", "?"),
                found.get("position", "?"), found.get("team", "-"),
                found.get("depth_chart_order")))
        else:
            print("    {:>8} -> NOT FOUND  (expected {})".format(
                espn_id, expected))

    print("\n  OPPORTUNITY FIELDS -- how many carry each signal")
    for field in ("depth_chart_order", "depth_chart_position", "injury_status",
                  "injury_body_part", "practice_participation", "years_exp",
                  "team", "number", "search_rank"):
        present = sum(1 for p in relevant.values() if p.get(field) is not None)
        print("    {:<24} {:>6} of {:,}  ({}%)".format(
            field, present, len(relevant), pct(present, len(relevant))))

    injured = [p for p in relevant.values() if p.get("injury_status")]
    print("\n  players with a non-empty injury_status: {}".format(len(injured)))
    statuses = {}
    for player in injured:
        status = player.get("injury_status")
        statuses[status] = statuses.get(status, 0) + 1
    print("    " + ", ".join("{}={}".format(k, v)
                             for k, v in sorted(statuses.items(), key=str)))

    print()
    return data


def probe_weekly(kind):
    """kind is 'stats' or 'projections' -- same URL shape, different data."""
    banner("{}  season {} week {}".format(kind.upper(), SEASON, WEEK))
    path = "{}/nfl/regular/{}/{}".format(kind, SEASON, WEEK)
    data, size, error = get(path)
    if error:
        print("  FAILED: {}".format(error))
        print("  path: {}/{}".format(BASE, path))
        if kind == "projections":
            print("  (if this 404s, weekly projections need another source --")
            print("   the valuation engine depends on them)")
        return

    print("  {:,} bytes, {:,} player entries".format(size, len(data)))
    if not data:
        print("  empty response")
        return

    # Find an entry with a decent number of stats to show the shape.
    richest = max(data.items(), key=lambda kv: len(kv[1] or {}))
    print("\n  richest entry: sleeper id {} with {} keys".format(
        richest[0], len(richest[1])))
    print("  keys: " + ", ".join(sorted(richest[1].keys())[:40]))

    interesting = ["pts_ppr", "pts_half_ppr", "pts_std", "rec", "rec_tgt",
                   "rush_att", "off_snp", "tm_off_snp", "gp"]
    print("\n  presence of the fields the model wants:")
    for field in interesting:
        count = sum(1 for v in data.values() if v and field in v)
        print("    {:<16} {:>6} of {:,}".format(field, count, len(data)))
    print()


def main():
    print("season {}  week {}\n".format(SEASON, WEEK))
    probe_state()
    players = probe_players()
    probe_weekly("stats")
    probe_weekly("projections")

    if players and os.environ.get("SLEEPER_SAVE") == "1":
        OUT.mkdir(exist_ok=True)
        path = OUT / "sleeper_players.json"
        path.write_text(json.dumps(players))
        print("saved {} ({:,} bytes)".format(path, path.stat().st_size))

    print("Done.")


if __name__ == "__main__":
    main()
