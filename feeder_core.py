"""
Scoreboard Feeder — jaettu logiikka
====================================
API-kutsut, SSH-tunneli, tietokanta ja sarjamäärittelyt.
Tukee usean ottelun seurantaa yhdellä kentällä.
"""

import json
import os
import platform
import re
import socket
import subprocess
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import pymysql
import requests
from dotenv import load_dotenv

# Lataa .env projektin juuresta
load_dotenv(Path(__file__).parent / ".env")

VERSION = "3.0"

# ─── Asetukset (.env) ────────────────────────────────────────────────────────

TASO_API_KEY = os.environ.get("TASO_API_KEY", "")

DB_HOST     = os.environ.get("DB_HOST", "localhost")
DB_PORT     = int(os.environ.get("DB_PORT", "3306"))
DB_USER     = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME     = os.environ.get("DB_NAME", "")

SSH_HOST = os.environ.get("SSH_HOST", "")
SSH_USER = os.environ.get("SSH_USER", "")
SSH_PORT = 22

# ─── API-asetukset ───────────────────────────────────────────────────────────

BASE_URL = "https://spl.torneopal.net/taso/rest"


def _build_api_headers() -> dict:
    return {
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9,fi;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://tulospalvelu.palloliitto.fi/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/144.0.0.0 Safari/537.36"
        ),
    }


SCOREBOARD_URL = "http://www.minormusic.fi/gsoft/scoreboard"

# ─── Kenttä (venue_id) ────────────────────────────────────────────────────────

# Oulunkylä 1 TN = venue_id 325 (Taso API getVenues)
VENUE_ID = os.environ.get("VENUE_ID", "325")

# ─── Apufunktiot ──────────────────────────────────────────────────────────────


def normalize_venue_slug(venue: str) -> str:
    """Muuntaa kenttänimen URL-turvalliseksi slugiksi.
    'Oulunkylä TN 1' → 'oulunkyla-tn-1'"""
    s = unicodedata.normalize("NFKD", venue).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# ─── SSH-tunneli (valinnainen) ────────────────────────────────────────────────


def needs_ssh_tunnel() -> bool:
    return bool(SSH_HOST)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def open_ssh_tunnel(local_port: int) -> subprocess.Popen:
    cmd = [
        "ssh", "-N",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=5",
        "-o", "ExitOnForwardFailure=yes",
        "-L", f"{local_port}:localhost:3306",
        f"{SSH_USER}@{SSH_HOST}",
    ]
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
    )


def wait_for_tunnel(local_port: int, timeout: int = 15) -> bool:
    for _ in range(timeout):
        time.sleep(1)
        if is_port_open(local_port):
            return True
    return False


# ─── MySQL ────────────────────────────────────────────────────────────────────


def connect_db(host: str = DB_HOST, port: int = DB_PORT) -> pymysql.Connection:
    return pymysql.connect(
        host=host, port=port,
        user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
        charset="utf8mb4", autocommit=False, connect_timeout=10,
    )


def db_upsert(conn, cache_key: str, data, data_type: str,
              category_id: str | None = None, ttl_hours: int = 3,
              auto_commit: bool = True):
    season = str(date.today().year)
    json_str = json.dumps(data, ensure_ascii=False)
    expires_at = datetime.now() + timedelta(hours=ttl_hours)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO api_cache
                (cache_key, data_type, category_id, season,
                 json_data, fetched_at, expires_at, is_historical)
            VALUES (%s, %s, %s, %s, %s, NOW(), %s, 0)
            ON DUPLICATE KEY UPDATE
                json_data     = VALUES(json_data),
                fetched_at    = NOW(),
                expires_at    = VALUES(expires_at),
                is_historical = 0
            """,
            (cache_key, data_type, category_id, season, json_str, expires_at),
        )
    if auto_commit:
        conn.commit()


def push_venue_matches_to_db(conn, venue: str,
                              matches_with_meta: list[tuple[dict, str, str]]):
    """Kirjoittaa kaikki kentän ottelut DB:hen.
    matches_with_meta: [(match_dict, cat_id, league_name), ...]
    """
    slug = normalize_venue_slug(venue)
    now_iso = datetime.now().isoformat(timespec="seconds")

    # Aggregaatti — kaikki ottelut yhdessä rivissä
    venue_data = {
        "venue": venue,
        "date": date.today().strftime("%Y-%m-%d"),
        "updated_at": now_iso,
        "matches": [],
    }

    for match, cat_id, league in matches_with_meta:
        match_id = str(match.get("match_id", ""))

        # Yksittäinen ottelu
        db_upsert(conn, f"match_{match_id}", match, "match_detail",
                  ttl_hours=3, auto_commit=False)

        # Lisää aggregaattiin
        venue_data["matches"].append({
            "match_id": match_id,
            "cat_id": cat_id,
            "league_name": league,
            "time": match.get("time", ""),
            "status": match.get("status", ""),
            "status_changed_at": match.get("status_changed_at", ""),
            "team_A_name": match.get("team_A_name", ""),
            "team_B_name": match.get("team_B_name", ""),
            "team_A_id": match.get("team_A_id", ""),
            "team_B_id": match.get("team_B_id", ""),
            "fs_A": match.get("fs_A"),
            "fs_B": match.get("fs_B"),
            "live_timer_on": match.get("live_timer_on"),
            "live_period": match.get("live_period"),
            "live_time_mmss": match.get("live_time_mmss"),
            "period_min": match.get("period_min"),
            "goals": match.get("goals", []),
            "club_A_crest": match.get("club_A_crest", ""),
            "club_B_crest": match.get("club_B_crest", ""),
            "venue_name": match.get("venue_name", ""),
        })

    # Järjestä ajan mukaan
    venue_data["matches"].sort(key=lambda m: m.get("time", ""))

    db_upsert(conn, f"venue_matches_{slug}", venue_data, "venue_matches",
              ttl_hours=6, auto_commit=False)
    conn.commit()


# ─── API ──────────────────────────────────────────────────────────────────────

_last_api_call = 0.0
_api_lock = threading.Lock()


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(_build_api_headers())
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=5, pool_maxsize=10, max_retries=2,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def api_get(session: requests.Session, endpoint: str, params: dict,
            log_fn=None) -> dict | None:
    global _last_api_call
    # Laske odotusaika lockin alla, mutta nuku sen ulkopuolella
    with _api_lock:
        wait = max(0, 0.6 - (time.time() - _last_api_call))
    if wait > 0:
        time.sleep(wait)
    with _api_lock:
        try:
            req_params = {**params}
            if TASO_API_KEY:
                req_params["api_key"] = TASO_API_KEY
            r = session.get(f"{BASE_URL}/{endpoint}", params=req_params, timeout=15)
            _last_api_call = time.time()
            r.raise_for_status()
            return r.json()
        except Exception as e:
            _last_api_call = time.time()
            if log_fn:
                log_fn(f"API-virhe ({endpoint}): {e}")
            return None


# ─── Otteluhaku ───────────────────────────────────────────────────────────────


def find_todays_venue_matches(session: requests.Session, venue: str,
                               team: str = "", log_fn=None,
                               stop_check=None) -> list[tuple[dict, str, str]]:
    """Hakee KAIKKI tänään kentällä pelattavat ottelut yhdellä API-kutsulla.
    Käyttää venue_id-parametria → 1 kutsu vs. vanhat 33+ kutsua.
    Palauttaa listan: [(match_dict, cat_id, league_name), ...]
    """
    today = date.today().strftime("%Y-%m-%d")
    if log_fn:
        filter_str = f"{venue} (venue_id={VENUE_ID})"
        if team:
            filter_str += f" / {team}"
        log_fn(f"Haetaan otteluita {today} | {filter_str} ...")

    data = api_get(session, "getMatches",
                   {"venue_id": VENUE_ID, "date": today},
                   log_fn=log_fn)
    if not data:
        return []

    matches = data if isinstance(data, list) else (
        data.get("matches") or data.get("data") or data.get("results") or []
    )

    found = []
    for m in matches:
        if stop_check and stop_check():
            break

        # Joukkuesuodatus (valinnainen)
        if team and team.lower() not in (
            (m.get("team_A_name") or "") + " " + (m.get("team_B_name") or "")
        ).lower():
            continue

        cat_id = f"{m.get('category_id', '')}!{m.get('competition_id', '')}"
        league = m.get("category_name") or cat_id
        found.append((m, cat_id, league))

        if log_fn:
            log_fn(f"  Löytyi: {m.get('team_A_name')} vs {m.get('team_B_name')} | {league}")

    # Järjestä alkuajan mukaan
    found.sort(key=lambda x: x[0].get("time", ""))

    if log_fn:
        log_fn(f"Yhteensä {len(found)} ottelua ({len(matches)} API:sta, {len(found)} suodatuksen jälkeen).")

    return found


def fetch_live_score(session: requests.Session, match_id: str,
                     log_fn=None) -> dict | None:
    data = api_get(session, "getMatch", {"match_id": match_id}, log_fn=log_fn)
    if not data:
        return None
    if isinstance(data, dict) and "match" in data:
        return data["match"]
    return data


def update_match_from_live(match: dict, live: dict):
    """Päivittää match-dictiä live-datalla. Seuraa status-muutoksia."""
    old_status = (match.get("status") or "").lower()

    if live.get("fs_A") is not None:
        match["fs_A"] = live["fs_A"]
        match["fs_B"] = live.get("fs_B")
    if live.get("status"):
        match["status"] = live["status"]
    for field in ["live_timer_on", "live_period", "live_time_mmss",
                   "live_A", "live_B", "period_min",
                   "goals", "team_A_id", "team_B_id",
                   "club_A_crest", "club_B_crest"]:
        if live.get(field) is not None:
            match[field] = live[field]

    # Seuraa milloin status muuttui
    new_status = (match.get("status") or "").lower()
    if new_status != old_status:
        match["status_changed_at"] = datetime.now().isoformat(timespec="seconds")


def is_match_active(match: dict) -> bool:
    """Onko ottelu live tai alkamassa lähiaikoina?"""
    status = (match.get("status") or "").lower()
    if status in ("live", "playing"):
        return True
    if status == "played":
        return False
    # Tulossa — tarkista onko alkuaika 30 min sisällä
    match_time = match.get("time", "")
    if match_time:
        try:
            kick = datetime.combine(date.today(),
                                     datetime.strptime(match_time[:5], "%H:%M").time())
            return (kick - datetime.now()).total_seconds() < 1800
        except ValueError:
            pass
    return True  # tuntematon status → aktiivinen


# ─── Muotoilu ─────────────────────────────────────────────────────────────────


def format_score(match: dict) -> str:
    a, b = match.get("fs_A"), match.get("fs_B")
    return f"{a} : {b}" if a is not None and b is not None else "- : -"


def format_status(match: dict) -> str:
    s = (match.get("status") or "").lower()
    if s == "played":
        return "LOPPU"
    if s in ("live", "playing"):
        return "LIVE"
    t = (match.get("time") or "")[:5]
    return t or "TULOSSA"
