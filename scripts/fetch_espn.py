#!/usr/bin/env python3
"""
Pull raw ESPN fantasy football league data and print a short summary.

Phase 1a of the waiver pickup simulator: this exists to confirm what ESPN
actually returns before any of the engine gets written against it.

Saves the full untouched response to out/ (uploaded as a build artifact by
the workflow) and prints only the handful of fields the build depends on,
so the Actions log stays readable.

Uses nothing outside the Python standard library on purpose -- no pip
install step, no dependency drift.
"""

import json
import os
import pathlib
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LEAGUE_ID = os.environ.get("ESPN_LEAGUE_ID", "65142363")
SEASONS = [int(s) for s in os.environ.get("ESPN_SEASONS", "2025,2026").split(",")]

VIEWS = ["mSettings", "mTeam", "mRoster", "mTransactions2"]

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
OUT = pathlib.Path("out")

# ESPN sometimes refuses requests with no user agent.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; alpine-waiver-sim/0.1)",
    "Accept": "application/json",
}

# Private leagues need these. Left empty for the public friends league;
# the work league will supply them from repository secrets later.
ESPN_S2 = os.environ.get("ESPN_S2", "")
SWID = os.environ.get("SWID", "")


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def build_url(season):
    query = "&".join("view=" + v for v in VIEWS)
    return "{base}/seasons/{season}/segments/0/leagues/{league}?{query}".format(
        base=BASE, season=season, league=LEAGUE_ID, query=query
    )


def fetch(season):
    url = build_url(season)
    headers = dict(HEADERS)
    if ESPN_S2 and SWID:
        headers["Cookie"] = "espn_s2={}; SWID={}".format(ESPN_S2, SWID)

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response), None
    except urllib.error.HTTPError as err:
        return None, "HTTP {} -- {}".format(err.code, err.reason)
    except urllib.error.URLError as err:
        return None, "connection failed -- {}".format(err.reason)


# ---------------------------------------------------------------------------
# Summarize
# ---------------------------------------------------------------------------


def line(char="-", width=68):
    print(char * width)


def team_name(team):
    """Team naming moved around between seasons -- handle both shapes."""
    if team.get("name"):
        return team["name"].strip()
    parts = [team.get("location", ""), team.get("nickname", "")]
    joined = " ".join(p for p in parts if p).strip()
    return joined or "(unnamed)"


def summarize_settings(settings):
    print("\nSETTINGS keys present:")
    print("  " + ", ".join(sorted(settings.keys())))

    scoring = settings.get("scoringSettings", {})
    items = scoring.get("scoringItems", [])
    print("\nSCORING")
    print("  scoringItems: {} entries".format(len(items)))
    print("  first 12 (statId -> points):")
    for item in items[:12]:
        print(
            "    statId {:>4}  points {:<8} isReverseItem={}".format(
                item.get("statId"),
                item.get("points"),
                item.get("isReverseItem"),
            )
        )
    for key in ("playerRankType", "scoringType", "matchupTieRule"):
        if key in scoring:
            print("  {}: {}".format(key, scoring[key]))

    roster = settings.get("rosterSettings", {})
    slots = roster.get("lineupSlotCounts", {})
    print("\nROSTER")
    print("  lineupSlotCounts (non-zero only):")
    for slot_id, count in sorted(slots.items(), key=lambda kv: int(kv[0])):
        if count:
            print("    slot {:>3} x{}".format(slot_id, count))
    for key in ("rosterLocktimeType", "isBenchUnlimited", "positionLimits"):
        if key in roster:
            print("  {}: {}".format(key, roster[key]))

    acquisition = settings.get("acquisitionSettings", {})
    print("\nACQUISITION / WAIVERS")
    if acquisition:
        for key in sorted(acquisition.keys()):
            print("  {}: {}".format(key, acquisition[key]))
    else:
        print("  (acquisitionSettings absent)")


def summarize_teams(teams):
    print("\nTEAMS ({} total)".format(len(teams)))
    if teams:
        print("  keys on a team object:")
        print("    " + ", ".join(sorted(teams[0].keys())))
    print("\n  {:>4}  {:<26} {:>7}  {:>6}  {}".format(
        "id", "name", "waiver", "roster", "owners"))
    for team in teams:
        roster = team.get("roster") or {}
        entries = roster.get("entries") or []
        owners = team.get("owners") or []
        print("  {:>4}  {:<26} {:>7}  {:>6}  {}".format(
            team.get("id"),
            team_name(team)[:26],
            team.get("waiverRank", "-"),
            len(entries),
            len(owners),
        ))
    if teams and (teams[0].get("owners") or []):
        print("\n  sample owner id: {}".format(teams[0]["owners"][0]))


def summarize_transactions(data):
    print("\nTRANSACTIONS")
    transactions = data.get("transactions")
    if transactions is None:
        print("  no 'transactions' key on the response")
        print("  (mTransactions2 may need an X-Fantasy-Filter header)")
        return
    print("  {} entries returned".format(len(transactions)))
    if not transactions:
        return

    by_type = {}
    by_status = {}
    for item in transactions:
        by_type[item.get("type")] = by_type.get(item.get("type"), 0) + 1
        by_status[item.get("status")] = by_status.get(item.get("status"), 0) + 1
    print("  types:    " + ", ".join(
        "{}={}".format(k, v) for k, v in sorted(by_type.items(), key=str)))
    print("  statuses: " + ", ".join(
        "{}={}".format(k, v) for k, v in sorted(by_status.items(), key=str)))
    print("\n  keys on a transaction object:")
    print("    " + ", ".join(sorted(transactions[0].keys())))


def summarize(season, data):
    line("=")
    print("SEASON {}  league {}".format(season, LEAGUE_ID))
    line("=")
    print("\nTOP-LEVEL keys:")
    print("  " + ", ".join(sorted(data.keys())))
    print("\n  seasonId:   {}".format(data.get("seasonId")))
    print("  scoringPeriodId: {}".format(data.get("scoringPeriodId")))

    status = data.get("status") or {}
    for key in ("currentMatchupPeriod", "isActive", "latestScoringPeriod"):
        if key in status:
            print("  status.{}: {}".format(key, status[key]))

    settings = data.get("settings") or {}
    if settings:
        print("\n  league name: {}".format(settings.get("name")))
        print("  size:        {}".format(settings.get("size")))
        summarize_settings(settings)
    else:
        print("\n  (no settings object returned)")

    summarize_teams(data.get("teams") or [])
    summarize_transactions(data)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    OUT.mkdir(exist_ok=True)
    failures = []

    for season in SEASONS:
        data, error = fetch(season)
        if error:
            line("=")
            print("SEASON {}  league {}".format(season, LEAGUE_ID))
            line("=")
            print("  FAILED: {}".format(error))
            print("  url: {}".format(build_url(season)))
            print()
            failures.append(season)
            continue

        path = OUT / "espn_{}_{}.json".format(LEAGUE_ID, season)
        path.write_text(json.dumps(data, indent=2))
        summarize(season, data)
        print("  raw response saved to {} ({:,} bytes)".format(
            path, path.stat().st_size))
        print()

    if failures:
        print("Seasons that failed: {}".format(
            ", ".join(str(s) for s in failures)))
    else:
        print("All seasons fetched successfully.")


if __name__ == "__main__":
    main()
