#!/usr/bin/env python3
"""
Gnistan Scoreboard Feeder — CLI
================================
Hakee tänään pelattavan ottelun tuloksen torneopal.netistä
ja syöttää sen Louhen MySQL-cacheen.

Käyttö:
    python scoreboard_feeder.py
    python scoreboard_feeder.py --venue "Töölö" --team "HJK"
    python scoreboard_feeder.py --interval 30
"""

import argparse
import sys
import time
from datetime import datetime

from feeder_core import (
    VERSION,
    connect_db,
    fetch_live_score,
    find_free_port,
    find_todays_match,
    format_score,
    format_status,
    make_session,
    open_ssh_tunnel,
    push_match_to_db,
    update_match_from_live,
    wait_for_tunnel,
)


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}")


def main():
    parser = argparse.ArgumentParser(description="Gnistan Scoreboard Feeder")
    parser.add_argument("--venue",    default="Oulunkylä")
    parser.add_argument("--team",     default="Gnistan")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════════╗")
    print(f"║        GNISTAN SCOREBOARD FEEDER  v{VERSION}              ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()
    print(f"  Kenttä   : {args.venue}  (osittainen haku)")
    print(f"  Joukkue  : {args.team}")
    print(f"  Päivitys : {args.interval}s")
    print()

    # SSH-tunneli
    local_port = find_free_port()
    log(f"Avataan SSH-tunneli (paikallinen portti {local_port})...")
    tunnel_proc = open_ssh_tunnel(local_port)

    if not wait_for_tunnel(local_port):
        print("\n  VIRHE: SSH-tunneli ei auennut 15 sekunnissa.")
        print("  Tarkista SSH-avain ja yhteys.")
        tunnel_proc.terminate()
        sys.exit(1)

    log(f"Tunneli auki: 127.0.0.1:{local_port}")

    # MySQL
    log("Yhdistetään MySQL:ään...")
    try:
        conn = connect_db(local_port)
        log("MySQL-yhteys OK.")
    except Exception as e:
        print(f"\n  VIRHE: MySQL-yhteys epäonnistui: {e}")
        tunnel_proc.terminate()
        sys.exit(1)

    print()
    print("─" * 56)

    # Etsi ottelu
    session = make_session()
    match, cat_id, league = find_todays_match(session, args.venue, args.team, log_fn=log)

    if not match:
        print()
        print("  Tänään ei löydy ottelua hakuehdoilla:")
        print(f"     Kenttä  : {args.venue}")
        print(f"     Joukkue : {args.team}")
        conn.close()
        tunnel_proc.terminate()
        sys.exit(0)

    match_id = str(match["match_id"])
    home = match.get("team_A_name", "?")
    away = match.get("team_B_name", "?")

    print()
    print(f"  OTTELU : {home} vs {away}")
    print(f"  Sarja  : {league}")
    print(f"  ID     : {match_id}")
    print()
    print("─" * 56)
    print()

    # Pääsilmukka
    last_score = None
    cycle = 0

    try:
        while True:
            cycle += 1

            live = fetch_live_score(session, match_id, log_fn=log)
            if live:
                update_match_from_live(match, live)

            score  = format_score(match)
            status = format_status(match)

            try:
                push_match_to_db(conn, match, cat_id, league)
                db_ok = "DB ok"
            except Exception as e:
                db_ok = f"DB virhe ({e})"
                try:
                    conn.ping(reconnect=True)
                except Exception:
                    pass

            changed = score != last_score
            last_score = score
            ts = datetime.now().strftime("%H:%M:%S")
            marker = "  *** TULOS MUUTTUI ***" if changed and cycle > 1 else ""

            print(f"  [{ts}]  {home} {score} {away}  |  {status}  |  {db_ok}{marker}")

            if (match.get("status") or "").lower() == "played":
                log("Ottelu päättynyt. Lopetetaan.")
                break

            for remaining in range(args.interval, 0, -1):
                print(f"\r  Seuraava päivitys {remaining:3d}s ...", end="", flush=True)
                time.sleep(1)
            print("\r" + " " * 40 + "\r", end="", flush=True)

    except KeyboardInterrupt:
        print()
        log("Keskeytetty (Ctrl+C).")

    finally:
        conn.close()
        tunnel_proc.terminate()
        log("Yhteydet suljettu.")


if __name__ == "__main__":
    main()
