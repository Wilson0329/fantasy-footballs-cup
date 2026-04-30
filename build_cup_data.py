#!/usr/bin/env python3
"""
Fantasy Footballs Cup — data builder.
Fetches FPL API data for league 286779 and calculates cup results,
outputting cup_data.json for the Lovable frontend.

Usage:
    python3 build_cup_data.py
    python3 build_cup_data.py --output path/to/cup_data.json
"""

import argparse
import json
import time
from datetime import date

import requests

BASE = "https://fantasy.premierleague.com/api"
LEAGUE_ID = 286779
CUP_NAME = "Fantasy Footballs Cup"

# All 12 teams in the league
TEAMS = [
    {"entry_id": 8794696, "name": "Return of the Fox",     "manager": "Charles Smith"},
    {"entry_id": 7552664, "name": "Beef Cherki",            "manager": "Alexander Bodek"},
    {"entry_id": 5155229, "name": "Vik the Impaler",        "manager": "David Pontin"},
    {"entry_id": 5124113, "name": "Soucek Madness",         "manager": "Sam Haseltine"},
    {"entry_id": 1623042, "name": "GVG XI",                 "manager": "George Georgiou"},
    {"entry_id": 6366909, "name": "HC XI II",               "manager": "Dominic Byrne"},
    {"entry_id": 5751594, "name": "I love big Győk",        "manager": "Adam Georghiou"},
    {"entry_id": 4789233, "name": "Inevitable",             "manager": "Jason Knightly"},
    {"entry_id": 5191754, "name": "Stay Classy SanDiogo",  "manager": "Ed Pragnell"},
    {"entry_id": 5145283, "name": "Habibi Jeebies",         "manager": "Arun Quayum"},
    {"entry_id": 4160647, "name": "Forever20",              "manager": "Joe Wilson"},
    {"entry_id": 5150105, "name": "Gyök, Stock & Barrel",  "manager": "Aron Rouse"},
]

# Cup GW schedule
GROUP_GWS = [18, 20, 22, 24, 26]
SEMI_GWS = [31, 33]
FINAL_GW = 38

# Round-robin fixture pattern for 6 teams (positions 1-6 within group)
# Each tuple is (team_rank_a, team_rank_b) — 1-indexed within the group
GROUP_ROUNDS = [
    [(1, 2), (3, 4), (5, 6)],
    [(1, 3), (2, 5), (4, 6)],
    [(1, 4), (2, 6), (3, 5)],
    [(1, 5), (2, 4), (3, 6)],
    [(1, 6), (2, 3), (4, 5)],
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})


def fetch(path, retries=3):
    url = f"{BASE}{path}"
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            print(f"  Retry {attempt + 1} for {path}: {e}")
            time.sleep(1)


def current_gw(bootstrap):
    for event in bootstrap["events"]:
        if event["is_current"]:
            return event["id"]
    for event in reversed(bootstrap["events"]):
        if event["is_finished"]:
            return event["id"]
    return 1


def gw_is_finished(bootstrap, gw):
    for event in bootstrap["events"]:
        if event["id"] == gw:
            return event["finished"]
    return False


# ─── Cup score calculation ───────────────────────────────────────────────────

def cup_score_from_picks(picks_data, live_points):
    """
    Cup score = sum of starting XI raw points (no captain bonus).
    Positions 1-11 are always the starting XI regardless of any chip.
    """
    score = 0
    captain_pts = 0
    vice_pts = 0

    for pick in picks_data["picks"]:
        if pick["multiplier"] == 0:   # bench player (not playing this GW, even with auto-subs)
            continue
        pid = pick["element"]
        pts = live_points.get(pid, 0)
        score += pts
        if pick["is_captain"]:
            captain_pts = pts
        if pick["is_vice_captain"]:
            vice_pts = pts

    return {"score": score, "captain_pts": captain_pts, "vice_pts": vice_pts}


def match_result(home_data, away_data):
    """Returns 'home', 'away', or 'draw'. Uses captain/vice as tiebreaker."""
    if home_data["score"] > away_data["score"]:
        return "home"
    if away_data["score"] > home_data["score"]:
        return "away"
    # Tiebreak: captain points
    if home_data["captain_pts"] > away_data["captain_pts"]:
        return "home"
    if away_data["captain_pts"] > home_data["captain_pts"]:
        return "away"
    # Second tiebreak: vice captain
    if home_data["vice_pts"] > away_data["vice_pts"]:
        return "home"
    if away_data["vice_pts"] > home_data["vice_pts"]:
        return "away"
    return "draw"


# ─── GW17 standings ──────────────────────────────────────────────────────────

def get_gw17_points(entry_id):
    history = fetch(f"/entry/{entry_id}/history/")
    for gw in history["current"]:
        if gw["event"] == 17:
            return gw["total_points"]
    return 0


def build_gw17_standings():
    print("Fetching GW17 standings for all 12 teams...")
    standings = []
    for team in TEAMS:
        pts = get_gw17_points(team["entry_id"])
        standings.append({**team, "gw17_points": pts})
        print(f"  {team['name']}: {pts} pts")
    standings.sort(key=lambda t: t["gw17_points"], reverse=True)
    return standings


# ─── Live player points ───────────────────────────────────────────────────────

_live_cache = {}

def get_live_points(gw):
    if gw not in _live_cache:
        print(f"  Fetching live data for GW{gw}...")
        data = fetch(f"/event/{gw}/live/")
        _live_cache[gw] = {el["id"]: el["stats"]["total_points"] for el in data["elements"]}
    return _live_cache[gw]


# ─── Group stage ─────────────────────────────────────────────────────────────

def build_group(group_label, group_teams, bootstrap):
    """Build fixtures and standings for one group."""
    print(f"\nBuilding Group {group_label}...")

    # Map rank (1-6 within group) to team
    ranked = {i + 1: group_teams[i] for i in range(6)}

    fixtures = []
    # Accumulate results for standings
    records = {t["entry_id"]: {
        "entry_id": t["entry_id"], "name": t["name"], "manager": t["manager"],
        "played": 0, "won": 0, "drawn": 0, "lost": 0,
        "points_for": 0, "points_against": 0, "cup_points": 0,
    } for t in group_teams}

    # Head-to-head record: h2h[a][b] = "win"/"draw"/"loss" from a's perspective
    h2h = {t["entry_id"]: {t2["entry_id"]: None for t2 in group_teams} for t in group_teams}

    for round_idx, round_fixtures in enumerate(GROUP_ROUNDS):
        gw = GROUP_GWS[round_idx]
        finished = gw_is_finished(bootstrap, gw)
        matches = []

        for (rank_a, rank_b) in round_fixtures:
            home_team = ranked[rank_a]
            away_team = ranked[rank_b]

            if not finished:
                matches.append({
                    "home": {"entry_id": home_team["entry_id"], "name": home_team["name"], "score": None, "captain_pts": None, "vice_pts": None},
                    "away": {"entry_id": away_team["entry_id"], "name": away_team["name"], "score": None, "captain_pts": None, "vice_pts": None},
                    "result": None,
                    "status": "upcoming",
                })
                continue

            live = get_live_points(gw)

            home_picks = fetch(f"/entry/{home_team['entry_id']}/event/{gw}/picks/")
            away_picks = fetch(f"/entry/{away_team['entry_id']}/event/{gw}/picks/")

            home_data = cup_score_from_picks(home_picks, live)
            away_data = cup_score_from_picks(away_picks, live)
            result = match_result(home_data, away_data)

            # Update records
            hid, aid = home_team["entry_id"], away_team["entry_id"]
            records[hid]["played"] += 1
            records[aid]["played"] += 1
            records[hid]["points_for"] += home_data["score"]
            records[hid]["points_against"] += away_data["score"]
            records[aid]["points_for"] += away_data["score"]
            records[aid]["points_against"] += home_data["score"]

            if result == "home":
                records[hid]["won"] += 1
                records[hid]["cup_points"] += 2
                records[aid]["lost"] += 1
                h2h[hid][aid] = "win"
                h2h[aid][hid] = "loss"
            elif result == "away":
                records[aid]["won"] += 1
                records[aid]["cup_points"] += 2
                records[hid]["lost"] += 1
                h2h[hid][aid] = "loss"
                h2h[aid][hid] = "win"
            else:
                records[hid]["drawn"] += 1
                records[hid]["cup_points"] += 1
                records[aid]["drawn"] += 1
                records[aid]["cup_points"] += 1
                h2h[hid][aid] = "draw"
                h2h[aid][hid] = "draw"

            matches.append({
                "home": {"entry_id": hid, "name": home_team["name"],
                         "score": home_data["score"], "captain_pts": home_data["captain_pts"], "vice_pts": home_data["vice_pts"]},
                "away": {"entry_id": aid, "name": away_team["name"],
                         "score": away_data["score"], "captain_pts": away_data["captain_pts"], "vice_pts": away_data["vice_pts"]},
                "result": result,
                "status": "complete",
            })

        fixtures.append({
            "gw": gw,
            "label": f"Round {round_idx + 1}",
            "status": "complete" if finished else "upcoming",
            "matches": matches,
        })

    standings = build_standings(list(records.values()), h2h)
    for i, row in enumerate(standings):
        row["position"] = i + 1
        row["qualified"] = i < 2  # top 2 qualify

    return {"teams": [t for t in group_teams], "fixtures": fixtures, "standings": standings}


def build_standings(records, h2h):
    """Sort with cup tiebreaker rules."""

    def h2h_wins(a_id, others_ids):
        wins = 0
        for b_id in others_ids:
            if h2h[a_id].get(b_id) == "win":
                wins += 1
        return wins

    def sort_key(r):
        return (-r["cup_points"], -r["points_for"])

    records.sort(key=sort_key)

    # Apply tiebreakers within groups of equal cup_points
    result = []
    i = 0
    while i < len(records):
        j = i
        while j < len(records) and records[j]["cup_points"] == records[i]["cup_points"]:
            j += 1
        tied = records[i:j]

        if len(tied) == 1:
            result.extend(tied)
        elif len(tied) == 2:
            a, b = tied[0], tied[1]
            a_id, b_id = a["entry_id"], b["entry_id"]
            if h2h[a_id].get(b_id) == "win":
                result.extend([a, b])
            elif h2h[b_id].get(a_id) == "win":
                result.extend([b, a])
            else:
                # H2H draw — sort by points_for
                result.extend(sorted(tied, key=lambda r: -r["points_for"]))
        else:
            # 3+ teams — H2H wins within the tied group, then points_for
            tied_ids = [r["entry_id"] for r in tied]
            result.extend(sorted(tied, key=lambda r: (
                -h2h_wins(r["entry_id"], [x for x in tied_ids if x != r["entry_id"]]),
                -r["points_for"]
            )))
        i = j

    return result


# ─── Knockout ────────────────────────────────────────────────────────────────

def build_knockout(group_a_standings, group_b_standings, bootstrap):
    print("\nBuilding knockout stage...")

    qualifiers_a = [s for s in group_a_standings if s["qualified"]]  # A1, A2
    qualifiers_b = [s for s in group_b_standings if s["qualified"]]  # B1, B2

    # A1 vs B2, A2 vs B1
    semi_matchups = [
        (qualifiers_a[0], qualifiers_b[1]),
        (qualifiers_a[1], qualifiers_b[0]),
    ]

    semi_finals = []
    final_teams = []

    for idx, (team_a, team_b) in enumerate(semi_matchups):
        legs = []
        agg_a, agg_b = 0, 0

        for gw in SEMI_GWS:
            if not gw_is_finished(bootstrap, gw):
                legs.append({"gw": gw, "score_a": None, "score_b": None, "status": "upcoming"})
                continue

            live = get_live_points(gw)
            picks_a = fetch(f"/entry/{team_a['entry_id']}/event/{gw}/picks/")
            picks_b = fetch(f"/entry/{team_b['entry_id']}/event/{gw}/picks/")
            data_a = cup_score_from_picks(picks_a, live)
            data_b = cup_score_from_picks(picks_b, live)
            agg_a += data_a["score"]
            agg_b += data_b["score"]
            legs.append({
                "gw": gw,
                "score_a": data_a["score"],
                "score_b": data_b["score"],
                "status": "complete",
            })

        # Determine winner if both legs played
        all_played = all(l["status"] == "complete" for l in legs)
        winner = None
        if all_played:
            if agg_a > agg_b:
                winner = {"entry_id": team_a["entry_id"], "name": team_a["name"]}
            elif agg_b > agg_a:
                winner = {"entry_id": team_b["entry_id"], "name": team_b["name"]}
            # Aggregate tie: could add further tiebreaker; leave as None for now
            final_teams.append(winner or {"entry_id": None, "name": "TBD"})
        else:
            final_teams.append({"entry_id": None, "name": "TBD"})

        semi_finals.append({
            "label": f"Semi-Final {idx + 1}",
            "team_a": {"entry_id": team_a["entry_id"], "name": team_a["name"], "manager": team_a["manager"]},
            "team_b": {"entry_id": team_b["entry_id"], "name": team_b["name"], "manager": team_b["manager"]},
            "legs": legs,
            "aggregate_a": agg_a,
            "aggregate_b": agg_b,
            "winner": winner,
        })

    # Final
    final_team_a = final_teams[0] if len(final_teams) > 0 else {"entry_id": None, "name": "TBD"}
    final_team_b = final_teams[1] if len(final_teams) > 1 else {"entry_id": None, "name": "TBD"}
    final_finished = gw_is_finished(bootstrap, FINAL_GW)
    final_score_a, final_score_b, final_winner = None, None, None

    if final_finished and final_team_a["entry_id"] and final_team_b["entry_id"]:
        live = get_live_points(FINAL_GW)
        picks_a = fetch(f"/entry/{final_team_a['entry_id']}/event/{FINAL_GW}/picks/")
        picks_b = fetch(f"/entry/{final_team_b['entry_id']}/event/{FINAL_GW}/picks/")
        data_a = cup_score_from_picks(picks_a, live)
        data_b = cup_score_from_picks(picks_b, live)
        final_score_a = data_a["score"]
        final_score_b = data_b["score"]
        if data_a["score"] > data_b["score"]:
            final_winner = final_team_a
        elif data_b["score"] > data_a["score"]:
            final_winner = final_team_b

    return {
        "semi_finals": semi_finals,
        "final": {
            "gw": FINAL_GW,
            "team_a": final_team_a,
            "team_b": final_team_b,
            "score_a": final_score_a,
            "score_b": final_score_b,
            "winner": final_winner,
            "status": "complete" if final_finished else "upcoming",
        },
    }


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/cup_data.json")
    args = parser.parse_args()

    print("Fetching bootstrap data...")
    bootstrap = fetch("/bootstrap-static/")
    cur_gw = current_gw(bootstrap)
    print(f"Current GW: {cur_gw}")

    # GW17 standings → split into groups
    gw17 = build_gw17_standings()
    print("\nGW17 standings (determines groups):")
    for i, t in enumerate(gw17):
        group = "A" if i < 6 else "B"
        print(f"  {i+1}. [{group}] {t['name']} — {t['gw17_points']} pts")

    group_a_teams = gw17[:6]
    group_b_teams = gw17[6:]

    group_a = build_group("A", group_a_teams, bootstrap)
    group_b = build_group("B", group_b_teams, bootstrap)

    knockout = build_knockout(group_a["standings"], group_b["standings"], bootstrap)

    cup_data = {
        "metadata": {
            "cup_name": CUP_NAME,
            "league_id": LEAGUE_ID,
            "last_updated": date.today().isoformat(),
            "current_gw": cur_gw,
            "season": "2025/26",
        },
        "groups": {
            "A": group_a,
            "B": group_b,
        },
        "knockout": knockout,
    }

    with open(args.output, "w") as f:
        json.dump(cup_data, f, indent=2)

    print(f"\nDone! Written to {args.output}")
    print(f"Group A qualifiers: {[s['name'] for s in group_a['standings'] if s['qualified']]}")
    print(f"Group B qualifiers: {[s['name'] for s in group_b['standings'] if s['qualified']]}")


if __name__ == "__main__":
    main()
