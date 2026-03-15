#!/bin/bash
# Cron watchdog — käynnistää feederin jos ei jo pyöri.
# Cron: * * * * * /home/minormusic/gsoft/run_feeder.sh

BASEDIR="$HOME/gsoft"
PIDFILE="$BASEDIR/feeder.pid"
LOGFILE="$BASEDIR/feeder.log"
VENV="$BASEDIR/.venv/bin/activate"

# Jo käynnissä?
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    exit 0
fi

# Siivoa mahdollinen stale PID
rm -f "$PIDFILE"

# Käynnistä
source "$VENV"
cd "$BASEDIR"
nohup python scoreboard_feeder.py --venue "Oulunkylä" --daemon --pidfile "$PIDFILE" >> "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
