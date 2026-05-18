#!/usr/bin/env python3
"""
fpl_firestore.py
Shared helper — reads site settings from Firestore for use in all build scripts.
Expects FIREBASE_SERVICE_ACCOUNT env var containing the service account JSON.
"""

import json
import os
import sys

import firebase_admin
from firebase_admin import credentials, firestore as fs_client
import requests

FPL_BASE = "https://fantasy.premierleague.com/api"

DEFAULT_CUP_GWS   = [18, 20, 22, 24, 26, 31, 33, 38]
DEFAULT_GROUP_GWS = [18, 20, 22, 24, 26]
DEFAULT_SEMI_GWS  = [31, 33]
DEFAULT_FINAL_GW  = 38


def init_firebase():
    """Initialise Firebase Admin SDK from FIREBASE_SERVICE_ACCOUNT env var."""
    svc = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not svc:
        print("ERROR: FIREBASE_SERVICE_ACCOUNT env var not set.", file=sys.stderr)
        sys.exit(1)
    if not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(svc))
        firebase_admin.initialize_app(cred)
    return fs_client.client()


def load_site_settings(db):
    """
    Load settings/site from Firestore.
    Returns the settings dict, or {} if the document doesn't exist yet.
    """
    doc = db.collection("settings").doc("site").get()
    return doc.to_dict() if doc.exists else {}


def require_league_id(settings):
    """
    Extract and return the league ID as an int.
    Exits with code 0 (not an error — just nothing to do yet) if unset.
    """
    lid = settings.get("leagueId")
    if not lid:
        print("ℹ️  No leagueId in Firestore settings/site — skipping build until league is set up.")
        sys.exit(0)
    return int(lid)


def parse_cup_gws(settings):
    """
    Parse cup GW schedule from settings.
    Returns (group_gws, semi_gws, final_gw).
    Falls back to defaults if not configured.
    """
    gws = settings.get("cupGWs", DEFAULT_CUP_GWS)
    group_gws = gws[:5]  if len(gws) >= 5 else DEFAULT_GROUP_GWS
    semi_gws  = gws[5:7] if len(gws) >= 7 else DEFAULT_SEMI_GWS
    final_gw  = gws[7]   if len(gws) >= 8 else DEFAULT_FINAL_GW
    return group_gws, semi_gws, final_gw


def fetch_teams_from_fpl(league_id, session):
    """
    Fetch the 12-team list dynamically from the FPL league standings API.
    Returns list of dicts: [{entry_id, name, manager}, ...].
    """
    print(f"  Fetching teams from FPL league {league_id}...")
    resp = session.get(
        f"{FPL_BASE}/leagues-classic/{league_id}/standings/",
        timeout=15
    )
    resp.raise_for_status()
    data    = resp.json()
    results = data["standings"]["results"][:12]
    teams   = [
        {
            "entry_id": r["entry"],
            "name":     r["entry_name"],
            "manager":  r["player_name"],
        }
        for r in results
    ]
    print(f"  Loaded {len(teams)} teams.")
    return teams
