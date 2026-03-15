<?php
/**
 * Gnistan Scoreboard — PHP
 * Lukee otteludata MySQL-cachesta ja näyttää scoreboardin.
 *
 * ?format=json  → JSON API
 * (oletus)      → HTML/CSS/JS OBS Browser Source (1920×72)
 * ?venue=slug   → eri kenttä (oletus: oulunkyla)
 */

date_default_timezone_set('Europe/Helsinki');
require_once __DIR__ . '/config.php';

// ─── Venue slug ──────────────────────────────────────────────────────────────

$venue_slug = isset($_GET['venue'])
    ? preg_replace('/[^a-z0-9-]/', '', strtolower($_GET['venue']))
    : DEFAULT_VENUE_SLUG;

// ─── Hae data MySQL:stä ──────────────────────────────────────────────────────

function get_venue_data(string $slug): ?array {
    $pdo = new PDO(
        'mysql:host=' . DB_HOST . ';dbname=' . DB_NAME . ';charset=utf8mb4',
        DB_USER, DB_PASSWORD,
        [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
    );

    $stmt = $pdo->prepare(
        'SELECT json_data, fetched_at FROM api_cache WHERE cache_key = ?'
    );
    $stmt->execute(["venue_matches_{$slug}"]);
    $row = $stmt->fetch(PDO::FETCH_ASSOC);

    if (!$row) return null;

    $data = json_decode($row['json_data'], true);
    $data['_fetched_at'] = $row['fetched_at'];
    return $data;
}

// ─── Valitse näytettävä ottelu ───────────────────────────────────────────────

function select_match(array $venue_data): ?array {
    $matches = $venue_data['matches'] ?? [];
    if (empty($matches)) return null;

    $now = time();
    $priority_team = strtolower(PRIORITY_TEAM);

    // Kerää eri kategoriat
    $live = [];
    $recent = [];
    $upcoming = [];
    $finished = [];

    foreach ($matches as $m) {
        $status = strtolower($m['status'] ?? '');

        if (in_array($status, ['live', 'playing'])) {
            $live[] = $m;
        } elseif ($status === 'played') {
            $changed = $m['status_changed_at'] ?? '';
            $changed_ts = $changed ? strtotime($changed) : 0;
            if ($changed_ts && ($now - $changed_ts) < 900) {
                $recent[] = $m; // päättynyt < 15 min sitten
            } else {
                $finished[] = $m;
            }
        } else {
            $upcoming[] = $m;
        }
    }

    // 1. LIVE — Gnistan ensin, muuten viimeisin alkanut
    if (!empty($live)) {
        return pick_priority($live, $priority_team);
    }

    // 2. Juuri päättynyt (< 15 min)
    if (!empty($recent)) {
        // Jos on myös upcoming, näytä se jos alkaa pian
        if (!empty($upcoming)) {
            $next = $upcoming[0]; // jo aikajärjestyksessä
            $kick = strtotime('today ' . substr($next['time'] ?? '', 0, 5));
            if ($kick && ($kick - $now) < 300) {
                // Seuraava alkaa 5 min sisällä → vaihda siihen
                return with_reason($next, 'upcoming_soon');
            }
        }
        return with_reason(pick_priority($recent, $priority_team), 'recent');
    }

    // 3. Seuraava tulossa
    if (!empty($upcoming)) {
        return with_reason($upcoming[0], 'upcoming');
    }

    // 4. Viimeisin päättynyt
    if (!empty($finished)) {
        return with_reason(end($finished), 'finished');
    }

    return null;
}

function pick_priority(array $matches, string $team): array {
    // Gnistan-ottelu ensin
    foreach ($matches as $m) {
        $teams = strtolower(($m['team_A_name'] ?? '') . ' ' . ($m['team_B_name'] ?? ''));
        if (strpos($teams, $team) !== false) {
            return with_reason($m, 'live');
        }
    }
    // Viimeisin (viimeinen listassa, joka on aikajärjestyksessä)
    return with_reason(end($matches), 'live');
}

function with_reason(array $match, string $reason): array {
    $match['_display_reason'] = $reason;
    return $match;
}

// ─── JSON API ────────────────────────────────────────────────────────────────

if (isset($_GET['format']) && $_GET['format'] === 'json') {
    header('Content-Type: application/json; charset=utf-8');
    header('Access-Control-Allow-Origin: *');
    header('Cache-Control: no-cache');

    $data = get_venue_data($venue_slug);
    if (!$data) {
        echo json_encode(['error' => 'no_data', 'match' => null]);
        exit;
    }

    $selected = select_match($data);
    $fetched = $data['_fetched_at'] ?? '';
    $feeder_alive = $fetched && (time() - strtotime($fetched)) < 180;

    echo json_encode([
        'match' => $selected,
        'all_matches' => array_map(function($m) {
            return [
                'match_id' => $m['match_id'],
                'time' => $m['time'] ?? '',
                'status' => $m['status'] ?? '',
                'team_A_name' => $m['team_A_name'] ?? '',
                'team_B_name' => $m['team_B_name'] ?? '',
                'fs_A' => $m['fs_A'],
                'fs_B' => $m['fs_B'],
            ];
        }, $data['matches'] ?? []),
        'venue' => $data['venue'] ?? '',
        'updated_at' => $data['updated_at'] ?? '',
        'feeder_alive' => $feeder_alive,
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

// ─── HTML Scoreboard ─────────────────────────────────────────────────────────
?>
<!DOCTYPE html>
<html lang="fi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1920">
<title>Scoreboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }

body {
    width: 1920px;
    height: 72px;
    overflow: hidden;
    font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
    background: transparent;
}

.scoreboard {
    display: flex;
    align-items: center;
    height: 72px;
    background: linear-gradient(180deg, #1a1a2e 0%, #16213e 100%);
    color: #fff;
    padding: 0 20px;
    transition: opacity 0.4s ease;
}

.scoreboard.fading { opacity: 0; }

.league {
    font-size: 13px;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 1px;
    width: 200px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.team {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 280px;
}

.team-home { justify-content: flex-end; text-align: right; }
.team-away { justify-content: flex-start; text-align: left; }

.team-name {
    font-size: 22px;
    font-weight: 700;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 260px;
}

.crest {
    width: 40px;
    height: 40px;
    border-radius: 50%;
    object-fit: contain;
    background: rgba(255,255,255,0.1);
    flex-shrink: 0;
}

.crest-placeholder {
    width: 40px;
    height: 40px;
    border-radius: 50%;
    background: rgba(255,255,255,0.05);
    flex-shrink: 0;
}

.score-box {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 12px;
    min-width: 160px;
    margin: 0 24px;
}

.score {
    font-size: 36px;
    font-weight: 800;
    color: #fce600;
    min-width: 40px;
    text-align: center;
    transition: transform 0.3s ease;
}

.score.flash {
    transform: scale(1.3);
    color: #fff;
}

.score-separator {
    font-size: 28px;
    font-weight: 300;
    color: #64748b;
}

.status-box {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-left: auto;
    min-width: 120px;
    justify-content: flex-end;
}

.status-text {
    font-size: 18px;
    font-weight: 600;
    color: #e2e8f0;
}

.live-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #ef4444;
    animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.8); }
}

.goals {
    font-size: 11px;
    color: #94a3b8;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 250px;
}

.team-col {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 2px;
}

.team-col.away {
    align-items: flex-start;
}

.no-match {
    font-size: 16px;
    color: #64748b;
    text-align: center;
    width: 100%;
}
</style>
</head>
<body>

<div class="scoreboard" id="sb">
    <div class="no-match" id="no-match">Ladataan...</div>
</div>

<script>
const POLL_INTERVAL = 30000;
const API_URL = '?format=json';

let currentMatchId = null;
let clockInterval = null;
let clockAnchorMs = 0;
let clockAnchorSec = 0;
let lastScoreA = null;
let lastScoreB = null;

async function fetchData() {
    try {
        const r = await fetch(API_URL);
        const data = await r.json();
        render(data);
    } catch (e) {
        console.error('Fetch error:', e);
    }
}

function render(data) {
    const sb = document.getElementById('sb');
    const m = data.match;

    if (!m) {
        sb.innerHTML = '<div class="no-match">Ei otteluita</div>';
        stopClock();
        return;
    }

    // Ottelun vaihto → fade
    const newId = m.match_id;
    if (currentMatchId && currentMatchId !== newId) {
        sb.classList.add('fading');
        setTimeout(() => {
            renderMatch(m);
            sb.classList.remove('fading');
        }, 400);
    } else {
        renderMatch(m);
    }
    currentMatchId = newId;
}

function renderMatch(m) {
    const sb = document.getElementById('sb');
    const status = (m.status || '').toLowerCase();
    const isLive = status === 'live' || status === 'playing';
    const isPlayed = status === 'played';

    const scoreA = m.fs_A != null ? m.fs_A : '-';
    const scoreB = m.fs_B != null ? m.fs_B : '-';

    // Score flash
    const flashA = lastScoreA !== null && scoreA !== lastScoreA;
    const flashB = lastScoreB !== null && scoreB !== lastScoreB;
    lastScoreA = scoreA;
    lastScoreB = scoreB;

    // Maalit
    const goalsA = formatGoals(m.goals || [], m.team_A_id);
    const goalsB = formatGoals(m.goals || [], m.team_B_id);

    // Logo
    const logoA = m.club_A_crest
        ? `<img class="crest" src="${m.club_A_crest}" onerror="this.className='crest-placeholder'">`
        : '<div class="crest-placeholder"></div>';
    const logoB = m.club_B_crest
        ? `<img class="crest" src="${m.club_B_crest}" onerror="this.className='crest-placeholder'">`
        : '<div class="crest-placeholder"></div>';

    // Status teksti
    let statusHtml;
    if (isLive) {
        statusHtml = `<div class="live-dot"></div><span class="status-text" id="clock"></span>`;
    } else if (isPlayed) {
        statusHtml = `<span class="status-text">LOPPU</span>`;
    } else {
        const t = (m.time || '').substring(0, 5);
        statusHtml = `<span class="status-text">${t || 'TULOSSA'}</span>`;
    }

    sb.innerHTML = `
        <div class="league">${m.league_name || ''}</div>
        <div class="team team-home">
            <div class="team-col">
                <div class="team-name">${m.team_A_name || ''}</div>
                <div class="goals">${goalsA}</div>
            </div>
            ${logoA}
        </div>
        <div class="score-box">
            <span class="score ${flashA ? 'flash' : ''}" id="score-a">${scoreA}</span>
            <span class="score-separator">:</span>
            <span class="score ${flashB ? 'flash' : ''}" id="score-b">${scoreB}</span>
        </div>
        <div class="team team-away">
            ${logoB}
            <div class="team-col away">
                <div class="team-name">${m.team_B_name || ''}</div>
                <div class="goals">${goalsB}</div>
            </div>
        </div>
        <div class="status-box">${statusHtml}</div>
    `;

    // Flash-efekti poisto
    if (flashA || flashB) {
        setTimeout(() => {
            document.querySelectorAll('.score.flash').forEach(el =>
                el.classList.remove('flash'));
        }, 500);
    }

    // Kello
    if (isLive) {
        startClock(m);
    } else {
        stopClock();
    }
}

function formatGoals(goals, teamId) {
    if (!goals || !goals.length || !teamId) return '';
    return goals
        .filter(g => String(g.team_id) === String(teamId))
        .map(g => {
            const name = (g.player_name || '').split(' ').pop();
            return `${name} ${g.time_min}'`;
        })
        .join(', ');
}

function parseMmss(mmss) {
    if (!mmss) return 0;
    const parts = mmss.split(':');
    return parseInt(parts[0] || 0) * 60 + parseInt(parts[1] || 0);
}

function startClock(m) {
    stopClock();

    const period = m.live_period;
    const timerOn = m.live_timer_on;
    const mmss = m.live_time_mmss;
    const periodMin = m.period_min || 45;

    if (period === -1) {
        // Puoliaika
        const el = document.getElementById('clock');
        if (el) el.textContent = 'HT';
        return;
    }

    const baseSec = (period && period > 1) ? periodMin * 60 : 0;

    if (timerOn && mmss) {
        clockAnchorMs = Date.now();
        clockAnchorSec = parseMmss(mmss);

        clockInterval = setInterval(() => {
            const elapsed = (Date.now() - clockAnchorMs) / 1000;
            const totalSec = baseSec + clockAnchorSec + elapsed;
            const displayMin = Math.floor(totalSec / 60) + 1;
            const el = document.getElementById('clock');
            if (el) el.textContent = displayMin + "'";
        }, 1000);

        // Näytä heti
        const displayMin = Math.floor((baseSec + clockAnchorSec) / 60) + 1;
        const el = document.getElementById('clock');
        if (el) el.textContent = displayMin + "'";
    } else {
        const el = document.getElementById('clock');
        if (el) el.textContent = 'LIVE';
    }
}

function stopClock() {
    if (clockInterval) {
        clearInterval(clockInterval);
        clockInterval = null;
    }
}

// Käynnistä
fetchData();
setInterval(fetchData, POLL_INTERVAL);
</script>
</body>
</html>
