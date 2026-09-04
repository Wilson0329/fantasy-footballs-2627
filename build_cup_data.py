#!/usr/bin/env python3
"""
Fantasy Footballs Cup — data builder (2026/27).
Reads league ID and cup GW schedule from Firestore (settings/site).
Outputs docs/cup_data.json.

Usage:
    python3 build_cup_data.py
    python3 build_cup_data.py --output path/to/cup_data.json
"""

import argparse
import json
import os
import time
from datetime import date

import requests

from fpl_firestore import init_firebase, load_site_settings, require_league_id, fetch_teams_from_fpl

BASE = "https://fantasy.premierleague.com/api"
CUP_NAME = "Fantasy Footballs Cup"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})

# ── Config: Firestore, or CLI overrides for local / off-season rebuilds ───────
_cli = argparse.ArgumentParser(add_help=False)
_cli.add_argument("--output", default="docs/cup_data.json")
_cli.add_argument("--league-id", type=int, default=None,
                  help="Use this league id directly and skip Firestore (for local/off-season rebuilds).")
_cli.add_argument("--season", default=None, help="Season label to stamp (used with --league-id).")
ARGS, _ = _cli.parse_known_args()

if ARGS.league_id:
    print(f"Using --league-id {ARGS.league_id} (skipping Firestore).")
    LEAGUE_ID = ARGS.league_id
    SEASON    = ARGS.season or "2026/27"
    TEAMS     = fetch_teams_from_fpl(LEAGUE_ID, SESSION)
else:
    print("Loading site settings from Firestore...")
    _db        = init_firebase()
    _settings  = load_site_settings(_db)
    LEAGUE_ID  = require_league_id(_settings)
    SEASON     = _settings.get("season", "2026/27")
    TEAMS      = fetch_teams_from_fpl(LEAGUE_ID, SESSION)
# ─────────────────────────────────────────────────────────────────────────────

# ── 2026/27 cup schedule (fixed format) ──────────────────────────────────────
# Round 1: two groups of six, round-robin — top 4 of each advance (8 teams).
# Round 2: the eight qualifiers are re-seeded into two groups of four, round-robin
#          — top 2 of each reach the semis (4 teams).
# Semis: two-legged. Final: single match.
SEEDING_GW     = 15                    # groups formed by the league table after GW15
GROUP_GWS      = [16, 18, 20, 22, 24]  # round 1 (5 rounds for 6 teams)
GROUP2_GWS     = [26, 28, 30]          # round 2 (3 rounds for 4 teams)
SEMI_GWS       = [34, 35]              # two-legged semi-finals
FINAL_GW       = 38                    # single-match final
GROUP1_ADVANCE = 4                     # top 4 of each group of six advance
GROUP2_ADVANCE = 2                     # top 2 of each group of four reach the semis

# Round-robin fixture pattern for 6 teams (positions 1–6 within group)
GROUP_ROUNDS = [
    [(1, 2), (3, 4), (5, 6)],
    [(1, 3), (2, 5), (4, 6)],
    [(1, 4), (2, 6), (3, 5)],
    [(1, 5), (2, 4), (3, 6)],
    [(1, 6), (2, 3), (4, 5)],
]
# Round-robin fixture pattern for 4 teams (positions 1–4 within group)
GROUP4_ROUNDS = [
    [(1, 2), (3, 4)],
    [(1, 3), (2, 4)],
    [(1, 4), (2, 3)],
]


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
        if event.get("finished"):
            return event["id"]
    return 1


def gw_is_finished(bootstrap, gw):
    for event in bootstrap["events"]:
        if event["id"] == gw:
            return event["finished"]
    return False


# ─── Cup score calculation ────────────────────────────────────────────────────

def cup_score_from_picks(picks_data, live_elements, first_fixture_by_team, player_team):
    """
    Cup score = sum of playing XI raw points (no captain bonus).
    - Bench boost chip: only positions 1-11 count (chip effect nullified).
    - Auto-subs (no chip): bench player gets multiplier=1 → counted; displaced starter gets 0 → excluded.
    - DGW players: only their first-fixture points count.
    """
    score = 0
    captain_pts = 0
    vice_pts = 0
    bench_boost = picks_data.get("active_chip") == "bboost"

    for pick in picks_data["picks"]:
        if bench_boost:
            if pick["position"] > 11:
                continue
        else:
            if pick["multiplier"] == 0:
                continue
        pid = pick["element"]
        el = live_elements.get(pid, {})
        explain = el.get("explain", [])

        if len(explain) > 1:
            # DGW — only count points from the first fixture
            team_id = player_team.get(pid)
            first_fix_id = first_fixture_by_team.get(team_id)
            pts = 0
            for fix_explain in explain:
                if fix_explain.get("fixture") == first_fix_id:
                    pts = sum(s.get("points", 0) for s in fix_explain.get("stats", []))
                    break
        else:
            pts = el.get("stats", {}).get("total_points", 0)

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
    if home_data["captain_pts"] > away_data["captain_pts"]:
        return "home"
    if away_data["captain_pts"] > home_data["captain_pts"]:
        return "away"
    if home_data["vice_pts"] > away_data["vice_pts"]:
        return "home"
    if away_data["vice_pts"] > home_data["vice_pts"]:
        return "away"
    return "draw"


# ─── Seeding GW standings ─────────────────────────────────────────────────────

def get_seeding_points(entry_id):
    history = fetch(f"/entry/{entry_id}/history/")
    for gw in history["current"]:
        if gw["event"] == SEEDING_GW:
            return gw["total_points"]
    # Fall back to the latest available GW if seeding GW not yet played
    if history["current"]:
        return history["current"][-1]["total_points"]
    return 0


def build_seeding_standings():
    print(f"Fetching GW{SEEDING_GW} standings for all {len(TEAMS)} teams...")
    standings = []
    for team in TEAMS:
        pts = get_seeding_points(team["entry_id"])
        standings.append({**team, "seeding_points": pts})
        print(f"  {team['name']}: {pts} pts")
    standings.sort(key=lambda t: t["seeding_points"], reverse=True)
    return standings


# ─── Live player points ───────────────────────────────────────────────────────

_live_cache = {}
_fixtures_cache = {}


def get_live_elements(gw):
    if gw not in _live_cache:
        print(f"  Fetching live data for GW{gw}...")
        data = fetch(f"/event/{gw}/live/")
        _live_cache[gw] = {el["id"]: el for el in data["elements"]}
    return _live_cache[gw]


def get_first_fixture_by_team(gw):
    if gw not in _fixtures_cache:
        fixtures = fetch(f"/fixtures/?event={gw}")
        fixtures_sorted = sorted(fixtures, key=lambda f: f.get("kickoff_time") or "")
        first = {}
        for fix in fixtures_sorted:
            for team_id in [fix["team_h"], fix["team_a"]]:
                if team_id not in first:
                    first[team_id] = fix["id"]
        _fixtures_cache[gw] = first
    return _fixtures_cache[gw]


# ─── Group stage ─────────────────────────────────────────────────────────────

def build_group(group_label, group_teams, bootstrap, rounds=GROUP_ROUNDS, gws=GROUP_GWS, advance=2):
    print(f"\nBuilding Group {group_label} ({len(group_teams)} teams, top {advance} advance)...")

    ranked = {i + 1: group_teams[i] for i in range(len(group_teams))}

    fixtures = []
    records = {t["entry_id"]: {
        "entry_id": t["entry_id"], "name": t["name"], "manager": t["manager"],
        "played": 0, "won": 0, "drawn": 0, "lost": 0,
        "points_for": 0, "points_against": 0, "cup_points": 0,
    } for t in group_teams}

    h2h = {t["entry_id"]: {t2["entry_id"]: None for t2 in group_teams} for t in group_teams}

    for round_idx, round_fixtures in enumerate(rounds):
        if round_idx >= len(gws):
            break
        gw = gws[round_idx]
        finished = gw_is_finished(bootstrap, gw)
        matches = []

        for (rank_a, rank_b) in round_fixtures:
            home_team = ranked.get(rank_a)
            away_team = ranked.get(rank_b)
            if not home_team or not away_team:
                continue

            if not finished:
                matches.append({
                    "home": {"entry_id": home_team["entry_id"], "name": home_team["name"], "score": None, "captain_pts": None, "vice_pts": None},
                    "away": {"entry_id": away_team["entry_id"], "name": away_team["name"], "score": None, "captain_pts": None, "vice_pts": None},
                    "result": None,
                    "status": "upcoming",
                })
                continue

            live = get_live_elements(gw)
            first_fix = get_first_fixture_by_team(gw)
            player_team = {p["id"]: p["team"] for p in bootstrap["elements"]}

            home_picks = fetch(f"/entry/{home_team['entry_id']}/event/{gw}/picks/")
            away_picks = fetch(f"/entry/{away_team['entry_id']}/event/{gw}/picks/")

            home_data = cup_score_from_picks(home_picks, live, first_fix, player_team)
            away_data = cup_score_from_picks(away_picks, live, first_fix, player_team)
            result = match_result(home_data, away_data)

            hid, aid = home_team["entry_id"], away_team["entry_id"]
            records[hid]["played"] += 1
            records[aid]["played"] += 1
            records[hid]["points_for"]     += home_data["score"]
            records[hid]["points_against"] += away_data["score"]
            records[aid]["points_for"]     += away_data["score"]
            records[aid]["points_against"] += home_data["score"]

            if result == "home":
                records[hid]["won"]        += 1
                records[hid]["cup_points"] += 2
                records[aid]["lost"]       += 1
                h2h[hid][aid] = "win";  h2h[aid][hid] = "loss"
            elif result == "away":
                records[aid]["won"]        += 1
                records[aid]["cup_points"] += 2
                records[hid]["lost"]       += 1
                h2h[hid][aid] = "loss"; h2h[aid][hid] = "win"
            else:
                records[hid]["drawn"]      += 1
                records[hid]["cup_points"] += 1
                records[aid]["drawn"]      += 1
                records[aid]["cup_points"] += 1
                h2h[hid][aid] = "draw"; h2h[aid][hid] = "draw"

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
        row["qualified"] = i < advance

    return {"teams": list(group_teams), "fixtures": fixtures, "standings": standings, "advance": advance}


def build_standings(records, h2h):
    def h2h_wins(a_id, others_ids):
        return sum(1 for b_id in others_ids if h2h[a_id].get(b_id) == "win")

    records.sort(key=lambda r: (-r["cup_points"], -r["points_for"]))

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
                result.extend(sorted(tied, key=lambda r: -r["points_for"]))
        else:
            tied_ids = [r["entry_id"] for r in tied]
            result.extend(sorted(tied, key=lambda r: (
                -h2h_wins(r["entry_id"], [x for x in tied_ids if x != r["entry_id"]]),
                -r["points_for"]
            )))
        i = j

    return result


# ─── Second-round group stage (re-seed the 8 qualifiers) ──────────────────────

def build_second_round(group_a_standings, group_b_standings, bootstrap):
    """
    Take the top GROUP1_ADVANCE of each first-round group, re-seed the eight
    qualifiers into two fresh groups of four (snake draw), and play a round-robin.
    Top GROUP2_ADVANCE of each second-round group reach the semi-finals.
    """
    print("\nBuilding second-round groups...")
    qualifiers = [s for s in group_a_standings if s["qualified"]] + \
                 [s for s in group_b_standings if s["qualified"]]

    # Seed the qualifiers: better first-round finish first, then cup points, then
    # points for. Snake the eight seeds into two balanced groups of four:
    #   Group C: seeds 1, 4, 5, 8    Group D: seeds 2, 3, 6, 7
    seeds = sorted(qualifiers, key=lambda r: (r["position"], -r.get("cup_points", 0), -r.get("points_for", 0)))

    def team_of(s):
        return {"entry_id": s["entry_id"], "name": s["name"], "manager": s["manager"]}

    if len(seeds) >= 8:
        c_teams = [team_of(seeds[i]) for i in (0, 3, 4, 7)]
        d_teams = [team_of(seeds[i]) for i in (1, 2, 5, 6)]
    else:
        # Round 1 not finished yet — split whatever qualifiers we have so far.
        c_teams = [team_of(s) for s in seeds[0::2]]
        d_teams = [team_of(s) for s in seeds[1::2]]

    group_c = build_group("C", c_teams, bootstrap, rounds=GROUP4_ROUNDS, gws=GROUP2_GWS, advance=GROUP2_ADVANCE)
    group_d = build_group("D", d_teams, bootstrap, rounds=GROUP4_ROUNDS, gws=GROUP2_GWS, advance=GROUP2_ADVANCE)
    return {"C": group_c, "D": group_d}


# ─── Knockout ─────────────────────────────────────────────────────────────────

def build_knockout(group_a_standings, group_b_standings, bootstrap):
    print("\nBuilding knockout stage...")

    qualifiers_a = [s for s in group_a_standings if s["qualified"]]
    qualifiers_b = [s for s in group_b_standings if s["qualified"]]

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

            live = get_live_elements(gw)
            first_fix = get_first_fixture_by_team(gw)
            player_team = {p["id"]: p["team"] for p in bootstrap["elements"]}
            picks_a = fetch(f"/entry/{team_a['entry_id']}/event/{gw}/picks/")
            picks_b = fetch(f"/entry/{team_b['entry_id']}/event/{gw}/picks/")
            data_a = cup_score_from_picks(picks_a, live, first_fix, player_team)
            data_b = cup_score_from_picks(picks_b, live, first_fix, player_team)
            agg_a += data_a["score"]
            agg_b += data_b["score"]
            legs.append({
                "gw": gw,
                "score_a": data_a["score"],
                "score_b": data_b["score"],
                "status": "complete",
            })

        all_played = all(l["status"] == "complete" for l in legs)
        winner = None
        if all_played:
            if agg_a > agg_b:
                winner = {"entry_id": team_a["entry_id"], "name": team_a["name"], "manager": team_a["manager"]}
            elif agg_b > agg_a:
                winner = {"entry_id": team_b["entry_id"], "name": team_b["name"], "manager": team_b["manager"]}
            final_teams.append(winner or {"entry_id": None, "name": "TBD", "manager": ""})
        else:
            final_teams.append({"entry_id": None, "name": "TBD", "manager": ""})

        semi_finals.append({
            "label": f"Semi-Final {idx + 1}",
            "team_a": {"entry_id": team_a["entry_id"], "name": team_a["name"], "manager": team_a["manager"]},
            "team_b": {"entry_id": team_b["entry_id"], "name": team_b["name"], "manager": team_b["manager"]},
            "legs": legs,
            "aggregate_a": agg_a,
            "aggregate_b": agg_b,
            "winner": winner,
        })

    final_team_a = final_teams[0] if len(final_teams) > 0 else {"entry_id": None, "name": "TBD", "manager": ""}
    final_team_b = final_teams[1] if len(final_teams) > 1 else {"entry_id": None, "name": "TBD", "manager": ""}
    final_finished = gw_is_finished(bootstrap, FINAL_GW)
    final_score_a, final_score_b, final_winner = None, None, None

    if final_finished and final_team_a["entry_id"] and final_team_b["entry_id"]:
        live = get_live_elements(FINAL_GW)
        first_fix = get_first_fixture_by_team(FINAL_GW)
        player_team = {p["id"]: p["team"] for p in bootstrap["elements"]}
        picks_a = fetch(f"/entry/{final_team_a['entry_id']}/event/{FINAL_GW}/picks/")
        picks_b = fetch(f"/entry/{final_team_b['entry_id']}/event/{FINAL_GW}/picks/")
        data_a = cup_score_from_picks(picks_a, live, first_fix, player_team)
        data_b = cup_score_from_picks(picks_b, live, first_fix, player_team)
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


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = ARGS

    print("Fetching bootstrap data...")
    bootstrap = fetch("/bootstrap-static/")
    cur_gw = current_gw(bootstrap)
    print(f"Current GW: {cur_gw}")
    print(f"Cup schedule — Group: {GROUP_GWS}, Semis: {SEMI_GWS}, Final: GW{FINAL_GW}")

    # Seeding GW standings → split into groups
    seeding = build_seeding_standings()
    print(f"\nGW{SEEDING_GW} standings (determines groups):")
    for i, t in enumerate(seeding):
        group = "A" if i < 6 else "B"
        print(f"  {i+1}. [{group}] {t['name']} — {t['seeding_points']} pts")

    # The groups are only seeded once GW15 is played. Until then, don't publish a
    # speculative bracket (it would shuffle every week) — write a pre-seed
    # placeholder so the site shows the format & schedule, and only flip to the
    # live bracket once real seeding exists.
    if not seeding or not gw_is_finished(bootstrap, SEEDING_GW):
        print(f"\nGW{SEEDING_GW} not finished yet — cup not seeded. Writing pre-seed "
              f"placeholder (site shows the format & schedule until then).")
        placeholder = {
            "metadata": {
                "cup_name":     CUP_NAME,
                "league_id":    LEAGUE_ID,
                "last_updated": date.today().isoformat(),
                "current_gw":   cur_gw,
                "season":       SEASON,
                "seeded":       False,
            },
        }
        with open(args.output, "w") as f:
            json.dump(placeholder, f, indent=2)
        print(f"Wrote pre-seed placeholder to {args.output}.")
        return

    group_a_teams = seeding[:6]
    group_b_teams = seeding[6:]

    # Round 1 — two groups of six, top 4 of each advance
    group_a = build_group("A", group_a_teams, bootstrap, rounds=GROUP_ROUNDS, gws=GROUP_GWS, advance=GROUP1_ADVANCE)
    group_b = build_group("B", group_b_teams, bootstrap, rounds=GROUP_ROUNDS, gws=GROUP_GWS, advance=GROUP1_ADVANCE)

    # Round 2 — re-seed the eight qualifiers into two groups of four, top 2 advance
    second = build_second_round(group_a["standings"], group_b["standings"], bootstrap)

    # Knockout — semis + final, fed by the second-round group standings
    knockout = build_knockout(second["C"]["standings"], second["D"]["standings"], bootstrap)

    cup_data = {
        "metadata": {
            "cup_name":   CUP_NAME,
            "league_id":  LEAGUE_ID,
            "last_updated": date.today().isoformat(),
            "current_gw": cur_gw,
            "season":     SEASON,
            "seeded":     True,
        },
        "groups": {
            "A": group_a,
            "B": group_b,
        },
        "groups2": {
            "C": second["C"],
            "D": second["D"],
        },
        "knockout": knockout,
    }

    with open(args.output, "w") as f:
        json.dump(cup_data, f, indent=2)

    print(f"\nDone! Written to {args.output}")
    print(f"Round 1 — Group A advance: {[s['name'] for s in group_a['standings'] if s['qualified']]}")
    print(f"Round 1 — Group B advance: {[s['name'] for s in group_b['standings'] if s['qualified']]}")
    print(f"Round 2 — Group C: {[t['name'] for t in second['C']['teams']]}")
    print(f"Round 2 — Group D: {[t['name'] for t in second['D']['teams']]}")


if __name__ == "__main__":
    main()
