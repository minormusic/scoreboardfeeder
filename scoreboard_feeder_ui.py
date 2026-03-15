#!/usr/bin/env python3
"""
Gnistan Scoreboard Feeder — GUI
Käynnistä: python scoreboard_feeder_ui.py
"""

import threading
import time
import tkinter as tk
from datetime import date, datetime

from feeder_core import (
    CATEGORIES,
    SCOREBOARD_URL,
    VERSION,
    api_get,
    connect_db,
    db_upsert,
    find_free_port,
    format_score,
    format_status,
    is_port_open,
    make_session,
    needs_ssh_tunnel,
    open_ssh_tunnel,
    parse_cat,
    update_match_from_live,
)


# ─── Taustaprosessi ──────────────────────────────────────────────────────────


class FeederWorker:
    def __init__(self, venue, team, interval, on_log, on_status, on_match):
        self.venue    = venue
        self.team     = team
        self.interval = interval
        self.on_log   = on_log
        self.on_status = on_status
        self.on_match  = on_match
        self._stop    = threading.Event()
        self._thread  = None
        self.tunnel_proc = None
        self.conn     = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self.tunnel_proc:
            try:
                self.tunnel_proc.terminate()
            except Exception:
                pass
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        self.on_status("idle")
        self.on_match("—", "- : -", "—", "", "")

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.on_log(f"[{ts}] {msg}")

    def _run(self):
        self.on_status("searching")

        # DB-yhteys — SSH-tunneli vain tarvittaessa
        if needs_ssh_tunnel():
            port = find_free_port()
            self._log(f"Avataan SSH-tunneli (portti {port})…")
            self.tunnel_proc = open_ssh_tunnel(port)
            for _ in range(15):
                if self._stop.is_set():
                    return
                time.sleep(1)
                if is_port_open(port):
                    break
            else:
                self._log("VIRHE: SSH-tunneli ei auennut.")
                self.on_status("error")
                return
            self._log("SSH-tunneli auki.")
            db_host, db_port = "127.0.0.1", port
        else:
            self._log("Suora MySQL-yhteys (palvelin).")
            db_host, db_port = None, None

        # MySQL
        try:
            self.conn = connect_db(db_host, db_port) if db_host else connect_db()
            self._log("MySQL-yhteys OK.")
        except Exception as e:
            self._log(f"VIRHE MySQL: {e}")
            self.on_status("error")
            if self.tunnel_proc:
                self.tunnel_proc.terminate()
            return

        # Etsi ottelu
        session = make_session()

        today = date.today().strftime("%Y-%m-%d")
        self._log(f"Etsitään ottelua {today} | {self.venue} | {self.team}…")

        match, league = None, ""
        for cid in CATEGORIES:
            if self._stop.is_set():
                return
            cat, comp = parse_cat(cid)
            data = api_get(session, "getMatches",
                           {"competition_id": comp, "category_id": cat},
                           log_fn=self._log)
            if not data:
                continue
            ms = (data if isinstance(data, list)
                  else data.get("matches") or data.get("data") or [])
            for m in ms:
                if (m.get("date") == today
                        and self.venue.lower() in (m.get("venue_name") or "").lower()
                        and self.team.lower() in (
                            (m.get("team_A_name") or "")
                            + " " + (m.get("team_B_name") or "")).lower()):
                    match = m
                    cd = api_get(session, "getCategory",
                                 {"competition_id": comp, "category_id": cat},
                                 log_fn=self._log)
                    if cd and isinstance(cd, dict):
                        league = (cd.get("category") or {}).get("category_name", cid)
                    break
            if match:
                break

        if not match:
            self._log("Tänään ei ottelua näillä hakuehdoilla.")
            self.on_status("error")
            self.conn.close()
            if self.tunnel_proc:
                self.tunnel_proc.terminate()
            return

        home = match.get("team_A_name", "?")
        away = match.get("team_B_name", "?")
        self._log(f"Ottelu: {home} vs {away} | {league}")
        self.on_status("running")

        # Pääsilmukka
        last_score = None
        while not self._stop.is_set():
            live = api_get(session, "getMatch",
                           {"match_id": match.get("match_id")},
                           log_fn=self._log)
            if live:
                if isinstance(live, dict) and "match" in live:
                    live = live["match"]
                update_match_from_live(match, live)

            score  = format_score(match)
            status = format_status(match)
            self.on_match(home, score, away, status, league)

            changed = score != last_score
            last_score = score

            try:
                match_id = str(match.get("match_id", ""))
                db_upsert(self.conn, f"match_{match_id}", match, "match_detail", ttl_hours=3)
                db_ok = "ok"
            except Exception:
                db_ok = "virhe"
                try:
                    self.conn.ping(reconnect=True)
                except Exception:
                    pass

            self._log(f"{home} {score} {away}  |  {status}  |  DB {db_ok}"
                      + ("  *** MUUTOS ***" if changed and score != "- : -" else ""))

            if (match.get("status") or "").lower() == "played":
                self._log("Ottelu päättynyt.")
                self.on_status("idle")
                break

            for _ in range(self.interval):
                if self._stop.is_set():
                    break
                time.sleep(1)

        self.conn.close()
        if self.tunnel_proc:
            self.tunnel_proc.terminate()
        self._log("Pysäytetty.")


# ─── GUI ──────────────────────────────────────────────────────────────────────


class App(tk.Tk):
    COLORS = {
        "idle":      "#6b7280",
        "searching": "#f59e0b",
        "running":   "#22c55e",
        "error":     "#ef4444",
    }
    STATUS_TEXT = {
        "idle":      "Pysäytetty",
        "searching": "Etsitään ottelua…",
        "running":   "Käynnissä",
        "error":     "Virhe / ei ottelua",
    }

    def __init__(self):
        super().__init__()
        self.title("Gnistan Scoreboard Feeder")
        self.resizable(False, False)
        self.configure(bg="#0f172a")
        self.worker = None
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        BG  = "#0f172a"
        BG2 = "#1e293b"
        FG  = "#f1f5f9"

        # Header
        hdr = tk.Frame(self, bg="#1d4ed8", pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"GNISTAN SCOREBOARD FEEDER  v{VERSION}",
                 font=("Helvetica", 14, "bold"),
                 bg="#1d4ed8", fg="white").pack()

        # Status
        sf = tk.Frame(self, bg=BG2, pady=8)
        sf.pack(fill="x")
        self._dot = tk.Label(sf, text="●", font=("Helvetica", 18),
                              bg=BG2, fg=self.COLORS["idle"])
        self._dot.pack(side="left", padx=(14, 4))
        self._status_lbl = tk.Label(sf, text=self.STATUS_TEXT["idle"],
                                     font=("Helvetica", 11),
                                     bg=BG2, fg=FG)
        self._status_lbl.pack(side="left")

        # Tuloslaatikko
        mf = tk.Frame(self, bg=BG, pady=12)
        mf.pack(fill="x", padx=12)

        self._league_lbl = tk.Label(mf, text="",
                                     font=("Helvetica", 9),
                                     bg=BG, fg="#94a3b8")
        self._league_lbl.pack()

        score_row = tk.Frame(mf, bg=BG)
        score_row.pack()

        self._home_lbl = tk.Label(score_row, text="—",
                                   font=("Helvetica", 15, "bold"),
                                   bg=BG, fg=FG, width=16, anchor="e")
        self._home_lbl.pack(side="left")

        self._score_lbl = tk.Label(score_row, text="- : -",
                                    font=("Helvetica", 22, "bold"),
                                    bg="#1e293b", fg="#fce600",
                                    padx=14, pady=4)
        self._score_lbl.pack(side="left", padx=8)

        self._away_lbl = tk.Label(score_row, text="—",
                                   font=("Helvetica", 15, "bold"),
                                   bg=BG, fg=FG, width=16, anchor="w")
        self._away_lbl.pack(side="left")

        self._match_status_lbl = tk.Label(mf, text="",
                                           font=("Helvetica", 10),
                                           bg=BG, fg="#94a3b8")
        self._match_status_lbl.pack(pady=(2, 0))

        # Asetukset
        tk.Frame(self, bg="#334155", height=1).pack(fill="x", padx=12)

        sf2 = tk.Frame(self, bg=BG, pady=8)
        sf2.pack(fill="x", padx=12)

        def setting_row(parent, label, default):
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, width=10, anchor="w",
                     bg=BG, fg="#94a3b8",
                     font=("Helvetica", 10)).pack(side="left")
            var = tk.StringVar(value=default)
            tk.Entry(row, textvariable=var, width=28,
                     bg=BG2, fg=FG, insertbackground=FG,
                     relief="flat", font=("Helvetica", 10)).pack(side="left", padx=(4, 0))
            return var

        self._venue_var    = setting_row(sf2, "Kenttä:",   "Oulunkylä")
        self._team_var     = setting_row(sf2, "Joukkue:",  "Gnistan")
        self._interval_var = setting_row(sf2, "Päivitys:", "60")

        # Napit
        bf = tk.Frame(self, bg=BG, pady=8)
        bf.pack(fill="x", padx=12)

        self._start_btn = tk.Button(
            bf, text="KÄYNNISTÄ",
            font=("Helvetica", 11, "bold"),
            bg="#22c55e", fg="white", activebackground="#16a34a",
            relief="flat", padx=16, pady=6, cursor="hand2",
            command=self._start,
        )
        self._start_btn.pack(side="left", padx=(0, 8))

        self._stop_btn = tk.Button(
            bf, text="PYSÄYTÄ",
            font=("Helvetica", 11, "bold"),
            bg="#ef4444", fg="white", activebackground="#b91c1c",
            relief="flat", padx=16, pady=6, cursor="hand2",
            state="disabled",
            command=self._stop,
        )
        self._stop_btn.pack(side="left")

        tk.Button(
            bf, text="Scoreboard",
            font=("Helvetica", 10),
            bg=BG2, fg="#60a5fa",
            relief="flat", padx=10, pady=6, cursor="hand2",
            command=lambda: self._open_browser(SCOREBOARD_URL),
        ).pack(side="right")

        # Loki
        tk.Frame(self, bg="#334155", height=1).pack(fill="x", padx=12)

        lf = tk.Frame(self, bg=BG)
        lf.pack(fill="both", expand=True, padx=12, pady=(6, 10))

        self._log_box = tk.Text(
            lf, height=10, bg=BG2, fg="#94a3b8",
            font=("Courier", 9), relief="flat",
            state="disabled", wrap="word",
        )
        sb = tk.Scrollbar(lf, command=self._log_box.yview)
        self._log_box.configure(yscrollcommand=sb.set)
        self._log_box.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f"{max(w, 520)}x{max(h, 520)}")

    def _start(self):
        try:
            interval = int(self._interval_var.get())
        except ValueError:
            interval = 60

        self.worker = FeederWorker(
            venue=self._venue_var.get().strip(),
            team=self._team_var.get().strip(),
            interval=interval,
            on_log=self._append_log,
            on_status=self._set_status,
            on_match=self._set_match,
        )
        self.worker.start()
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self.worker = None
        self._start_btn.config(state="normal")
        self._stop_btn.config(state="disabled")

    def _append_log(self, msg):
        self.after(0, lambda: self._do_append_log(msg))

    def _do_append_log(self, msg):
        self._log_box.config(state="normal")
        self._log_box.insert("end", msg + "\n")
        self._log_box.see("end")
        self._log_box.config(state="disabled")

    def _set_status(self, status):
        self.after(0, lambda: self._do_set_status(status))

    def _do_set_status(self, status):
        self._dot.config(fg=self.COLORS.get(status, "#6b7280"))
        self._status_lbl.config(text=self.STATUS_TEXT.get(status, status))
        if status in ("idle", "error"):
            self._start_btn.config(state="normal")
            self._stop_btn.config(state="disabled")

    def _set_match(self, home, score, away, status, league):
        self.after(0, lambda: self._do_set_match(home, score, away, status, league))

    def _do_set_match(self, home, score, away, status, league):
        self._home_lbl.config(text=home)
        self._score_lbl.config(text=score)
        self._away_lbl.config(text=away)
        self._match_status_lbl.config(text=status)
        self._league_lbl.config(text=league)

    def _open_browser(self, url):
        import webbrowser
        webbrowser.open(url)

    def _on_close(self):
        if self.worker:
            self.worker.stop()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
