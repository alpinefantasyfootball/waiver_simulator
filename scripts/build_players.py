#!/usr/bin/env python3
"""
Build the player intelligence file: who's healthy, who's getting the ball,
and who's projected to do what.

Phase 1c of the waiver pickup simulator. ESPN says who owns whom; this
says why a player might be worth claiming.

Scoring note: ESPN's scoring map uses 53 statIds whose meanings are not
documented. Rather than guess at that mapping, this uses Sleeper's own
pts_ppr and pts_half_ppr, which correspond exactly to the two leagues
(D-Town is full PPR, ALPINE is half). Validate against ESPN's applied
totals for a completed week before trusting it further.

Writes:
  data/players.json          full record per player
  data/players_report.json   coverage summary and top movers

Standard library only.
"""

import json
import os
import pathlib
import urllib.error
import urllib.request

SLEEPER = "https://api.sleeper.app/v1"
DATA = pathlib.Path("data")

SEASON = os.environ.get("SLEEPER_SEASON", "2025")
# Which weeks of actual results to pull. Trend metrics use the last three
# weeks that actually have data.
FIRST_WEEK = int(os.environ.get("SLEEPER_FIRST_WEEK", "1"))
LAST_WEEK = int(os.environ.get("SLEEPER_LAST_WEEK", "18"))
# Weeks to pull projections for. Empty means skip.
PROJECTION_WEEKS = [
    int(w) for w in os.environ.get("SLEEPER_PROJECTION_WEEKS", "").split(",")
    if w.strip().isdigit()
]

HEADERS = {"User-Agent": "alpine-waiver-sim/1.0", "Accept": "application/json"}

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF", "FB"}

TREND_WEEKS = 3


def get(path):
    url = "{}/{}".format(SLEEPER, path.lstrip("/"))
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response), None
    except urllib.error.HTTPError as err:
        return None, "HTTP {} -- {}".format(err.code, err.reason)
    except urllib.error.URLError as err:
        return None, "connection failed -- {}".format(err.reason)


def safe_div(numerator, denominator):
    if not denominator:
        return None
    return round(numerator / denominator, 4)


# ---------------------------------------------------------------------------
# Load inputs
# ---------------------------------------------------------------------------


def load_crosswalk():
    path = DATA / "crosswalk.json"
    if not path.exists():
        print("  no crosswalk.json -- run the crosswalk workflow first")
        return {}
    payload = json.loads(path.read_text())
    # sleeper_id -> espn_id, the direction this pipeline needs
    reverse = {}
    for espn_id, entry in (payload.get("map") or {}).items():
        reverse[entry["sleeper_id"]] = espn_id
    print("  crosswalk: {:,} sleeper ids carry an espn id".format(len(reverse)))
    return reverse


def load_players():
    data, error = get("players/nfl")
    if error:
        raise SystemExit("players/nfl failed: {}".format(error))
    keep = {
        pid: p for pid, p in data.items()
        if p.get("position") in FANTASY_POSITIONS and p.get("active") is not False
    }
    print("  players: {:,} total, {:,} at fantasy positions".format(
        len(data), len(keep)))
    return keep


def load_week(kind, week):
    data, error = get("{}/nfl/regular/{}/{}".format(kind, SEASON, week))
    if error:
        return None, error
    return data or {}, None


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def team_target_totals(week_stats, players):
    """
    Sleeper reports targets per player but not per team, so total them
    here. Target share is only meaningful against a team denominator.
    """
    totals = {}
    for pid, line in week_stats.items():
        if not line:
            continue
        player = players.get(pid)
        if not player:
            continue
        team = player.get("team")
        if not team:
            continue
        totals[team] = totals.get(team, 0) + (line.get("rec_tgt") or 0)
    return totals


def build_weekly(players, crosswalk):
    """Pull each week once, attach per-player lines, and derive shares."""
    weekly = {pid: {} for pid in players}
    weeks_with_data = []

    for week in range(FIRST_WEEK, LAST_WEEK + 1):
        stats, error = load_week("stats", week)
        if error:
            print("  week {:>2}: FAILED {}".format(week, error))
            continue
        if not stats:
            print("  week {:>2}: empty".format(week))
            continue

        targets = team_target_totals(stats, players)
        counted = 0
        for pid, line in stats.items():
            if pid not in weekly or not line:
                continue
            player = players[pid]
            team = player.get("team")
            off_snp = line.get("off_snp")
            tm_off_snp = line.get("tm_off_snp")

            weekly[pid][week] = {
                "pts_ppr": line.get("pts_ppr"),
                "pts_half_ppr": line.get("pts_half_ppr"),
                "rec_tgt": line.get("rec_tgt"),
                "rec": line.get("rec"),
                "rush_att": line.get("rush_att"),
                "off_snp": off_snp,
                "tm_off_snp": tm_off_snp,
                "snap_share": safe_div(off_snp or 0, tm_off_snp),
                "target_share": safe_div(
                    line.get("rec_tgt") or 0, targets.get(team)),
            }
            counted += 1

        weeks_with_data.append(week)
        print("  week {:>2}: {:,} player lines".format(week, counted))

    return weekly, weeks_with_data


def trend(weekly_for_player, weeks, field):
    """Mean of `field` over the last TREND_WEEKS weeks that have a value."""
    values = []
    for week in reversed(weeks):
        line = weekly_for_player.get(week)
        if not line:
            continue
        value = line.get(field)
        if value is None:
            continue
        values.append(value)
        if len(values) >= TREND_WEEKS:
            break
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def load_projections():
    projections = {}
    for week in PROJECTION_WEEKS:
        data, error = load_week("projections", week)
        if error:
            print("  projections week {}: FAILED {}".format(week, error))
            continue
        for pid, line in (data or {}).items():
            if not line:
                continue
            projections.setdefault(pid, {})[week] = {
                "pts_ppr": line.get("pts_ppr"),
                "pts_half_ppr": line.get("pts_half_ppr"),
                "rec_tgt": line.get("rec_tgt"),
                "rush_att": line.get("rush_att"),
            }
        print("  projections week {:>2}: {:,} entries".format(
            week, len(data or {})))
    return projections


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("season {}  weeks {}-{}\n".format(SEASON, FIRST_WEEK, LAST_WEEK))

    crosswalk = load_crosswalk()
    players = load_players()
    print()

    weekly, weeks = build_weekly(players, crosswalk)
    print()

    projections = load_projections()
    print()

    records = {}
    for pid, player in players.items():
        player_weeks = weekly.get(pid) or {}
        games = len(player_weeks)

        # The signal that drives most good claims: a player sitting behind
        # someone on the depth chart is only interesting if that someone
        # is hurt. This records the ingredients; the engine joins them.
        record = {
            "sleeper_id": pid,
            "espn_id": crosswalk.get(pid),
            "name": player.get("full_name"),
            "position": player.get("position"),
            "team": player.get("team"),
            "depth_chart_order": player.get("depth_chart_order"),
            "depth_chart_position": player.get("depth_chart_position"),
            "injury_status": player.get("injury_status"),
            "injury_body_part": player.get("injury_body_part"),
            "years_exp": player.get("years_exp"),
            "games_with_data": games,
            "trend": {
                "snap_share": trend(player_weeks, weeks, "snap_share"),
                "target_share": trend(player_weeks, weeks, "target_share"),
                "rush_att": trend(player_weeks, weeks, "rush_att"),
                "pts_ppr": trend(player_weeks, weeks, "pts_ppr"),
                "pts_half_ppr": trend(player_weeks, weeks, "pts_half_ppr"),
            },
            "weekly": {str(w): line for w, line in sorted(player_weeks.items())},
        }
        if pid in projections:
            record["projections"] = {
                str(w): line for w, line in sorted(projections[pid].items())
            }
        records[pid] = record

    DATA.mkdir(exist_ok=True)
    path = DATA / "players.json"
    path.write_text(json.dumps({
        "season": SEASON,
        "weeks": weeks,
        "players": records,
    }, indent=2))

    # Coverage summary -- read this, not the big file.
    with_stats = [r for r in records.values() if r["games_with_data"]]
    with_snaps = [r for r in with_stats if r["trend"]["snap_share"] is not None]
    with_espn = [r for r in records.values() if r["espn_id"]]
    with_depth = [r for r in records.values()
                  if r["depth_chart_order"] is not None]

    movers = sorted(
        (r for r in with_snaps if r["position"] in ("RB", "WR", "TE")),
        key=lambda r: -(r["trend"]["snap_share"] or 0))[:15]

    report = {
        "season": SEASON,
        "weeks_pulled": weeks,
        "players_total": len(records),
        "players_with_stats": len(with_stats),
        "players_with_snap_share": len(with_snaps),
        "players_with_espn_id": len(with_espn),
        "players_with_depth_chart": len(with_depth),
        "projection_weeks": PROJECTION_WEEKS,
        "players_with_projections": sum(
            1 for r in records.values() if "projections" in r),
        "top_snap_share_last_3": [
            {
                "name": r["name"],
                "position": r["position"],
                "team": r["team"],
                "snap_share": r["trend"]["snap_share"],
                "target_share": r["trend"]["target_share"],
                "pts_ppr": r["trend"]["pts_ppr"],
            }
            for r in movers
        ],
    }
    report_path = DATA / "players_report.json"
    report_path.write_text(json.dumps(report, indent=2))

    print("=" * 68)
    print("wrote {} ({:,} bytes)".format(path, path.stat().st_size))
    print("wrote {} ({:,} bytes) -- this is the one to read".format(
        report_path, report_path.stat().st_size))
    print()
    print("  players with any stats:  {:,}".format(len(with_stats)))
    print("  with snap share trend:   {:,}".format(len(with_snaps)))
    print("  with an espn id:         {:,}".format(len(with_espn)))
    print("  with depth chart order:  {:,}".format(len(with_depth)))


if __name__ == "__main__":
    main()
