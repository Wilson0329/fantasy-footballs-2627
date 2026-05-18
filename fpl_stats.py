#!/usr/bin/env python3
"""
FPL Stats — pulls public data about your Fantasy Premier League team.
Usage:
    python fpl_stats.py --team-id 12345
    FPL_TEAM_ID=12345 python fpl_stats.py
"""

import argparse
import os
import sys

import requests

BASE_URL = "https://fantasy.premierleague.com/api"

POSITION_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
})


def fetch(path):
    url = f"{BASE_URL}{path}"
    resp = SESSION.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_bootstrap():
    """Cached bootstrap data — players, teams, current GW."""
    return fetch("/bootstrap-static/")


def current_gw(bootstrap):
    for event in bootstrap["events"]:
        if event["is_current"]:
            return event
    # fallback: last finished
    for event in reversed(bootstrap["events"]):
        if event["is_finished"]:
            return event
    return bootstrap["events"][0]


def player_map(bootstrap):
    return {p["id"]: p for p in bootstrap["elements"]}


def team_map(bootstrap):
    return {t["id"]: t["short_name"] for t in bootstrap["teams"]}


def print_header(text):
    print(f"\n{'─' * 50}")
    print(f"  {text}")
    print(f"{'─' * 50}")


def show_manager(entry):
    print_header("MANAGER")
    print(f"  Team:     {entry['name']}")
    print(f"  Manager:  {entry['player_first_name']} {entry['player_last_name']}")
    print(f"  Points:   {entry['summary_overall_points']:,}")
    print(f"  Rank:     {entry['summary_overall_rank']:,}")
    print(f"  GW pts:   {entry['summary_event_points']}")
    print(f"  GW rank:  {entry['summary_event_rank']:,}" if entry['summary_event_rank'] else "  GW rank:  —")


def show_leagues(entry):
    print_header("MINI-LEAGUES")
    leagues = entry.get("leagues", {})
    classic = leagues.get("classic", [])
    if not classic:
        print("  No classic leagues found.")
        return
    print(f"  {'League':<35} {'Rank':>7}  {'Last GW':>7}")
    print(f"  {'─'*35} {'─'*7}  {'─'*7}")
    for league in classic:
        name = league["name"][:34]
        rank = f"{league['entry_rank']:,}"
        last = f"{league['entry_last_rank']:,}" if league.get("entry_last_rank") else "—"
        print(f"  {name:<35} {rank:>7}  {last:>7}")


def show_squad(picks, players, teams, gw_name):
    print_header(f"SQUAD — {gw_name}")
    print(f"  {'#':<2}  {'Player':<25} {'Pos':<4} {'Club':<5} {'£':>5}  {'Pts':>4}  {'Sel%':>5}")
    print(f"  {'─'*2}  {'─'*25} {'─'*4} {'─'*5} {'─'*5}  {'─'*4}  {'─'*5}")

    for i, pick in enumerate(picks):
        if i == 11:
            print(f"  {'— BENCH —':}")

        pid = pick["element"]
        p = players.get(pid, {})
        name = p.get("web_name", f"ID:{pid}")[:24]
        pos = POSITION_MAP.get(p.get("element_type", 0), "?")
        club = teams.get(p.get("team", 0), "?")
        cost = p.get("now_cost", 0) / 10
        pts = p.get("event_points", 0)
        sel = p.get("selected_by_percent", "?")
        cap = " (C)" if pick.get("is_captain") else (" (V)" if pick.get("is_vice_captain") else "")

        print(f"  {i+1:<2}  {name+cap:<25} {pos:<4} {club:<5} {cost:>5.1f}  {pts:>4}  {sel:>5}%")


def show_history(history):
    print_header("RECENT GAMEWEEKS")
    gw_history = history.get("current", [])
    if not gw_history:
        print("  No GW history available.")
        return

    print(f"  {'GW':<4} {'Pts':>5}  {'Rank':>10}  {'Value':>7}  {'Chip':<10}")
    print(f"  {'─'*4} {'─'*5}  {'─'*10}  {'─'*7}  {'─'*10}")
    for gw in gw_history[-10:]:  # last 10 GWs
        chip = gw.get("active_chip") or "—"
        val = gw.get("value", 0) / 10
        pts = gw.get("points", 0)
        rank = f"{gw['overall_rank']:,}" if gw.get("overall_rank") else "—"
        print(f"  {gw['event']:<4} {pts:>5}  {rank:>10}  {val:>7.1f}  {chip:<10}")


def main():
    parser = argparse.ArgumentParser(description="FPL Stats — no login required")
    parser.add_argument("--team-id", type=int, default=None,
                        help="Your FPL team ID (visible in your FPL URL)")
    args = parser.parse_args()

    team_id = args.team_id or int(os.environ.get("FPL_TEAM_ID", 0))
    if not team_id:
        print("Error: provide --team-id or set FPL_TEAM_ID env var.")
        print("  Your team ID is in the URL: fantasy.premierleague.com/entry/XXXXX/")
        sys.exit(1)

    print(f"\nFetching FPL data for team {team_id}...")

    try:
        bootstrap = get_bootstrap()
        entry = fetch(f"/entry/{team_id}/")
        history = fetch(f"/entry/{team_id}/history/")
        gw = current_gw(bootstrap)
        picks_data = fetch(f"/entry/{team_id}/event/{gw['id']}/picks/")
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            print(f"Error: team ID {team_id} not found. Check your FPL URL.")
        else:
            print(f"API error: {e}")
        sys.exit(1)
    except requests.RequestException as e:
        print(f"Connection error: {e}")
        sys.exit(1)

    players = player_map(bootstrap)
    teams = team_map(bootstrap)

    show_manager(entry)
    show_leagues(entry)
    show_history(history)
    show_squad(picks_data["picks"], players, teams, gw["name"])

    print(f"\n{'─' * 50}\n")


if __name__ == "__main__":
    main()
