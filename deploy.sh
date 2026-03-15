#!/bin/bash
# Deploy scoreboard feeder Louhelle
set -e

REMOTE="minormusic@whm57.louhi.net"
FEEDER_DIR="~/gsoft"
SCOREBOARD_DIR="~/public_html/gsoft/scoreboard"

echo "=== Scoreboard Feeder Deploy ==="

# 1. Feeder-koodi → ~/gsoft/
echo "Syncing feeder..."
rsync -avz --delete \
    --exclude='.env' \
    --exclude='.venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    --exclude='.git/' \
    --exclude='.claude/' \
    --exclude='scoreboard/' \
    --exclude='deploy.sh' \
    --exclude='feeder.pid' \
    --exclude='feeder.log' \
    ./ "$REMOTE:$FEEDER_DIR/"

# 2. Scoreboard → ~/public_html/gsoft/scoreboard/
echo "Syncing scoreboard..."
rsync -avz \
    --exclude='config.php' \
    scoreboard/ "$REMOTE:$SCOREBOARD_DIR/"

# 3. Riippuvuudet
echo "Installing dependencies..."
ssh "$REMOTE" "cd $FEEDER_DIR && source .venv/bin/activate && pip install -q -r requirements.txt"

# 4. Tee run_feeder.sh ajettavaksi
ssh "$REMOTE" "chmod +x $FEEDER_DIR/run_feeder.sh"

echo ""
echo "Deploy valmis."
echo "Scoreboard: http://www.minormusic.fi/gsoft/scoreboard/"
echo ""
echo "Muista:"
echo "  - Tarkista ~/gsoft/.env (TASO_API_KEY, DB_PASSWORD)"
echo "  - Tarkista ~/public_html/gsoft/scoreboard/config.php (DB_PASSWORD)"
echo "  - Lisää cron: * * * * * /home/minormusic/gsoft/run_feeder.sh"
