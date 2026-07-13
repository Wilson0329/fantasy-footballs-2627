#!/usr/bin/env python3
"""
build_podcast_data.py — weekly podcast BRIEF generator (2026/27).

Runs in CI the evening before each gameweek and prepares the *source material* for the
NotebookLM Audio Overview. It is deliberately split from publishing:

  1. Gate — only proceeds when the next FPL gameweek deadline is within --window-hours
     (i.e. "the evening before"). Otherwise it exits 0 and does nothing.
  2. Reads the already-built docs/league_data.json + docs/cup_data.json for league state.
  3. Uses Gemini with Google Search grounding to pull the key points from this week's
     Premier League manager press conferences from public sources.
  4. Fuses league state + press-conference news into a narrated brief, written to be read
     aloud by two podcast hosts, and saves it to  briefs/gw{n}.md  (the NotebookLM source).
  5. Writes  briefs/gw{n}.meta.json  with a polished public title + one-line description.

It never touches docs/podcast_data.json or any audio, and nothing under briefs/ is served
by the site. The episode only appears to league members once you run publish_episode.py
after generating the audio in NotebookLM — so the site always looks fully automated.

Env:
  GEMINI_API_KEY   required (skips gracefully if missing)
  GEMINI_MODEL     optional, default "gemini-2.5-flash"

Usage:
  python3 build_podcast_data.py                 # normal CI run (with gate)
  python3 build_podcast_data.py --gw 3          # force a specific GW (skips the gate)
  python3 build_podcast_data.py --window-hours 36
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import requests

FPL_BASE = "https://fantasy.premierleague.com/api"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})


# ─── FPL helpers ──────────────────────────────────────────────────────────────

def fetch(path):
    r = SESSION.get(f"{FPL_BASE}{path}", timeout=15)
    r.raise_for_status()
    return r.json()


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def next_gameweek(bootstrap, now):
    """Return (gw_id, deadline_dt) for the next upcoming gameweek, or (None, None)."""
    upcoming = [e for e in bootstrap["events"] if parse_dt(e["deadline_time"]) > now]
    if not upcoming:
        return None, None
    ev = min(upcoming, key=lambda e: parse_dt(e["deadline_time"]))
    return ev["id"], parse_dt(ev["deadline_time"])


# ─── League state → text ──────────────────────────────────────────────────────

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def summarise_league(league, cup):
    """Turn the pre-built JSON into a compact factual brief for the LLM. Facts only."""
    lines = []
    if league and league.get("standings"):
        st = league["standings"]
        lines.append("LEAGUE STANDINGS (rank. team — manager — total pts, last GW pts):")
        for s in st:
            lines.append(f"  {s['rank']}. {s['name']} — {s['manager']} — "
                         f"{s['total_points']} pts (GW {s.get('gw_points', '?')})")
        # Title race / relegation framing
        if len(st) >= 2:
            lines.append(f"\nTITLE RACE: {st[0]['name']} leads; "
                         f"{st[1]['name']} is {st[0]['total_points'] - st[1]['total_points']} pts behind.")
            lines.append(f"RELEGATION: bottom two are {st[-2]['name']} and {st[-1]['name']}.")

    if league and league.get("form"):
        hot = league["form"][0]
        cold = league["form"][-1]
        lines.append(f"\nFORM (last 5 GWs): hottest is {hot['name']} ({hot['form_avg']} avg); "
                     f"coldest is {cold['name']} ({cold['form_avg']} avg).")

    if cup and cup.get("knockout"):
        ko = cup["knockout"]
        fin = ko.get("final", {})
        ta = fin.get("team_a", {}).get("name", "TBD")
        tb = fin.get("team_b", {}).get("name", "TBD")
        lines.append(f"\nCUP: final is {ta} vs {tb} (status: {fin.get('status', 'upcoming')}).")

    if not lines:
        lines.append("No league data available yet — keep league references generic.")
    return "\n".join(lines)


# ─── Gemini ───────────────────────────────────────────────────────────────────

def gemini_client():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("GEMINI_API_KEY not set — skipping brief generation.", file=sys.stderr)
        sys.exit(0)
    try:
        from google import genai
    except ImportError:
        print("google-genai not installed — run: pip install google-genai", file=sys.stderr)
        sys.exit(0)
    return genai.Client(api_key=key)


def grounded_pressers(client, gw):
    """Pull key press-conference points from public sources via Google Search grounding."""
    from google.genai import types
    prompt = (
        f"Search public sources for the latest Premier League manager pre-match press "
        f"conferences ahead of Gameweek {gw} of the current season. Summarise the key "
        f"points as concise bullet points: confirmed injuries, suspensions, expected "
        f"returns, likely lineup changes, and any notable manager quotes. Focus on "
        f"information that matters for Fantasy Premier League selection. Keep it factual."
    )
    resp = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )
    return (resp.text or "").strip()


def generate_brief(client, gw, league_text, pressers_text):
    """Fuse league state + pressers into narrated podcast source notes."""
    prompt = (
        "You are writing the SOURCE NOTES for a two-host fantasy football podcast episode "
        "(the kind NotebookLM turns into an Audio Overview). The podcast previews an "
        "upcoming gameweek of a private 12-manager Fantasy Premier League among friends.\n\n"
        f"Write an engaging, information-dense brief for Gameweek {gw} of roughly 600-900 "
        "words. Cover, in a natural narrative flow: the title race, the mid-table, the "
        "relegation scrap, and the cup. Crucially, weave the real press-conference news "
        "into the league story — e.g. if a leading manager owns a player who is now "
        "doubtful, call that out as a threat to their week.\n\n"
        "Rules:\n"
        "- Use ONLY the league facts provided below. Do not invent standings, points or names.\n"
        "- The press-conference notes are real-world football news you may reference freely.\n"
        "- Refer to league managers by name to make it personal.\n"
        "- Write it to be read aloud and discussed by two hosts; lively but grounded.\n\n"
        f"=== LEAGUE FACTS ===\n{league_text}\n\n"
        f"=== PRESS-CONFERENCE NOTES ===\n{pressers_text or '(none available this week)'}\n"
    )
    resp = client.models.generate_content(model=MODEL, contents=prompt)
    return (resp.text or "").strip()


def generate_meta(client, gw, brief):
    """Produce a polished public title + one-line teaser. Returns dict."""
    prompt = (
        "Based on the podcast brief below, return ONLY a JSON object with two keys:\n"
        '  "title": a short episode title, max 60 chars (e.g. "Gameweek 3 Preview").\n'
        '  "description": a friendly 1-2 sentence teaser for league members, max 200 chars, '
        "no spoilers of specific point totals.\n"
        "Return raw JSON only, no markdown fences.\n\n"
        f"=== BRIEF ===\n{brief[:4000]}"
    )
    resp = client.models.generate_content(model=MODEL, contents=prompt)
    text = (resp.text or "").strip()
    # Strip accidental code fences
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        meta = json.loads(text)
    except ValueError:
        meta = {}
    return {
        "title": (meta.get("title") or f"Gameweek {gw} Preview")[:60],
        "description": (meta.get("description") or "").strip()[:200],
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-hours", type=float, default=30.0,
                        help="How many hours before the deadline counts as 'evening before'.")
    parser.add_argument("--gw", type=int, default=None,
                        help="Force a specific gameweek and skip the timing gate.")
    parser.add_argument("--briefs-dir", default="briefs")
    parser.add_argument("--league", default="docs/league_data.json")
    parser.add_argument("--cup", default="docs/cup_data.json")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)

    # ── Timing gate ──
    if args.gw is not None:
        gw = args.gw
        print(f"Forced GW{gw} (gate skipped).")
    else:
        print("Checking gameweek schedule...")
        bootstrap = fetch("/bootstrap-static/")
        gw, deadline = next_gameweek(bootstrap, now)
        if gw is None:
            print("No upcoming gameweek found — season may be over. Skipping.")
            return
        hours_until = (deadline - now).total_seconds() / 3600
        print(f"Next gameweek: GW{gw}, deadline {deadline.isoformat()} "
              f"({hours_until:.1f}h away).")
        if not (0 < hours_until <= args.window_hours):
            print(f"Not within the {args.window_hours}h evening-before window — skipping.")
            return
        print("Within window — building this week's brief.")

    # ── Skip if already built for this GW ──
    os.makedirs(args.briefs_dir, exist_ok=True)
    brief_path = os.path.join(args.briefs_dir, f"gw{gw}.md")
    meta_path = os.path.join(args.briefs_dir, f"gw{gw}.meta.json")
    if os.path.exists(brief_path):
        print(f"{brief_path} already exists — nothing to do.")
        return

    # ── Build ──
    league = load_json(args.league)
    cup = load_json(args.cup)
    league_text = summarise_league(league, cup)

    client = gemini_client()
    print("Fetching press-conference notes (grounded search)...")
    pressers = grounded_pressers(client, gw)
    print("Writing brief...")
    brief = generate_brief(client, gw, league_text, pressers)
    meta = generate_meta(client, gw, brief)

    with open(brief_path, "w") as f:
        f.write(f"# Fantasy Footballs — Gameweek {gw} Podcast Brief\n\n")
        f.write(f"_Generated {now.isoformat()} — source document for the NotebookLM Audio Overview._\n\n")
        f.write(brief)
        f.write("\n\n---\n\n## Press-conference notes (sources)\n\n")
        f.write(pressers or "_none available_")
        f.write("\n")

    meta["gw"] = gw
    meta["generated_at"] = now.isoformat()
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"\nDone. Brief → {brief_path}")
    print(f"       Meta  → {meta_path}")
    print("\nNext step (manual): open NotebookLM, add the brief as a source, generate the "
          "Audio Overview, download it, then run:")
    print(f"    python3 publish_episode.py --gw {gw} --audio /path/to/audio.mp3")


if __name__ == "__main__":
    main()
