#!/usr/bin/env python3
"""
Gnistan Scoreboard Feeder — CLI
================================
Hakee kaikki päivän ottelut kentältä Taso API:sta
ja syöttää ne MySQL-cacheen scoreboardia varten.

Käyttö:
    python scoreboard_feeder.py --venue "Oulunkylä"
    python scoreboard_feeder.py --venue "Oulunkylä" --team "Gnistan"
    python scoreboard_feeder.py --venue "Oulunkylä" --interval 30 --daemon
"""

import argparse
import atexit
import os
import signal
import sys
import time
from datetime import datetime

from feeder_core import (
    VERSION,
    connect_db,
    fetch_live_score,
    find_free_port,
    find_todays_venue_matches,
    format_score,
    format_status,
    is_match_active,
    make_session,
    needs_ssh_tunnel,
    open_ssh_tunnel,
    push_venue_matches_to_db,
    update_match_from_live,
    wait_for_tunnel,
)

DISCOVERY_INTERVAL = 600   # 10 min
IDLE_SLEEP = 300           # 5 min kun kaikki pelattu


def log(msg: str, daemon: bool = False):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"  [{ts}] {msg}"
    print(line, flush=True)


def write_pid(path: str):
    with open(path, "w") as f:
        f.write(str(os.getpid()))


def remove_pid(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description="Gnistan Scoreboard Feeder")
    parser.add_argument("--venue",    default="Oulunkylä")
    parser.add_argument("--team",     default="",
                        help="Valinnainen joukkuesuodatin (tyhjä = kaikki)")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--daemon",   action="store_true",
                        help="Daemon-moodi: ei terminaali-animaatioita")
    parser.add_argument("--pidfile",  default="feeder.pid")
    args = parser.parse_args()

    # PID-tiedosto
    pidfile = os.path.abspath(args.pidfile)
    write_pid(pidfile)
    atexit.register(remove_pid, pidfile)

    if not args.daemon:
        print("╔══════════════════════════════════════════════════════╗")
        print(f"║        GNISTAN SCOREBOARD FEEDER  v{VERSION}              ║")
        print("╚══════════════════════════════════════════════════════╝")
        print()
        print(f"  Kenttä   : {args.venue}")
        if args.team:
            print(f"  Joukkue  : {args.team}")
        print(f"  Päivitys : {args.interval}s")
        print()

    # DB-yhteys
    tunnel_proc = None
    if needs_ssh_tunnel():
        local_port = find_free_port()
        log(f"Avataan SSH-tunneli (portti {local_port})...")
        tunnel_proc = open_ssh_tunnel(local_port)
        if not wait_for_tunnel(local_port):
            log("VIRHE: SSH-tunneli ei auennut.")
            sys.exit(1)
        log("SSH-tunneli auki.")
        db_host, db_port = "127.0.0.1", local_port
    else:
        log("Suora MySQL-yhteys.")
        db_host, db_port = None, None

    try:
        conn = connect_db(db_host, db_port) if db_host else connect_db()
        log("MySQL-yhteys OK.")
    except Exception as e:
        log(f"VIRHE: MySQL-yhteys epäonnistui: {e}")
        if tunnel_proc:
            tunnel_proc.terminate()
        sys.exit(1)

    session = make_session()

    # Discovery: hae kaikki päivän ottelut kentältä
    matches_meta = find_todays_venue_matches(
        session, args.venue, args.team, log_fn=log,
    )

    if not matches_meta:
        log("Ei otteluita tänään näillä hakuehdoilla.")
        conn.close()
        if tunnel_proc:
            tunnel_proc.terminate()
        sys.exit(0)

    # Tulosta löydetyt
    if not args.daemon:
        print()
        print("─" * 56)
        for match, cat_id, league in matches_meta:
            home = match.get("team_A_name", "?")
            away = match.get("team_B_name", "?")
            t = (match.get("time") or "")[:5]
            print(f"  {t}  {home} vs {away}  |  {league}")
        print("─" * 56)
        print()

    # Kirjoita heti cacheen
    push_venue_matches_to_db(conn, args.venue, matches_meta)
    log("Ottelut kirjoitettu cacheen.")

    # Pääsilmukka
    last_discovery = time.time()

    def graceful_exit(sig, frame):
        log("Signaali vastaanotettu, lopetetaan.")
        conn.close()
        if tunnel_proc:
            tunnel_proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, graceful_exit)

    try:
        while True:
            # Re-discovery 10 min välein
            if time.time() - last_discovery > DISCOVERY_INTERVAL:
                log("Re-discovery...")
                new_meta = find_todays_venue_matches(
                    session, args.venue, args.team, log_fn=log,
                )
                if new_meta:
                    # Säilytä vanhojen otteluiden status_changed_at
                    old_by_id = {
                        str(m.get("match_id")): m
                        for m, _, _ in matches_meta
                    }
                    for match, _, _ in new_meta:
                        mid = str(match.get("match_id"))
                        if mid in old_by_id and "status_changed_at" in old_by_id[mid]:
                            match["status_changed_at"] = old_by_id[mid]["status_changed_at"]
                    matches_meta = new_meta
                last_discovery = time.time()

            # Pollaa aktiiviset ottelut
            any_active = False
            for match, cat_id, league in matches_meta:
                if not is_match_active(match):
                    continue
                any_active = True
                match_id = str(match.get("match_id", ""))
                live = fetch_live_score(session, match_id, log_fn=log)
                if live:
                    update_match_from_live(match, live)

            # Kirjoita kaikki cacheen
            try:
                push_venue_matches_to_db(conn, args.venue, matches_meta)
            except Exception as e:
                log(f"DB-virhe: {e}")
                try:
                    conn.ping(reconnect=True)
                except Exception:
                    pass

            # Logita tilanne
            for match, cat_id, league in matches_meta:
                home = match.get("team_A_name", "?")
                away = match.get("team_B_name", "?")
                score = format_score(match)
                status = format_status(match)
                log(f"{home} {score} {away}  |  {status}")

            # Kaikki pelattu?
            all_done = all(
                (m.get("status") or "").lower() == "played"
                for m, _, _ in matches_meta
            )

            if all_done:
                # Tarkista onko kello yli 23
                if datetime.now().hour >= 23:
                    log("Kaikki ottelut pelattu, lopetetaan.")
                    break
                log(f"Kaikki pelattu, odotetaan {IDLE_SLEEP}s (re-discovery)...")
                for _ in range(IDLE_SLEEP):
                    time.sleep(1)
                last_discovery = 0  # pakota re-discovery
                continue

            # Odota seuraavaan polliin
            if args.daemon:
                time.sleep(args.interval)
            else:
                for remaining in range(args.interval, 0, -1):
                    print(f"\r  Seuraava päivitys {remaining:3d}s ...",
                          end="", flush=True)
                    time.sleep(1)
                print("\r" + " " * 40 + "\r", end="", flush=True)

    except KeyboardInterrupt:
        log("Keskeytetty (Ctrl+C).")

    finally:
        conn.close()
        if tunnel_proc:
            tunnel_proc.terminate()
        log("Yhteydet suljettu.")


if __name__ == "__main__":
    main()
