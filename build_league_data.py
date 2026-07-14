#!/usr/bin/env python3
"""
Fantasy Footballs League — data builder (2026/27).
Reads league ID and settings from Firestore (settings/site).
Outputs docs/league_data.json.

Usage:
    python3 build_league_data.py
    python3 build_league_data.py --output path/to/league_data.json
"""

import argparse
import json
import time
from datetime import date

import requests

from fpl_firestore import init_firebase, load_site_settings, require_league_id, fetch_teams_from_fpl

BASE = "https://fantasy.premierleague.com/api"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

# ── Config: Firestore, or CLI overrides for local / off-season rebuilds ───────
_cli = argparse.ArgumentParser(add_help=False)
_cli.add_argument("--output", default="docs/league_data.json")
_cli.add_argument("--league-id", type=int, default=None,
                  help="Use this league id directly and skip Firestore (for local/off-season rebuilds).")
_cli.add_argument("--season", default=None, help="Season label to stamp (used with --league-id).")
ARGS, _ = _cli.parse_known_args()

if ARGS.league_id:
    print(f"Using --league-id {ARGS.league_id} (skipping Firestore).")
    LEAGUE_ID = ARGS.league_id
    SEASON    = ARGS.season or "2025/26"
    TEAMS     = fetch_teams_from_fpl(LEAGUE_ID, SESSION)
else:
    print("Loading site settings from Firestore...")
    _db       = init_firebase()
    _settings = load_site_settings(_db)
    LEAGUE_ID = require_league_id(_settings)
    SEASON    = _settings.get("season", "2026/27")
    TEAMS     = fetch_teams_from_fpl(LEAGUE_ID, SESSION)
# ─────────────────────────────────────────────────────────────────────────────


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
            "rank":         r["rank"],
            "rank_last":    r["last_rank"],
            "entry_id":     r["entry"],
            "name":         r["entry_name"],
            "manager":      r["player_name"],
            "total_points": r["total"],
            "gw_points":    r["event_total"],
        })
    return out


# ─── Caches ───────────────────────────────────────────────────────────────────

_element_cache      = {}
_picks_cache        = {}
_team_history_cache = {}


def get_picks(entry_id, gw):
    key = (entry_id, gw)
    if key not in _picks_cache:
        _picks_cache[key] = fetch(f"/entry/{entry_id}/event/{gw}/picks/")
    return _picks_cache[key]


def get_team_history(entry_id):
    if entry_id not in _team_history_cache:
        data = fetch(f"/entry/{entry_id}/history/")
        _team_history_cache[entry_id] = data.get("current", [])
    return _team_history_cache[entry_id]


def get_element_history(player_id):
    if player_id not in _element_cache:
        data = fetch(f"/element-summary/{player_id}/")
        _element_cache[player_id] = data.get("history", [])
    return _element_cache[player_id]


def player_pts_from_gw(player_id, from_gw):
    return sum(h["total_points"] for h in get_element_history(player_id) if h["round"] >= from_gw)


def player_pts_window(player_id, from_gw, to_gw=None):
    history = get_element_history(player_id)
    if to_gw is None:
        return sum(h["total_points"] for h in history if h["round"] >= from_gw)
    return sum(h["total_points"] for h in history if from_gw <= h["round"] < to_gw)


def player_pts_in_gw(player_id, gw):
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
            out_pts = player_pts_from_gw(out_id, gw)
            later_sales = [t2["event"] for t2 in all_transfers
                           if t2["element_out"] == in_id and t2["event"] > gw]
            later_sold_gw = min(later_sales) if later_sales else None
            in_pts = player_pts_window(in_id, gw, later_sold_gw)
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
                    "still_in_squad": later_sold_gw is None,
                },
                "differential": diff,
            })
        result.append({
            "entry_id": team["entry_id"],
            "name":     team["name"],
            "manager":  team["manager"],
            "net_gain": sum(t["differential"] for t in trades),
            "trades":   trades,
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
            picks = get_picks(team["entry_id"], gw)
            active_picks = [p for p in picks["picks"] if p["multiplier"] > 0]
            if not active_picks:
                continue
            cap_pick = max(active_picks, key=lambda p: p["multiplier"])
            if cap_pick["multiplier"] < 2:
                continue
            cap_id = cap_pick["element"]
            raw_pts = player_pts_in_gw(cap_id, gw)
            bonus = (cap_pick["multiplier"] - 1) * raw_pts
            total_bonus += bonus
            by_gw.append({
                "gw": gw,
                "player_id": cap_id,
                "player": player_names.get(cap_id, f"Player {cap_id}"),
                "multiplier": cap_pick["multiplier"],
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


# ─── Form table ───────────────────────────────────────────────────────────────

def is_gw_finished(bootstrap, gw):
    for event in bootstrap["events"]:
        if event["id"] == gw:
            return event["finished"]
    return True


def build_form(bootstrap, current_gw):
    print("\nBuilding form table...")
    gw_live = not is_gw_finished(bootstrap, current_gw)
    result = []
    for team in TEAMS:
        history = get_team_history(team["entry_id"])
        if gw_live and history and history[-1]["event"] == current_gw:
            history = history[:-1]
        last5  = history[-5:]
        scores = [gw["points"] for gw in last5]
        avg    = round(sum(scores) / len(scores), 1) if scores else 0
        result.append({
            "entry_id":     team["entry_id"],
            "name":         team["name"],
            "manager":      team["manager"],
            "last5_scores": scores,
            "last5_gws":    [gw["event"] for gw in last5],
            "form_avg":     avg,
        })
    result.sort(key=lambda x: -x["form_avg"])
    for i, r in enumerate(result):
        r["form_rank"] = i + 1
    return result


# ─── Bench points ─────────────────────────────────────────────────────────────

def build_bench_points():
    print("\nBuilding bench points...")
    result = []
    for team in TEAMS:
        history   = get_team_history(team["entry_id"])
        total     = sum(gw["points_on_bench"] for gw in history)
        gws_played = len(history)
        result.append({
            "entry_id":          team["entry_id"],
            "name":              team["name"],
            "manager":           team["manager"],
            "total_bench_points": total,
            "avg_bench_per_gw":  round(total / gws_played, 1) if gws_played else 0,
            "by_gw": [{"gw": h["event"], "bench_pts": h["points_on_bench"]} for h in history],
        })
    result.sort(key=lambda x: -x["total_bench_points"])
    return result


# ─── Captain hit rate ─────────────────────────────────────────────────────────

def build_captain_hit_rate(bootstrap, current_gw):
    print("\nBuilding captain hit rate...")
    gw_finished = {e["id"]: e["finished"] for e in bootstrap["events"]}
    result = []
    for team in TEAMS:
        print(f"  {team['name']}...")
        hits = played = 0
        for gw in range(1, current_gw + 1):
            if not gw_finished.get(gw, False):
                continue
            try:
                picks_data = get_picks(team["entry_id"], gw)
            except Exception:
                continue
            starters = [p for p in picks_data["picks"] if p["position"] <= 11]
            captain  = next((p for p in starters if p["is_captain"]), None)
            if not captain:
                continue
            try:
                starter_pts = {p["element"]: player_pts_in_gw(p["element"], gw) for p in starters}
            except Exception:
                continue
            cap_pts = starter_pts.get(captain["element"], 0)
            max_pts = max(starter_pts.values()) if starter_pts else 0
            played += 1
            if cap_pts >= max_pts:
                hits += 1
        result.append({
            "entry_id":     team["entry_id"],
            "name":         team["name"],
            "manager":      team["manager"],
            "captain_hits": hits,
            "gws_played":   played,
            "hit_rate":     round(hits / played * 100, 1) if played else 0,
        })
    result.sort(key=lambda x: -x["hit_rate"])
    return result


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = ARGS

    print("Fetching bootstrap data...")
    bootstrap  = fetch("/bootstrap-static/")
    current_gw = get_current_gw(bootstrap)
    print(f"Current GW: {current_gw}")

    player_names = {p["id"]: p["web_name"] for p in bootstrap["elements"]}

    standings   = build_standings()
    trade_diff  = build_trade_differential(player_names)
    captain_pts = build_captain_points(bootstrap, current_gw, player_names)
    form        = build_form(bootstrap, current_gw)
    bench_pts   = build_bench_points()
    captain_hr  = build_captain_hit_rate(bootstrap, current_gw)

    league_data = {
        "metadata": {
            "league_id":    LEAGUE_ID,
            "last_updated": date.today().isoformat(),
            "current_gw":   current_gw,
            "season":       SEASON,
        },
        "standings":         standings,
        "trade_differential": trade_diff,
        "captain_points":    captain_pts,
        "form":              form,
        "bench_points":      bench_pts,
        "captain_hit_rate":  captain_hr,
    }

    with open(args.output, "w") as f:
        json.dump(league_data, f, indent=2)
    print(f"\nDone! Written to {args.output}")


if __name__ == "__main__":
    main()
