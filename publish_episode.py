#!/usr/bin/env python3
"""
publish_episode.py — publish a NotebookLM episode to the site.

This is the one manual step. After you generate the Audio Overview in NotebookLM from
briefs/gw{n}.md and download it, run:

    python3 publish_episode.py --gw 3 --audio ~/Downloads/gw3.wav

The NotebookLM download is a .wav — that's fine, it's converted to mp3 automatically
(needs ffmpeg installed; `brew install ffmpeg`). A .mp3 input is copied as-is.

It will:
  1. Place the audio at docs/podcast-gw{n}.mp3 (converting from .wav if needed)
  2. Read the title + description from briefs/gw{n}.meta.json (unless you override them)
  3. Add/replace the episode in docs/podcast_data.json (newest gameweek first)

Then commit & push — the episode appears on the site's Podcast tab. Nothing about the
brief, sources or research is exposed; members only ever see the finished episode.

Options:
    --title "..."   override the episode title
    --desc  "..."   override the description
    --date  YYYY-MM-DD   override publish date (defaults to today)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date

DOCS = "docs"
DATA_PATH = os.path.join(DOCS, "podcast_data.json")


def load_meta(gw, briefs_dir="briefs"):
    path = os.path.join(briefs_dir, f"gw{gw}.meta.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def load_data():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH) as f:
            try:
                d = json.load(f)
            except ValueError:
                d = {}
    else:
        d = {}
    d.setdefault("episodes", [])
    return d


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gw", type=int, required=True)
    parser.add_argument("--audio", required=True, help="Path to the downloaded audio file.")
    parser.add_argument("--title", default=None)
    parser.add_argument("--desc", default=None)
    parser.add_argument("--date", default=None, help="Publish date YYYY-MM-DD (default: today).")
    args = parser.parse_args()

    if not os.path.exists(args.audio):
        print(f"Audio file not found: {args.audio}", file=sys.stderr)
        sys.exit(1)

    meta = load_meta(args.gw)
    title = args.title or meta.get("title") or f"Gameweek {args.gw} Preview"
    desc = args.desc if args.desc is not None else meta.get("description", "")
    published = args.date or date.today().isoformat()

    # 1. Place audio in docs/ as mp3. NotebookLM downloads .wav (~10x larger), so
    #    convert to keep the repo small and match the site's audio/mpeg player.
    os.makedirs(DOCS, exist_ok=True)
    audio_name = f"podcast-gw{args.gw}.mp3"
    dest = os.path.join(DOCS, audio_name)
    ext = os.path.splitext(args.audio)[1].lower()
    if ext == ".mp3":
        shutil.copyfile(args.audio, dest)
        print(f"Copied audio → {dest}")
    elif shutil.which("ffmpeg"):
        print(f"Converting {ext or 'audio'} → mp3 (keeps the repo small)...")
        subprocess.run(
            ["ffmpeg", "-y", "-i", args.audio, "-codec:a", "libmp3lame", "-b:a", "128k", dest],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"Converted → {dest}")
    else:
        print(f"ERROR: input is '{ext or 'unknown'}', not .mp3, and ffmpeg isn't installed.",
              file=sys.stderr)
        print("  Install it (macOS: brew install ffmpeg) then re-run, or convert to .mp3 first.",
              file=sys.stderr)
        sys.exit(1)

    # 2. Upsert the episode
    data = load_data()
    episode = {
        "gw": args.gw,
        "title": title,
        "description": desc,
        "audio_file": audio_name,
        "published_at": published,
    }
    data["episodes"] = [e for e in data["episodes"] if e.get("gw") != args.gw]
    data["episodes"].append(episode)
    data["episodes"].sort(key=lambda e: e.get("gw", 0), reverse=True)

    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Updated {DATA_PATH} ({len(data['episodes'])} episode(s)).")

    print("\nPublished:")
    print(f"  GW{args.gw} — {title}")
    print(f"  {desc}")
    print("\nNow commit & push:")
    print(f"  git add {dest} {DATA_PATH} && git commit -m 'podcast: GW{args.gw} episode' && git push")


if __name__ == "__main__":
    main()
