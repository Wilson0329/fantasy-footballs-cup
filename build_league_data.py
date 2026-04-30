#!/usr/bin/env python3
"""
Fantasy Footballs League — data builder.
Outputs docs/league_data.json with standings, trade differential, and captain points.

Usage:
    python3 build_league_data.py
    python3 build_league_data.py --output path/to/league_data.json
"""

import argparse
import json
import time
from datetime import date

import requests

BASE = "https://fantasy.premierleague.com/api"
LEAGUE_ID = 286779

TEAMS = [
    {"entry_id": 8794696, "name": "Return of the Fox",    "manager": "Charles Smith"},
    {"entry_id": 7552664, "name": "Beef Cherki",           "manager": "Alexander Bodek"},
    {"entry_id": 5155229, "name": "Vik the Impaler",       "manager": "David Pontin"},
    {"entry_id": 5124113, "name": "Soucek Madness",        "manager": "Sam Haseltine"},
    {"entry_id": 1623042, "name": "GVG XI",                "manager": "George Georgiou"},
    {"entry_id": 6366909, "name": "HC XI II",              "manager": "Dominic Byrne"},
    {"entry_id": 5751594, "name": "I love big Győk",       "manager": "Adam Georghiou"},
    {"entry_id": 4789233, "name": "Inevitable",            "manager": "Jason Knightly"},
    {"entry_id": 5191754, "name": "Stay Classy SanDiogo", "manager": "Ed Pragnell"},
    {"entry_id": 5145283, "name": "Habibi Jeebies",        "manager": "Arun Quayum"},
    {"entry_id": 4160647, "name": "Forever20",             "manager": "Joe Wilson"},
    {"entry_id": 5150105, "name": "Gyök, Stock & Barrel", "manager": "Aron Rouse"},
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
            print(f"  Retry {attempt+1} for {path}: {e}")
            time.sleep(1)


def get_current_gw(bootstrap):
    for event in bootstrap["events"]:
        if event["is_current"]:
            return event["id"]
    for event in reversed(bootstrap["events"]):
        if event["finished"]:
            return event["id"]
    return 1


# ─── League standings ─────────────────────────────────────────────────────────

def build_standings():
    print("Fetching league standings...")
    data = fetch(f"/leagues-classic/{LEAGUE_ID}/standings/")
    out = []
    for r in data["standings"]["results"]:
        out.append({
            "rank": r["rank"],
            "rank_last": r["last_rank"],
            "entry_id": r["entry"],
            "name": r["entry_name"],
            "manager": r["player_name"],
            "total_points": r["total"],
            "gw_points": r["event_total"],
        })
    return out


# ─── Element history cache ────────────────────────────────────────────────────

_element_cache = {}


def get_element_history(player_id):
    """Returns list of per-fixture history records for a player."""
    if player_id not in _element_cache:
        data = fetch(f"/element-summary/{player_id}/")
        _element_cache[player_id] = data.get("history", [])
    return _element_cache[player_id]


def player_pts_from_gw(player_id, from_gw):
    """Total FPL points scored in all rounds >= from_gw (includes DGW fixtures)."""
    return sum(h["total_points"] for h in get_element_history(player_id) if h["round"] >= from_gw)


def player_pts_window(player_id, from_gw, to_gw=None):
    """Points scored from from_gw up to (but not including) to_gw. If to_gw is None, counts all remaining rounds."""
    history = get_element_history(player_id)
    if to_gw is None:
        return sum(h["total_points"] for h in history if h["round"] >= from_gw)
    return sum(h["total_points"] for h in history if from_gw <= h["round"] < to_gw)


def player_pts_in_gw(player_id, gw):
    """Total FPL points for a player in a specific GW (sums both fixtures in a DGW)."""
    return sum(h["total_points"] for h in get_element_history(player_id) if h["round"] == gw)


# ─── Trade differential ───────────────────────────────────────────────────────

def build_trade_differential(player_names):
    print("\nBuilding trade differential...")
    result = []
    for team in TEAMS:
        print(f"  {team['name']}...")
        all_transfers = fetch(f"/entry/{team['entry_id']}/transfers/")

        trades = []
        for t in sorted(all_transfers, key=lambda x: x["event"]):
            gw = t["event"]
            if gw <= 4:
                continue
            out_id, in_id = t["element_out"], t["element_in"]

            # player_out: points from transfer GW to now (what you gave up)
            out_pts = player_pts_from_gw(out_id, gw)

            # player_in: find the earliest subsequent transfer where this player
            # was sold out (i.e. element_out == in_id AND event > gw)
            later_sales = [t2["event"] for t2 in all_transfers
                           if t2["element_out"] == in_id and t2["event"] > gw]
            later_sold_gw = min(later_sales) if later_sales else None
            in_pts = player_pts_window(in_id, gw, later_sold_gw)
            still_in_squad = later_sold_gw is None

            diff = in_pts - out_pts
            trades.append({
                "gw": gw,
                "time": t["time"],
                "player_out": {
                    "id": out_id,
                    "name": player_names.get(out_id, f"Player {out_id}"),
                    "pts_since": out_pts,
                },
                "player_in": {
                    "id": in_id,
                    "name": player_names.get(in_id, f"Player {in_id}"),
                    "pts_while_held": in_pts,
                    "still_in_squad": still_in_squad,
                },
                "differential": diff,
            })
        net_gain = sum(t["differential"] for t in trades)
        result.append({
            "entry_id": team["entry_id"],
            "name": team["name"],
            "manager": team["manager"],
            "net_gain": net_gain,
            "trades": trades,
        })
    result.sort(key=lambda t: -t["net_gain"])
    return result


# ─── Captain points ───────────────────────────────────────────────────────────

def build_captain_points(bootstrap, current_gw, player_names):
    print("\nBuilding captain points...")
    gw_finished = {e["id"]: e["finished"] for e in bootstrap["events"]}
    result = []

    for team in TEAMS:
        print(f"  {team['name']}...")
        total_bonus = 0
        by_gw = []

        for gw in range(4, current_gw + 1):
            if not gw_finished.get(gw, False):
                continue
            picks = fetch(f"/entry/{team['entry_id']}/event/{gw}/picks/")

            # Find effective captain: pick with the highest multiplier
            # (handles auto-promoted VC if original captain played 0 mins)
            active_picks = [p for p in picks["picks"] if p["multiplier"] > 0]
            if not active_picks:
                continue
            cap_pick = max(active_picks, key=lambda p: p["multiplier"])
            cap_multiplier = cap_pick["multiplier"]
            if cap_multiplier < 2:
                # No captaincy bonus (e.g. 0-min bench boost edge case)
                continue

            cap_id = cap_pick["element"]
            raw_pts = player_pts_in_gw(cap_id, gw)
            bonus = (cap_multiplier - 1) * raw_pts  # 1x extra for C, 2x extra for TC
            total_bonus += bonus

            by_gw.append({
                "gw": gw,
                "player_id": cap_id,
                "player": player_names.get(cap_id, f"Player {cap_id}"),
                "multiplier": cap_multiplier,
                "raw_pts": raw_pts,
                "bonus": bonus,
            })

        result.append({
            "entry_id": team["entry_id"],
            "name": team["name"],
            "manager": team["manager"],
            "total_captain_bonus": total_bonus,
            "by_gw": by_gw,
        })

    result.sort(key=lambda t: -t["total_captain_bonus"])
    return result


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/league_data.json")
    args = parser.parse_args()

    print("Fetching bootstrap data...")
    bootstrap = fetch("/bootstrap-static/")
    current_gw = get_current_gw(bootstrap)
    print(f"Current GW: {current_gw}")

    player_names = {p["id"]: p["web_name"] for p in bootstrap["elements"]}

    standings   = build_standings()
    trade_diff  = build_trade_differential(player_names)
    captain_pts = build_captain_points(bootstrap, current_gw, player_names)

    league_data = {
        "metadata": {
            "league_id": LEAGUE_ID,
            "last_updated": date.today().isoformat(),
            "current_gw": current_gw,
            "season": "2025/26",
        },
        "standings": standings,
        "trade_differential": trade_diff,
        "captain_points": captain_pts,
    }

    with open(args.output, "w") as f:
        json.dump(league_data, f, indent=2)
    print(f"\nDone! Written to {args.output}")


if __name__ == "__main__":
    main()
