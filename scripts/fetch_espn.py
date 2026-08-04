#!/usr/bin/env python3
"""
Pull raw ESPN fantasy football league data and print a short summary.

Phase 1a of the waiver pickup simulator: this exists to confirm what ESPN
actually returns before any of the engine gets written against it.

Three independent probes, selected with ESPN_SECTIONS:

  league    settings, teams, waiver order          (confirmed working)
  pool      free agent / waiver player pool        (new)
  activity  transaction history via activity feed  (new)

Each probe catches its own failures, so one endpoint misbehaving never
hides the results of another.

Saves full untouched responses to out/ and prints only what the build
depends on, so the Actions log stays readable.

Standard library only -- no pip install step, no dependency drift.
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
SEASONS = [int(s) for s in os.environ.get("ESPN_SEASONS", "2025").split(",")]
SECTIONS = [s.strip() for s in
            os.environ.get("ESPN_SECTIONS", "transactions").split(",")]

# Which week to ask the pool about. 0 means "whatever ESPN considers current".
SCORING_PERIOD = int(os.environ.get("ESPN_SCORING_PERIOD", "0"))

LEAGUE_VIEWS = ["mSettings", "mTeam", "mRoster", "mTransactions2"]

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
OUT = pathlib.Path("out")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; alpine-waiver-sim/0.3)",
    "Accept": "application/json",
}

ESPN_S2 = os.environ.get("ESPN_S2", "")
SWID = os.environ.get("SWID", "")

# How many players to ask for. The pool is large; this is plenty to
# confirm the shape without pulling every kicker in the league.
POOL_LIMIT = int(os.environ.get("ESPN_POOL_LIMIT", "150"))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def get(url, fantasy_filter=None):
    """Fetch JSON. Returns (data, error_string)."""
    headers = dict(HEADERS)
    if ESPN_S2 and SWID:
        headers["Cookie"] = "espn_s2={}; SWID={}".format(ESPN_S2, SWID)
    if fantasy_filter is not None:
        headers["X-Fantasy-Filter"] = json.dumps(fantasy_filter)

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response), None
    except urllib.error.HTTPError as err:
        body = ""
        try:
            body = err.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        return None, "HTTP {} -- {} {}".format(err.code, err.reason, body)
    except urllib.error.URLError as err:
        return None, "connection failed -- {}".format(err.reason)
    except json.JSONDecodeError as err:
        return None, "response was not JSON -- {}".format(err)


def league_url(season, views, suffix=""):
    query = "&".join("view=" + v for v in views)
    return "{base}/seasons/{season}/segments/0/leagues/{league}{suffix}?{query}".format(
        base=BASE, season=season, league=LEAGUE_ID, suffix=suffix, query=query
    )


def save(name, data):
    OUT.mkdir(exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(data, indent=2))
    print("  saved {} ({:,} bytes)".format(path, path.stat().st_size))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def line(char="-", width=68):
    print(char * width)


def banner(text):
    line("=")
    print(text)
    line("=")


def keys_of(obj, label):
    if isinstance(obj, dict):
        print("  keys on {}:".format(label))
        print("    " + ", ".join(sorted(obj.keys())))


def find_keys_containing(obj, needle, path="", found=None, depth=0):
    """
    Walk a nested structure looking for key names containing `needle`.

    Rather than guessing what ESPN calls the waiver fields, we go and
    look. Cheaper than being wrong later.
    """
    if found is None:
        found = {}
    if depth > 4:
        return found
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = "{}.{}".format(path, key) if path else key
            if needle.lower() in key.lower():
                found[here] = value if not isinstance(
                    value, (dict, list)) else type(value).__name__
            find_keys_containing(value, needle, here, found, depth + 1)
    elif isinstance(obj, list) and obj:
        find_keys_containing(obj[0], needle, path + "[0]", found, depth + 1)
    return found


def team_name(team):
    if team.get("name"):
        return team["name"].strip()
    parts = [team.get("location", ""), team.get("nickname", "")]
    return " ".join(p for p in parts if p).strip() or "(unnamed)"


# ---------------------------------------------------------------------------
# Probe: league
# ---------------------------------------------------------------------------


def probe_league(season):
    banner("LEAGUE  season {}  league {}".format(season, LEAGUE_ID))
    data, error = get(league_url(season, LEAGUE_VIEWS))
    if error:
        print("  FAILED: {}".format(error))
        return

    settings = data.get("settings") or {}
    print("  name: {}   size: {}".format(
        settings.get("name"), settings.get("size")))
    print("  scoringPeriodId: {}".format(data.get("scoringPeriodId")))

    teams = data.get("teams") or []
    print("\n  {:>4}  {:<26} {:>7}  {:>6}".format(
        "id", "name", "waiver", "roster"))
    for team in teams:
        entries = (team.get("roster") or {}).get("entries") or []
        print("  {:>4}  {:<26} {:>7}  {:>6}".format(
            team.get("id"), team_name(team)[:26],
            team.get("waiverRank", "-"), len(entries)))

    if teams:
        counter = teams[0].get("transactionCounter")
        if counter:
            print("\n  transactionCounter on team {} (the fallback signal):".format(
                teams[0].get("id")))
            for key in sorted(counter.keys()):
                value = counter[key]
                if not isinstance(value, (dict, list)):
                    print("    {}: {}".format(key, value))

    save("espn_league_{}_{}.json".format(LEAGUE_ID, season), data)
    print()


# ---------------------------------------------------------------------------
# Probe: free agent / waiver pool
# ---------------------------------------------------------------------------


def probe_pool(season):
    banner("POOL  season {}  scoringPeriod {}".format(season, SCORING_PERIOD))

    url = league_url(season, ["kona_player_info"])
    if SCORING_PERIOD:
        url += "&scoringPeriodId={}".format(SCORING_PERIOD)

    fantasy_filter = {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "limit": POOL_LIMIT,
            "offset": 0,
            "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
        }
    }

    data, error = get(url, fantasy_filter)
    if error:
        print("  FAILED: {}".format(error))
        print("  url: {}".format(url))
        print("  filter: {}".format(json.dumps(fantasy_filter)))
        return

    print("  top-level keys: " + ", ".join(sorted(data.keys())))
    players = data.get("players")
    if players is None:
        print("  no 'players' key returned -- filter shape may be wrong")
        save("espn_pool_{}_{}.json".format(LEAGUE_ID, season), data)
        return

    print("  players returned: {}".format(len(players)))
    if not players:
        return

    first = players[0]
    keys_of(first, "a pool entry")
    keys_of(first.get("player"), "the nested player object")

    # Status distribution -- this is what green vs yellow will look like.
    statuses = {}
    for entry in players:
        status = entry.get("status") or (entry.get("player") or {}).get("status")
        statuses[status] = statuses.get(status, 0) + 1
    print("\n  status distribution: " + ", ".join(
        "{}={}".format(k, v) for k, v in sorted(statuses.items(), key=str)))

    on_team = sum(1 for e in players if e.get("onTeamId"))
    print("  entries with a non-zero onTeamId: {}".format(on_team))

    # Go looking for the waiver fields rather than guessing their names.
    waiver_fields = find_keys_containing(first, "waiver")
    print("\n  keys containing 'waiver' on a pool entry:")
    if waiver_fields:
        for path in sorted(waiver_fields):
            print("    {} = {}".format(path, waiver_fields[path]))
    else:
        print("    none found (expected on a completed season -- nothing")
        print("    is actively on waivers once the year is over)")

    ownership = (first.get("player") or {}).get("ownership")
    keys_of(ownership, "the ownership object")

    stats = (first.get("player") or {}).get("stats") or []
    print("\n  stats entries on first player: {}".format(len(stats)))
    if stats:
        keys_of(stats[0], "a stats entry")
        combos = {}
        for entry in stats:
            key = (entry.get("statSourceId"), entry.get("statSplitTypeId"))
            combos[key] = combos.get(key, 0) + 1
        print("  (statSourceId, statSplitTypeId) counts:")
        for key in sorted(combos, key=str):
            print("    {} x{}   (sourceId 0=actual, 1=projected)".format(
                key, combos[key]))

    print("\n  first 10 by percent owned:")
    for entry in players[:10]:
        player = entry.get("player") or {}
        ownership = player.get("ownership") or {}
        print("    {:>7}  {:<24} posId {:<3} owned {:>5}%  injured={}".format(
            player.get("id"),
            str(player.get("fullName"))[:24],
            player.get("defaultPositionId"),
            round(ownership.get("percentOwned", 0), 1),
            player.get("injuryStatus", "-"),
        ))

    save("espn_pool_{}_{}.json".format(LEAGUE_ID, season), data)
    print()


# ---------------------------------------------------------------------------
# Probe: activity feed (transaction history)
# ---------------------------------------------------------------------------


def probe_activity(season):
    banner("ACTIVITY  season {}".format(season))

    url = ("{base}/seasons/{season}/segments/0/leagues/{league}"
           "/communication/?view=kona_league_communication").format(
        base=BASE, season=season, league=LEAGUE_ID)

    fantasy_filter = {
        "topics": {
            "filterType": {"value": ["ACTIVITY_TRANSACTIONS"]},
            "limit": 100,
            "limitPerMessageSet": {"value": 100},
            "offset": 0,
            "sortMessageDate": {"sortPriority": 1, "sortAsc": False},
            "sortFor": {"sortPriority": 2, "sortAsc": False},
        }
    }

    data, error = get(url, fantasy_filter)
    if error:
        print("  FAILED: {}".format(error))
        print("  url: {}".format(url))
        print("  (if this is a 401, the activity feed needs auth even on")
        print("   a public league -- fall back to transactionCounter)")
        return

    print("  top-level keys: " + ", ".join(sorted(data.keys())))
    topics = data.get("topics") or []
    print("  topics returned: {}".format(len(topics)))
    if not topics:
        print("  empty feed -- filter may need messageTypeId values")
        save("espn_activity_{}_{}.json".format(LEAGUE_ID, season), data)
        return

    keys_of(topics[0], "a topic")
    messages = topics[0].get("messages") or []
    print("  messages on first topic: {}".format(len(messages)))
    if messages:
        keys_of(messages[0], "a message")

    total = sum(len(t.get("messages") or []) for t in topics)
    print("\n  total messages across all topics: {}".format(total))

    type_counts = {}
    for topic in topics:
        for message in (topic.get("messages") or []):
            type_id = message.get("messageTypeId")
            type_counts[type_id] = type_counts.get(type_id, 0) + 1
    print("  messageTypeId distribution: " + ", ".join(
        "{}={}".format(k, v) for k, v in sorted(type_counts.items(), key=str)))

    print("\n  first 8 messages raw:")
    shown = 0
    for topic in topics:
        for message in (topic.get("messages") or []):
            if shown >= 8:
                break
            print("    {}".format(json.dumps(message)[:170]))
            shown += 1
        if shown >= 8:
            break

    save("espn_activity_{}_{}.json".format(LEAGUE_ID, season), data)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def probe_transactions(season):
    """
    The activity feed 404'd and mTransactions2 was first requested with no
    filter header at all. Rather than guess again, try several documented-by-
    reverse-engineering shapes in one run and report which ones return data.
    """
    banner("TRANSACTIONS  season {}".format(season))

    league = league_url(season, ["mTransactions2"])
    comm_base = ("{base}/seasons/{season}/segments/0/leagues/{league}"
                 "/communication").format(
        base=BASE, season=season, league=LEAGUE_ID)

    attempts = [
        ("A  mTransactions2 + full filter", league, {
            "transactions": {
                "filterType": {"value": ["WAIVER", "FREEAGENT", "TRADE",
                                         "ROSTER", "DRAFT"]},
                "limit": 1000, "offset": 0,
                "sortDate": {"sortPriority": 1, "sortAsc": False},
            }
        }),
        ("B  mTransactions2 + minimal filter", league, {
            "transactions": {"limit": 1000, "offset": 0}
        }),
        ("C  communication, no trailing slash",
         comm_base + "?view=kona_league_communication", {
             "topics": {
                 "filterType": {"value": ["ACTIVITY_TRANSACTIONS"]},
                 "limit": 100, "offset": 0,
             }
         }),
        ("D  communication + messageTypeIds",
         comm_base + "/?view=kona_league_communication", {
             "topics": {
                 "filterType": {"value": ["ACTIVITY_TRANSACTIONS"]},
                 "limit": 100,
                 "limitPerMessageSet": {"value": 100},
                 "offset": 0,
                 "sortMessageDate": {"sortPriority": 1, "sortAsc": False},
                 "filterIncludeMessageTypeIds": {
                     "value": [178, 180, 179, 239, 181, 244]},
             }
         }),
    ]

    winner = None
    for label, url, fantasy_filter in attempts:
        print("\n{}".format(label))
        data, error = get(url, fantasy_filter)
        if error:
            print("  FAILED: {}".format(error[:220]))
            continue

        keys = sorted(data.keys())
        print("  OK. top-level keys: {}".format(", ".join(keys)))

        rows = data.get("transactions")
        if rows is not None:
            print("  transactions: {} entries".format(len(rows)))
            if rows:
                keys_of(rows[0], "a transaction")
                types = {}
                for row in rows:
                    types[row.get("type")] = types.get(row.get("type"), 0) + 1
                print("  types: " + ", ".join(
                    "{}={}".format(k, v) for k, v in sorted(types.items(), key=str)))
                winner = winner or (label, data)
            continue

        topics = data.get("topics")
        if topics is not None:
            total = sum(len(t.get("messages") or []) for t in topics)
            print("  topics: {}   messages: {}".format(len(topics), total))
            if total:
                for topic in topics:
                    for message in (topic.get("messages") or []):
                        keys_of(message, "a message")
                        print("  sample: {}".format(json.dumps(message)[:200]))
                        break
                    break
                winner = winner or (label, data)
            continue

        print("  neither 'transactions' nor 'topics' present")

    print()
    line("=")
    if winner:
        print("WORKING SHAPE: {}".format(winner[0]))
        save("espn_transactions_{}_{}.json".format(LEAGUE_ID, season), winner[1])
    else:
        print("No shape returned transaction data.")
        print("Falling back to transactionCounter, printed below.")
    line("=")

    # Always pull the fallback signal so one run answers both questions.
    print("\ntransactionCounter per team (the fallback for activity rates):")
    data, error = get(league_url(season, ["mTeam"]))
    if error:
        print("  FAILED: {}".format(error))
        return
    teams = data.get("teams") or []
    for team in teams:
        counter = team.get("transactionCounter") or {}
        flat = {k: v for k, v in counter.items()
                if not isinstance(v, (dict, list))}
        print("  {:>3}  {:<24} {}".format(
            team.get("id"), team_name(team)[:24],
            ", ".join("{}={}".format(k, flat[k]) for k in sorted(flat))))
    print()


PROBES = {
    "league": probe_league,
    "transactions": probe_transactions,
    "pool": probe_pool,
    "activity": probe_activity,
}


def main():
    print("league {}   seasons {}   sections {}\n".format(
        LEAGUE_ID, SEASONS, SECTIONS))

    unknown = [s for s in SECTIONS if s not in PROBES]
    if unknown:
        print("Unknown section(s): {}".format(", ".join(unknown)))
        print("Valid: {}\n".format(", ".join(sorted(PROBES))))

    for season in SEASONS:
        for section in SECTIONS:
            probe = PROBES.get(section)
            if probe:
                probe(season)

    print("Done.")


if __name__ == "__main__":
    main()
