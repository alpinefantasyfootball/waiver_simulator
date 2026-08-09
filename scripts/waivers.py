#!/usr/bin/env python3
"""
Waiver rules and claim sequencing.

Phase 1e of the waiver pickup simulator. The engine says which players
would help; this says which claims to actually submit, in what order, and
what each is likely to cost.

Three things live here:

  1. Order resolution -- who claims ahead of you, under either ESPN
     format, and what the order looks like afterwards.

  2. Priority as a resource -- in the rolling league a successful claim
     drops you to last, and you climb back one slot per successful claim
     by anyone else. That cost is finite and computable.

  3. Sequencing -- both leagues process waivers six days a week and
     players clear on their own schedules, so this is a scheduling
     problem across dates rather than a ranked list. Winning a claim
     Wednesday can cost you a better player clearing Thursday.

The opponent model is deliberately a thin interface with a naive
implementation behind it. Real per-manager behaviour needs transaction
history, which only starts accruing in Week 1.

Pure computation. No APIs, no files required.
"""

import itertools
import json
import os

ROLLING = "rolling"
WEEKLY_RESET = "weekly_reset"


# ---------------------------------------------------------------------------
# Order resolution
# ---------------------------------------------------------------------------


def order_from_waiver_ranks(teams):
    """[team_id] sorted by current waiver rank, best priority first."""
    ranked = [t for t in teams if t.get("waiver_rank") is not None]
    ranked.sort(key=lambda t: t["waiver_rank"])
    return [t["team_id"] for t in ranked]


def order_from_standings(teams):
    """Inverse standings: worst record claims first."""
    def key(team):
        record = team.get("record") or {}
        wins = record.get("wins") or 0
        points = record.get("points_for") or 0
        return (wins, points)
    return [t["team_id"] for t in sorted(teams, key=key)]


def resolve_run(order, claims, mode):
    """
    Process one waiver run.

    order  : [team_id], best priority first
    claims : {team_id: [player_id, ...]} in that team's own preference order
    mode   : ROLLING or WEEKLY_RESET

    Returns (awards, new_order) where awards is {player_id: team_id}.

    A team's claims are all processed when its turn comes, and only the
    first successful award costs it position -- which is why bundling
    claims into one run is cheaper than spreading them across days.
    """
    awards = {}
    taken = set()
    moved = []

    for team_id in order:
        won_any = False
        for player_id in claims.get(team_id) or []:
            if player_id in taken:
                continue
            awards[player_id] = team_id
            taken.add(player_id)
            won_any = True
        if won_any:
            moved.append(team_id)

    if mode == ROLLING:
        new_order = [t for t in order if t not in moved] + moved
    else:
        new_order = list(order)

    return awards, new_order


def slot_of(order, team_id):
    """1-based priority position, or None if the team isn't in the order."""
    return order.index(team_id) + 1 if team_id in order else None


# ---------------------------------------------------------------------------
# Priority as a resource
# ---------------------------------------------------------------------------


def recovery_runs(from_slot, to_slot, claims_per_run):
    """
    How many waiver runs it takes to climb from one slot back to another.

    You move up one place for every successful claim another team makes,
    so this is just distance divided by league activity. Returns None when
    nobody ever claims and you'd never move.
    """
    distance = from_slot - to_slot
    if distance <= 0:
        return 0.0
    if not claims_per_run:
        return None
    return round(distance / claims_per_run, 2)


def claims_per_run_from_activity(teams, runs_per_week, weeks_played):
    """
    Fit league claim volume from transactionCounter totals.

    Coarse -- acquisitions lump waiver claims together with free agent
    adds, and only claims move the order. Treated as an upper bound until
    real transaction history exists.
    """
    total = sum((t.get("activity") or {}).get("acquisitions") or 0
                for t in teams)
    runs = max(1, runs_per_week * max(1, weeks_played))
    return round(total / runs, 3)


# ---------------------------------------------------------------------------
# Opponent model
# ---------------------------------------------------------------------------


class OpponentModel:
    """
    Probability that a given team claims a given player in one run.

    The naive implementation multiplies how often a manager claims at all
    by how appealing the player is to them. Swap in a fitted version once
    there is transaction history; everything downstream only calls
    claim_probability.
    """

    def __init__(self, activity_per_run=None, appeal=None, default_appeal=0.1):
        self.activity_per_run = activity_per_run or {}
        self.appeal = appeal or {}
        self.default_appeal = default_appeal

    def claim_probability(self, team_id, player_id):
        rate = self.activity_per_run.get(team_id, 0.0)
        if rate <= 0:
            return 0.0
        appeal = self.appeal.get((team_id, player_id), self.default_appeal)
        return max(0.0, min(1.0, rate * appeal))


def probability_of_landing(order, my_team, player_id, opponent):
    """
    Chance the player survives to your turn.

    Only teams ahead of you matter -- a team behind you can't take him
    first regardless of how badly they want him.
    """
    my_slot = slot_of(order, my_team)
    if my_slot is None:
        return 0.0
    survival = 1.0
    for team_id in order[:my_slot - 1]:
        survival *= (1.0 - opponent.claim_probability(team_id, player_id))
    return round(survival, 4)


# ---------------------------------------------------------------------------
# Sequencing across process dates
# ---------------------------------------------------------------------------


def plan_claims(dates, my_team, order, mode, opponent, max_claims_per_run=3):
    """
    Choose which claims to submit on which date.

    dates: [{"label": str, "candidates": [{"player_id", "value"}]}]
           in chronological order, each the set clearing that day.

    Under ROLLING, winning on an early date drops you to last for every
    later date, so passing on a modest claim today can be worth more than
    taking it. Under WEEKLY_RESET the order is restored, so the question
    collapses to which claims to make.

    Returns {"expected_value", "plan": [{date, claim, probability, value}]}.
    """
    def search(index, current_order):
        if index >= len(dates):
            return 0.0, []

        date = dates[index]
        candidates = sorted(
            date.get("candidates") or [],
            key=lambda c: -c.get("value", 0.0))[:max_claims_per_run]

        # Option one: claim nothing today.
        best_value, best_plan = search(index + 1, current_order)
        best_plan = [{"date": date["label"], "claim": None}] + best_plan

        for candidate in candidates:
            probability = probability_of_landing(
                current_order, my_team, candidate["player_id"], opponent)
            value = candidate.get("value", 0.0)

            # If we win, we move to the back for everything after.
            won_order = current_order
            if mode == ROLLING:
                won_order = ([t for t in current_order if t != my_team]
                             + [my_team])

            won_rest, won_plan = search(index + 1, won_order)
            lost_rest, lost_plan = search(index + 1, current_order)

            expected = (probability * (value + won_rest)
                        + (1 - probability) * lost_rest)

            if expected > best_value:
                best_value = expected
                rest_plan = won_plan if probability >= 0.5 else lost_plan
                best_plan = [{
                    "date": date["label"],
                    "claim": candidate["player_id"],
                    "probability": probability,
                    "value": value,
                }] + rest_plan

        return best_value, best_plan

    value, plan = search(0, list(order))
    return {"expected_value": round(value, 2), "plan": plan}


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def scenarios():
    print("=" * 68)
    print("1. ROLLING -- a successful claim sends you to the back")
    print("=" * 68)
    order = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    awards, new_order = resolve_run(order, {1: ["playerA"]}, ROLLING)
    print("  before:", order)
    print("  team 1 claims playerA ->", awards)
    print("  after :", new_order)
    assert new_order == [2, 3, 4, 5, 6, 7, 8, 9, 10, 1]
    print("  team 1 slot: {} -> {}".format(
        slot_of(order, 1), slot_of(new_order, 1)))

    print("\n  two winners in one run, in priority order:")
    awards, after2 = resolve_run(order, {1: ["a"], 4: ["b"]}, ROLLING)
    print("  after :", after2)
    assert after2[-2:] == [1, 4], after2
    print("  team 4 claimed later, so lands behind team 1 at the back")

    print("\n  a losing claim costs nothing:")
    awards, after3 = resolve_run(order, {1: ["x"], 5: ["x"]}, ROLLING)
    print("  awards:", awards, "-> only team 1 got him")
    print("  after :", after3)
    assert slot_of(after3, 5) == 4, "team 5 should move up, not down"

    print("\n" + "=" * 68)
    print("2. WEEKLY RESET -- claiming does not move you")
    print("=" * 68)
    awards, reset_order = resolve_run(order, {1: ["playerA"]}, WEEKLY_RESET)
    print("  after :", reset_order)
    assert reset_order == order

    print("\n" + "=" * 68)
    print("3. RECOVERY -- how long the back of the line lasts")
    print("=" * 68)
    for rate in (2.0, 0.7, 0.2):
        runs = recovery_runs(10, 1, rate)
        print("  {:.1f} claims/run -> {} runs to climb 10th back to 1st "
              "(~{:.1f} weeks at 6 runs/wk)".format(
                  rate, runs, runs / 6 if runs else 0))

    print("\n" + "=" * 68)
    print("4. LANDING PROBABILITY -- worse slot, worse odds")
    print("=" * 68)
    opponent = OpponentModel(
        activity_per_run={t: 0.5 for t in order}, default_appeal=0.6)
    for team in (1, 3, 6, 10):
        probability = probability_of_landing(order, team, "star", opponent)
        print("  slot {:>2}: {:.1%} chance he survives to you".format(
            slot_of(order, team), probability))

    print("\n" + "=" * 68)
    print("5. SEQUENCING -- the case a ranked list gets wrong")
    print("=" * 68)
    dates = [
        {"label": "Wed", "candidates": [{"player_id": "modest", "value": 6.0}]},
        {"label": "Thu", "candidates": [{"player_id": "big", "value": 15.0}]},
    ]
    quiet = OpponentModel(activity_per_run={t: 0.15 for t in order},
                          default_appeal=0.5)
    result = plan_claims(dates, my_team=3, order=order, mode=ROLLING,
                         opponent=quiet)
    print("  rolling league, you are slot 3:")
    print("   ", json.dumps(result, indent=2).replace("\n", "\n    "))

    reset = plan_claims(dates, my_team=3, order=order, mode=WEEKLY_RESET,
                        opponent=quiet)
    print("\n  same board, weekly-reset league:")
    print("    expected value {} vs {} under rolling".format(
        reset["expected_value"], result["expected_value"]))
    assert reset["expected_value"] >= result["expected_value"], (
        "resetting priority should never be worth less than spending it")
    print("    (reset is worth at least as much -- priority costs nothing "
          "there)")

    print("\n" + "=" * 68)
    print("6. SEQUENCING -- when passing is the right move")
    print("=" * 68)
    busy = OpponentModel(activity_per_run={t: 0.9 for t in order},
                         default_appeal=0.8)
    tight = [
        {"label": "Wed", "candidates": [{"player_id": "meh", "value": 2.0}]},
        {"label": "Thu", "candidates": [{"player_id": "star", "value": 25.0}]},
    ]
    busy_plan = plan_claims(tight, my_team=2, order=order, mode=ROLLING,
                            opponent=busy)
    wednesday = busy_plan["plan"][0]["claim"]
    print("  busy rolling league, you are slot 2")
    print("  Wednesday: 2-point player   Thursday: 25-point player")
    print("  -> Wednesday decision: {}".format(wednesday or "PASS"))
    assert wednesday is None, "should hold priority for the better player"

    reset_plan = plan_claims(tight, my_team=2, order=order, mode=WEEKLY_RESET,
                             opponent=busy)
    print("  -> same board, weekly reset: {}".format(
        reset_plan["plan"][0]["claim"]))
    assert reset_plan["plan"][0]["claim"] == "meh", (
        "with a free reset there is no reason to pass")
    print("  priority is only worth holding when spending it costs you")

    print("\nAll scenarios passed.")


if __name__ == "__main__":
    scenarios()
