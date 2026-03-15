"""
Scoreboard Feeder — jaettu logiikka
====================================
API-kutsut, SSH-tunneli, tietokanta ja sarjamäärittelyt.
"""

import json
import os
import platform
import socket
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pymysql
import requests
from dotenv import load_dotenv

# Lataa .env projektin juuresta
load_dotenv(Path(__file__).parent / ".env")

VERSION = "2.0"

# ─── Asetukset (.env) ────────────────────────────────────────────────────────

SSH_HOST = os.environ.get("SSH_HOST", "whm57.louhi.net")
SSH_USER = os.environ.get("SSH_USER", "minormusic")
SSH_PORT = 22

DB_USER     = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME     = os.environ.get("DB_NAME", "")

# ─── API-asetukset ───────────────────────────────────────────────────────────

BASE_URL = "https://spl.torneopal.net/taso/rest"

API_HEADERS = {
    "Accept": "json/n9tnjq45uuccbe8nbfy6q7ggmreqntvs",
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

# ─── Sarjat 2026 ─────────────────────────────────────────────────────────────

CATEGORIES_2026 = [
    # SPL valtakunnalliset (spljp26)
    "MSC!spljp26",    "NSC!spljp26",    "KC!spljp26",
    "VL!spljp26",     "NL!spljp26",     "M1L!spljp26",
    "M1!spljp26",     "N1!spljp26",     "M2!spljp26",     "N2!spljp26",
    "P21SM!spljp26",  "P211!spljp26",
    "P18SM!spljp26",  "P18SMK!spljp26", "P181!spljp26",
    "T18SM!spljp26",  "T18SMK!spljp26", "T181!spljp26",
    # Etelä alue (etejp26)
    "M3!etejp26",
    "P121!etejp26", "P122!etejp26", "P12LE!etejp26",
    "P131!etejp26", "P132!etejp26", "P13LE!etejp26",
    "P141!etejp26", "P142!etejp26", "P14LE!etejp26",
    "P151!etejp26", "P152!etejp26", "P15LE!etejp26",
    "P161!etejp26", "P162!etejp26", "P16LE!etejp26",
]

# ─── SSH-tunneli ──────────────────────────────────────────────────────────────


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
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


def wait_for_tunnel(local_port: int, timeout: int = 15) -> bool:
    for _ in range(timeout):
        time.sleep(1)
        if is_port_open(local_port):
            return True
    return False


# ─── MySQL ────────────────────────────────────────────────────────────────────


def connect_db(local_port: int) -> pymysql.Connection:
    return pymysql.connect(
        host="127.0.0.1",
        port=local_port,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=10,
    )


def db_upsert(conn, cache_key: str, data, data_type: str,
              category_id: str | None = None, ttl_hours: int = 3):
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
            (cache_key, data_type, category_id, "2026", json_str, expires_at),
        )
    conn.commit()


def push_match_to_db(conn, match: dict, cat_id: str, league: str):
    match_id = str(match.get("match_id", ""))
    db_upsert(conn, f"match_{match_id}",       match,   "match_detail", ttl_hours=3)
    db_upsert(conn, f"matches_basic_{cat_id}",  [match], "matches", category_id=cat_id, ttl_hours=3)
    db_upsert(conn, f"league_for_match_{match_id}",
              {"league_name": league}, "league_meta", ttl_hours=3)


# ─── API ──────────────────────────────────────────────────────────────────────

_last_api_call = 0.0


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(API_HEADERS)
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=5, pool_maxsize=10, max_retries=2,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def api_get(session: requests.Session, endpoint: str, params: dict,
            log_fn=None) -> dict | None:
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < 0.6:
        time.sleep(0.6 - elapsed)
    try:
        r = session.get(f"{BASE_URL}/{endpoint}", params=params, timeout=15)
        _last_api_call = time.time()
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _last_api_call = time.time()
        if log_fn:
            log_fn(f"API-virhe ({endpoint}): {e}")
        return None


def parse_cat(cat_id: str) -> tuple[str, str]:
    if "!" in cat_id:
        c, comp = cat_id.split("!", 1)
        return c, comp
    return cat_id, "etejp26"


# ─── Otteluhaku ───────────────────────────────────────────────────────────────


def find_todays_match(session: requests.Session, venue: str, team: str,
                      log_fn=None, stop_check=None):
    """Käy sarjat läpi ja palauttaa (match, cat_id, league) tai (None, '', '')."""
    today = date.today().strftime("%Y-%m-%d")
    if log_fn:
        log_fn(f"Etsitään ottelua {today} | {venue} | {team} ...")

    for cat_id in CATEGORIES_2026:
        if stop_check and stop_check():
            return None, "", ""

        cat, comp = parse_cat(cat_id)
        data = api_get(session, "getMatches",
                       {"competition_id": comp, "category_id": cat},
                       log_fn=log_fn)
        if not data:
            continue

        matches = data if isinstance(data, list) else (
            data.get("matches") or data.get("data") or data.get("results") or []
        )

        for m in matches:
            if (
                m.get("date") == today
                and venue.lower() in (m.get("venue_name") or "").lower()
                and team.lower() in (
                    (m.get("team_A_name") or "") + " " + (m.get("team_B_name") or "")
                ).lower()
            ):
                league = ""
                cat_info = api_get(session, "getCategory",
                                   {"competition_id": comp, "category_id": cat},
                                   log_fn=log_fn)
                if cat_info and isinstance(cat_info, dict):
                    league = (cat_info.get("category") or {}).get("category_name", cat_id)
                if log_fn:
                    log_fn(f"Löytyi: {m.get('team_A_name')} vs {m.get('team_B_name')} | {league}")
                return m, cat_id, league

    return None, "", ""


def fetch_live_score(session: requests.Session, match_id: str,
                     log_fn=None) -> dict | None:
    data = api_get(session, "getMatch", {"match_id": match_id}, log_fn=log_fn)
    if not data:
        return None
    if isinstance(data, dict) and "match" in data:
        return data["match"]
    return data


def update_match_from_live(match: dict, live: dict):
    """Päivittää match-dictiä live-datalla."""
    if live.get("fs_A") is not None:
        match["fs_A"] = live["fs_A"]
        match["fs_B"] = live.get("fs_B")
    if live.get("status"):
        match["status"] = live["status"]
    for field in ["live_timer_on", "live_period", "live_time_mmss",
                   "live_A", "live_B", "period_min",
                   "goals", "team_A_id", "team_B_id"]:
        if live.get(field) is not None:
            match[field] = live[field]


# ─── Muotoilu ─────────────────────────────────────────────────────────────────


def format_score(match: dict) -> str:
    a, b = match.get("fs_A"), match.get("fs_B")
    return f"{a} : {b}" if a is not None and b is not None else "- : -"


def format_status(match: dict) -> str:
    s = (match.get("status") or "").lower()
    if s == "played":
        return "LOPPU"
    if s in ("live", "playing"):
        return "● LIVE"
    t = (match.get("time") or "")[:5]
    return t or "TULOSSA"
