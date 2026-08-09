#!/usr/bin/env python3
"""
The engine: optimal lineup solver and marginal value.

Phase 1d of the waiver pickup simulator. This is the part that makes the
tool disagree with public rankings, correctly.

A free agent's worth is not what he scores. It is how much he changes
*your* projected starting lineup once somebody comes off the roster to
make room. An 11-point running back is worth nothing to a team already
starting two better ones, and a great deal to a team whose RB2 is out.

Everything here is pure computation -- no APIs, no credentials. It reads
what the pipeline already produced:

  data/leagues.json (or leagues_<season>.json)  rosters, slots, scoring
  data/crosswalk.json                           espn id -> sleeper id
  data/players.json                             projections per week

Writes data/recommendations.json and data/recommendations_report.json.
"""

import json
import os
import pathlib

DATA = pathlib.Path("data")

SEASON = os.environ.get("ENGINE_SEASON", "2026")
LIVE_SEASON = os.environ.get("ENGINE_LIVE_SEASON", "2026")
IS_BACKTEST = SEASON != LIVE_SEASON

# How many add/drop pairings to report per league.
TOP_N = int(os.environ.get("ENGINE_TOP_N", "15"))

# Which weeks to evaluate over. Blank means "every week the projections
# file happens to carry".
WEEKS = [w.strip() for w in os.environ.get("ENGINE_WEEKS", "").split(",")
         if w.strip()]

# Positions that can fill the FLEX slot in both ESPN leagues.
FLEX_ELIGIBLE = {"RB", "WR", "TE"}

# Slot name -> the positions allowed in it.
SLOT_POSITIONS = {
    "QB": {"QB"},
    "RB": {"RB", "FB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "FLEX": FLEX_ELIGIBLE | {"FB"},
    "K": {"K"},
    "DST": {"DEF"},
}

NON_STARTING = {"BENCH", "IR"}


# ---------------------------------------------------------------------------
# Lineup solver
# ---------------------------------------------------------------------------


def starting_slots(lineup_config):
    """
    Expand the slot counts into a list of individual slots to fill,
    dedicated positions first and FLEX last.

    Order matters: filling FLEX before the dedicated slots would let a
    running back take the flex spot while the RB2 slot goes to someone
    worse. Dedicated first, flex from the leftovers, is optimal for a
    single flex.
    """
    dedicated, flex = [], []
    for name, count in (lineup_config.get("slots") or {}).items():
        if name in NON_STARTING or name not in SLOT_POSITIONS:
            continue
        target = flex if name == "FLEX" else dedicated
        target.extend([name] * count)
    return dedicated + flex


def optimal_lineup(roster, points, lineup_config):
    """
    Highest-scoring legal lineup from `roster`.

    roster: list of dicts with at least position and an id
    points: {player_id: projected points}
    Returns (total, [(slot, player)]).
    """
    available = sorted(
        roster,
        key=lambda p: -(points.get(p["id"]) or 0.0))
    used = set()
    filled = []
    total = 0.0

    for slot in starting_slots(lineup_config):
        allowed = SLOT_POSITIONS[slot]
        for player in available:
            if player["id"] in used:
                continue
            if player.get("position") not in allowed:
                continue
            used.add(player["id"])
            filled.append((slot, player))
            total += points.get(player["id"]) or 0.0
            break

    return round(total, 2), filled


def lineup_over_weeks(roster, points_by_week, lineup_config):
    """Sum of the optimal lineup across every evaluated week."""
    return round(sum(
        optimal_lineup(roster, points, lineup_config)[0]
        for points in points_by_week.values()), 2)


# ---------------------------------------------------------------------------
# Marginal value
# ---------------------------------------------------------------------------


def marginal_value(roster, add, drop, points_by_week, lineup_config):
    """
    Points the (add, drop) pair adds to the starting lineup, summed over
    the evaluated weeks. Negative means the swap makes you worse.
    """
    base = lineup_over_weeks(roster, points_by_week, lineup_config)
    swapped = [p for p in roster if p["id"] != drop["id"]] + [add]
    after = lineup_over_weeks(swapped, points_by_week, lineup_config)
    return round(after - base, 2)


def best_pairing(roster, add, droppable, points_by_week, lineup_config):
    """The drop that maximises the value of adding this player."""
    best = None
    for drop in droppable:
        value = marginal_value(roster, add, drop, points_by_week, lineup_config)
        if best is None or value > best[1]:
            best = (drop, value)
    return best


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def display_name(pid, record):
    """Sleeper gives team defenses no full_name -- use the team code."""
    name = record.get("name")
    if name:
        return name
    if record.get("position") == "DEF":
        return "{} D/ST".format(record.get("team") or pid)
    return str(pid)


def projection_weeks_for(pid, by_week):
    return sum(1 for points in by_week.values() if pid in points)


def load(name):
    path = DATA / name
    if not path.exists():
        raise SystemExit("missing {} -- run the earlier workflows first".format(path))
    return json.loads(path.read_text())


def points_lookup(players, scoring_flavour, weeks):
    """
    {week: {sleeper_id: projected points}} using the field that matches
    the league's PPR flavour.
    """
    field = "pts_half_ppr" if scoring_flavour == "half_ppr" else "pts_ppr"
    by_week = {}
    for pid, record in players.items():
        for week, line in (record.get("projections") or {}).items():
            if weeks and week not in weeks:
                continue
            value = line.get(field)
            if value is None:
                continue
            by_week.setdefault(week, {})[pid] = value
    return by_week, field


def roster_players(team, crosswalk, players):
    """Turn an ESPN roster into engine players keyed on sleeper id."""
    out, unresolved = [], []
    for entry in team.get("roster") or []:
        espn_id = str(entry.get("player_id"))
        sleeper_id = (crosswalk.get(espn_id) or {}).get("sleeper_id")
        if not sleeper_id:
            unresolved.append(entry.get("name") or espn_id)
            continue
        record = players.get(sleeper_id) or {}
        out.append({
            "id": sleeper_id,
            "espn_id": espn_id,
            "name": entry.get("name") or record.get("name"),
            "position": record.get("position") or entry.get("position"),
            "slot": entry.get("slot"),
            "injury_status": record.get("injury_status"),
        })
    return out, unresolved


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def evaluate_league(league, crosswalk, players):
    name = league.get("name")
    lineup_config = league.get("lineup") or {}
    flavour = (league.get("scoring") or {}).get("flavour")

    by_week, field = points_lookup(players, flavour, WEEKS)
    weeks = sorted(by_week)
    print("\n{}".format("-" * 68))
    print("{}  ({}, using {})".format(name, flavour, field))
    print("  weeks evaluated: {}".format(", ".join(weeks) or "none"))

    if not weeks:
        print("  no projection weeks available -- nothing to evaluate")
        return None

    teams = league.get("teams") or []
    rostered = set()
    team_rosters = {}
    unresolved_all = []
    for team in teams:
        roster, unresolved = roster_players(team, crosswalk, players)
        team_rosters[team["team_id"]] = roster
        rostered.update(p["id"] for p in roster)
        unresolved_all.extend(unresolved)
        if unresolved:
            print("  team {}: {} unresolved -- {}".format(
                team["team_id"], len(unresolved), ", ".join(
                    str(u) for u in unresolved[:5])))
    print("  rostered players resolved: {}, unresolved: {}".format(
        len(rostered), len(unresolved_all)))

    sizes = [len(r) for r in team_rosters.values()]
    print("  rosters: {} teams, {}-{} players each".format(
        len(teams), min(sizes) if sizes else 0, max(sizes) if sizes else 0))

    if not any(sizes):
        print("  all rosters empty -- league has not drafted yet")
        return None

    # Free agents: projected, at a fantasy position, nobody's.
    pool = []
    for pid, record in players.items():
        if pid in rostered:
            continue
        if record.get("position") not in (FLEX_ELIGIBLE | {"QB", "K", "DEF"}):
            continue
        if not any(pid in points for points in by_week.values()):
            continue
        pool.append({
            "id": pid,
            "espn_id": record.get("espn_id"),
            "name": display_name(pid, record),
            "position": record.get("position"),
            "injury_status": record.get("injury_status"),
            "snap_share": (record.get("trend") or {}).get("snap_share"),
            "target_share": (record.get("trend") or {}).get("target_share"),
        })
    print("  free agent pool: {:,} projected players".format(len(pool)))

    results = {}
    for team in teams:
        roster = team_rosters[team["team_id"]]
        if not roster:
            continue

        base = lineup_over_weeks(roster, by_week, lineup_config)

        # Only bench players are droppable -- never break up the starters
        # to make room. The solver decides who starts, so "bench" here
        # means whoever the optimal lineup leaves out.
        _, starters = optimal_lineup(roster, by_week[weeks[0]], lineup_config)
        starting_ids = {p["id"] for _, p in starters}
        droppable = [p for p in roster if p["id"] not in starting_ids] or roster

        # A player with no projection scores zero in the solver, which makes
        # him look like a free drop when in truth we simply don't know. Note
        # the coverage so a recommendation resting on absent data is visible
        # rather than silently confident.
        for player in roster:
            player["projected_weeks"] = projection_weeks_for(
                player["id"], by_week)
        blind_drops = sum(1 for p in droppable if not p["projected_weeks"])

        scored = []
        for candidate in pool:
            pairing = best_pairing(
                roster, candidate, droppable, by_week, lineup_config)
            if not pairing or pairing[1] <= 0:
                continue
            drop, value = pairing
            scored.append({
                "add": candidate["name"],
                "add_id": candidate["id"],
                "position": candidate["position"],
                "drop": drop["name"],
                "drop_id": drop["id"],
                "drop_projected_weeks": drop.get("projected_weeks"),
                "net_points": value,
                "snap_share": candidate["snap_share"],
                "target_share": candidate["target_share"],
                "injury_status": candidate["injury_status"],
            })

        scored.sort(key=lambda r: -r["net_points"])
        results[str(team["team_id"])] = {
            "team_name": team.get("name"),
            "baseline_points": base,
            "roster_size": len(roster),
            "droppable": len(droppable),
            "droppable_without_projections": blind_drops,
            "recommendations": scored[:TOP_N],
        }

    return {
        "league": name,
        "key": league.get("key"),
        "scoring_field": field,
        "weeks": weeks,
        "pool_size": len(pool),
        "rostered_resolved": len(rostered),
        "rostered_unresolved": len(unresolved_all),
        "unresolved_sample": [str(u) for u in unresolved_all[:10]],
        "teams": results,
    }


def main():
    print("season {}{}\n".format(SEASON, "   [BACKTEST]" if IS_BACKTEST else ""))

    leagues_file = ("leagues_{}.json".format(SEASON) if IS_BACKTEST
                    else "leagues.json")
    leagues = load(leagues_file)
    crosswalk = load("crosswalk.json").get("map") or {}
    players = load("players.json").get("players") or {}

    print("  {}: {} league(s)".format(leagues_file, len(leagues.get("leagues", []))))
    print("  crosswalk: {:,} entries".format(len(crosswalk)))
    print("  players:   {:,} records".format(len(players)))

    output = []
    for league in leagues.get("leagues") or []:
        result = evaluate_league(league, crosswalk, players)
        if result:
            output.append(result)

    DATA.mkdir(exist_ok=True)
    suffix = "_{}".format(SEASON) if IS_BACKTEST else ""
    path = DATA / "recommendations{}.json".format(suffix)
    path.write_text(json.dumps({
        "season": SEASON,
        "backtest": IS_BACKTEST,
        "leagues": output,
    }, indent=2))

    # Compact report: the first team's top pairings, which is the thing
    # worth eyeballing to see whether the engine is sane.
    report = {"season": SEASON, "backtest": IS_BACKTEST, "leagues": []}
    for result in output:
        first = next(iter(result["teams"].values()), None)
        report["leagues"].append({
            "league": result["league"],
            "scoring_field": result["scoring_field"],
            "weeks": result["weeks"],
            "pool_size": result["pool_size"],
            "teams_evaluated": len(result["teams"]),
            "rostered_resolved": result["rostered_resolved"],
            "rostered_unresolved": result["rostered_unresolved"],
            "unresolved_sample": result["unresolved_sample"],
            "sample_droppable_without_projections":
                first and first.get("droppable_without_projections"),
            "sample_team": first and first["team_name"],
            "sample_baseline_points": first and first["baseline_points"],
            "sample_top_pairings": (first or {}).get("recommendations", [])[:8],
        })
    report_path = DATA / "recommendations_report{}.json".format(suffix)
    report_path.write_text(json.dumps(report, indent=2))

    print("\n" + "=" * 68)
    print("wrote {} ({:,} bytes)".format(path, path.stat().st_size))
    print("wrote {} ({:,} bytes) -- this is the one to read".format(
        report_path, report_path.stat().st_size))


if __name__ == "__main__":
    main()
