#!/usr/bin/env python3
"""
Build and measure the ESPN -> Sleeper player crosswalk.

Phase 1c of the waiver pickup simulator. ESPN identifies players by its
own integer id; the stats and opportunity pipeline is keyed on Sleeper
ids. Everything downstream depends on joining the two.

The obvious join -- Sleeper's espn_id field -- covers only about 46% of
active fantasy players, and the misses are systematic rather than random:
players with ESPN ids above roughly 4,000,000, meaning anyone who entered
the league from about 2022 onward. Those are exactly the young ascending
players who appear on waivers, so the id join alone is unusable.

This script therefore matches in tiers and reports what each tier
contributes, so the coverage number is measured rather than hoped for:

  1  espn_id            exact, when present
  2  name + position    normalized full name, must be unique
  3  name + pos + team  disambiguates duplicate names
  4  last name + pos + team   catches nickname/formatting drift

Writes data/crosswalk.json and prints a coverage report plus every
unmatched player, because the residual list is the thing worth reading.

Standard library only.
"""

import json
import os
import pathlib
import re
import unicodedata
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ESPN_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
SLEEPER_BASE = "https://api.sleeper.app/v1"

LEAGUE_ID = os.environ.get("ESPN_LEAGUE_ID", "65142363")
SEASON = int(os.environ.get("ESPN_SEASON", "2026"))
ESPN_LIMIT = int(os.environ.get("ESPN_PLAYER_LIMIT", "1500"))

DATA = pathlib.Path("data")

ESPN_S2 = os.environ.get("ESPN_S2", "")
SWID = os.environ.get("SWID", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; alpine-waiver-sim/1.0)",
    "Accept": "application/json",
}

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}

ESPN_POSITIONS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}

# ESPN proTeamId -> abbreviation. Verified names differ from Sleeper's in
# a couple of places, normalized below.
ESPN_TEAMS = {
    0: None, 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
    7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
    14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG",
    20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF",
    26: "SEA", 27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL",
    34: "HOU",
}

TEAM_ALIASES = {"WSH": "WAS", "JAC": "JAX", "LA": "LAR", "OAK": "LV",
                "SD": "LAC", "STL": "LAR"}

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_name(raw):
    """
    Fold a display name to a comparable key.

    Handles the four things that actually differ between the two sources:
    accents, punctuation (D'Andre, Smith-Njigba), generational suffixes,
    and stray whitespace.
    """
    if not raw:
        return ""
    text = unicodedata.normalize("NFKD", str(raw))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[.'`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    parts = [p for p in text.split() if p not in SUFFIXES]
    return " ".join(parts)


def normalize_team(raw):
    if not raw:
        return None
    team = str(raw).upper()
    return TEAM_ALIASES.get(team, team)


def last_name_key(raw):
    parts = normalize_name(raw).split()
    return parts[-1] if parts else ""


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def get(url, headers=None, fantasy_filter=None):
    request_headers = dict(HEADERS)
    if headers:
        request_headers.update(headers)
    if fantasy_filter is not None:
        request_headers["X-Fantasy-Filter"] = json.dumps(fantasy_filter)

    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response), None
    except urllib.error.HTTPError as err:
        return None, "HTTP {} -- {}".format(err.code, err.reason)
    except urllib.error.URLError as err:
        return None, "connection failed -- {}".format(err.reason)


def fetch_espn_players():
    url = "{base}/seasons/{season}/segments/0/leagues/{league}?view=kona_player_info".format(
        base=ESPN_BASE, season=SEASON, league=LEAGUE_ID)
    headers = {}
    if ESPN_S2 and SWID:
        headers["Cookie"] = "espn_s2={}; SWID={}".format(ESPN_S2, SWID)

    fantasy_filter = {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
            "limit": ESPN_LIMIT,
            "offset": 0,
            "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
        }
    }
    data, error = get(url, headers, fantasy_filter)
    if error:
        return None, error
    return data.get("players") or [], None


def fetch_sleeper_players():
    data, error = get("{}/players/nfl".format(SLEEPER_BASE))
    if error:
        return None, error
    return data, None


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------


def build_indexes(sleeper):
    by_espn_id = {}
    by_def_team = {}
    by_name_pos = {}
    by_name_pos_team = {}
    by_last_pos_team = {}

    considered = 0
    for player_id, player in sleeper.items():
        position = player.get("position")
        if position not in FANTASY_POSITIONS:
            continue
        if player.get("active") is False:
            continue
        considered += 1

        record = {
            "sleeper_id": player_id,
            "name": player.get("full_name"),
            "position": position,
            "team": normalize_team(player.get("team")),
            "depth": player.get("depth_chart_order"),
        }

        espn_id = player.get("espn_id")
        if espn_id:
            by_espn_id[str(espn_id)] = record

        # Sleeper keys defenses by team code and gives them no
        # conventional full name, so they can never match on name.
        # They join on the team abbreviation instead.
        if position == "DEF":
            code = record["team"] or normalize_team(player_id)
            if code:
                by_def_team.setdefault(code, []).append(record)
            continue

        name = normalize_name(player.get("full_name"))
        if not name:
            continue

        by_name_pos.setdefault((name, position), []).append(record)
        if record["team"]:
            by_name_pos_team.setdefault(
                (name, position, record["team"]), []).append(record)
            by_last_pos_team.setdefault(
                (last_name_key(player.get("full_name")), position,
                 record["team"]), []).append(record)

    return {
        "considered": considered,
        "by_espn_id": by_espn_id,
        "by_def_team": by_def_team,
        "by_name_pos": by_name_pos,
        "by_name_pos_team": by_name_pos_team,
        "by_last_pos_team": by_last_pos_team,
    }


def only(bucket):
    """Return the single record in a bucket, or None if absent/ambiguous."""
    if bucket and len(bucket) == 1:
        return bucket[0]
    return None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def match_one(espn_player, index):
    espn_id = str(espn_player["espn_id"])
    name = normalize_name(espn_player["name"])
    position = espn_player["position"]
    team = espn_player["team"]

    if position == "DEF":
        found = only(index["by_def_team"].get(team))
        if found:
            return found, "def_team"

    found = index["by_espn_id"].get(espn_id)
    if found:
        return found, "espn_id"

    found = only(index["by_name_pos"].get((name, position)))
    if found:
        return found, "name_pos"

    if team:
        found = only(index["by_name_pos_team"].get((name, position, team)))
        if found:
            return found, "name_pos_team"

        found = only(index["by_last_pos_team"].get(
            (last_name_key(espn_player["name"]), position, team)))
        if found:
            return found, "last_pos_team"

    return None, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("league {}  season {}  espn player limit {}\n".format(
        LEAGUE_ID, SEASON, ESPN_LIMIT))

    espn_raw, error = fetch_espn_players()
    if error:
        print("ESPN fetch FAILED: {}".format(error))
        raise SystemExit(1)
    print("ESPN:    {:,} player entries".format(len(espn_raw)))

    sleeper_raw, error = fetch_sleeper_players()
    if error:
        print("Sleeper fetch FAILED: {}".format(error))
        raise SystemExit(1)
    print("Sleeper: {:,} player records".format(len(sleeper_raw)))

    espn_players = []
    for entry in espn_raw:
        player = entry.get("player") or {}
        position = ESPN_POSITIONS.get(player.get("defaultPositionId"))
        if not position:
            continue
        ownership = player.get("ownership") or {}
        espn_players.append({
            "espn_id": player.get("id"),
            "name": player.get("fullName"),
            "position": position,
            "team": normalize_team(ESPN_TEAMS.get(player.get("proTeamId"))),
            "percent_owned": round(ownership.get("percentOwned", 0) or 0, 1),
        })
    print("ESPN at fantasy positions: {:,}\n".format(len(espn_players)))

    index = build_indexes(sleeper_raw)
    print("Sleeper indexed (active, fantasy positions): {:,}".format(
        index["considered"]))
    print("  with an espn_id: {:,}\n".format(len(index["by_espn_id"])))

    crosswalk, unmatched = {}, []
    tiers = {"def_team": 0, "espn_id": 0, "name_pos": 0,
             "name_pos_team": 0, "last_pos_team": 0}

    for player in espn_players:
        found, tier = match_one(player, index)
        if found:
            tiers[tier] += 1
            crosswalk[str(player["espn_id"])] = {
                "sleeper_id": found["sleeper_id"],
                "name": player["name"],
                "position": player["position"],
                "matched_by": tier,
            }
        else:
            unmatched.append(player)

    total = len(espn_players)
    matched = len(crosswalk)

    print("=" * 68)
    print("COVERAGE")
    print("=" * 68)
    for tier in ("def_team", "espn_id", "name_pos", "name_pos_team",
                 "last_pos_team"):
        count = tiers[tier]
        share = 0 if not total else round(100.0 * count / total, 1)
        print("  {:<16} {:>6}   {:>5}%".format(tier, count, share))
    print("  {:<16} {:>6}   {:>5}%".format(
        "MATCHED", matched, 0 if not total else round(100.0 * matched / total, 1)))
    print("  {:<16} {:>6}   {:>5}%".format(
        "unmatched", len(unmatched),
        0 if not total else round(100.0 * len(unmatched) / total, 1)))

    # The number that actually matters: coverage among players anyone
    # would consider claiming, not across every third-string name ESPN
    # happens to list.
    relevant = [p for p in espn_players if p["percent_owned"] >= 1.0]
    relevant_unmatched = [p for p in unmatched if p["percent_owned"] >= 1.0]
    print("\n  among players owned in >=1% of ESPN leagues:")
    print("    {:,} players, {} unmatched ({}%)".format(
        len(relevant), len(relevant_unmatched),
        0 if not relevant else round(
            100.0 * len(relevant_unmatched) / len(relevant), 1)))

    if unmatched:
        print("\n" + "=" * 68)
        print("UNMATCHED  (top 40 by ownership -- the ones worth fixing)")
        print("=" * 68)
        unmatched.sort(key=lambda p: -p["percent_owned"])
        for player in unmatched[:40]:
            print("  {:>8}  {:<26} {:<4} {:<4} owned {:>5}%".format(
                player["espn_id"], str(player["name"])[:26],
                player["position"], player["team"] or "-",
                player["percent_owned"]))

    DATA.mkdir(exist_ok=True)
    path = DATA / "crosswalk.json"
    path.write_text(json.dumps({
        "season": SEASON,
        "espn_players": total,
        "matched": matched,
        "tiers": tiers,
        "map": crosswalk,
    }, indent=2))
    print("\nwrote {} ({:,} bytes)".format(path, path.stat().st_size))

    # Small companion file: everything worth reading, none of the bulk.
    report_path = DATA / "crosswalk_report.json"
    report_path.write_text(json.dumps({
        "season": SEASON,
        "espn_players": total,
        "matched": matched,
        "unmatched_count": len(unmatched),
        "match_rate_pct": 0 if not total else round(100.0 * matched / total, 1),
        "tiers": tiers,
        "unmatched": [
            {
                "espn_id": p["espn_id"],
                "name": p["name"],
                "position": p["position"],
                "team": p["team"],
                "percent_owned": p["percent_owned"],
            }
            for p in sorted(unmatched, key=lambda p: -p["percent_owned"])
        ],
    }, indent=2))
    print("wrote {} ({:,} bytes) -- this is the one to read".format(
        report_path, report_path.stat().st_size))


if __name__ == "__main__":
    main()
