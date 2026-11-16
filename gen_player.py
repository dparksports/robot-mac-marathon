#!/usr/bin/env python3
"""
gen_player.py — Generate player.html: a security DVR-style video player dashboard.
Run from the robot-mac-main directory:
    python3 gen_player.py
Then open player.html in Safari or Chrome.
"""

import os, glob, re, json
from datetime import datetime
from collections import defaultdict

# ─── 1. Parse flagged filenames from intrusion_report.html ───────────────────
flagged = set()
report_path = 'intrusion_report.html'
if os.path.exists(report_path):
    with open(report_path) as f:
        html = f.read()
    for m in re.findall(r'class="filename">(.*?)</div>', html):
        flagged.add(m.strip())
print(f"Flagged clips from intrusion report: {len(flagged)}")

# ─── 2. Scan sessions ─────────────────────────────────────────────────────────
sessions = []
for sdir in sorted(glob.glob('security_*')):
    if not os.path.isdir(sdir):
        continue
    # Only trigger_*.mov files (skip final_ / recovered_)
    movs = sorted(f for f in glob.glob(os.path.join(sdir, 'trigger_*.mov')))
    if not movs:
        continue

    clips = []
    for path in movs:
        fname = os.path.basename(path)
        size = os.path.getsize(path)
        # parse time from trigger_YYYY-MM-DD_HH-MM-SS.mov
        parts = os.path.splitext(fname)[0].split('_')
        time_str = ''
        date_str = ''
        if len(parts) >= 3:
            date_str = parts[1]
            time_str = parts[2].replace('-', ':')
        clips.append({
            'name': fname,
            'path': path.replace('\\', '/'),
            'date': date_str,
            'time': time_str,
            'size': size,
            'flagged': fname in flagged,
        })

    # Session start time from directory name security_YYYY-MM-DD_HH-MM-SS
    sparts = sdir.split('_')
    s_date = sparts[1] if len(sparts) > 1 else ''
    s_time = sparts[2].replace('-', ':') if len(sparts) > 2 else ''

    sessions.append({
        'id': sdir,
        'date': s_date,
        'time': s_time,
        'clips': clips,
        'flaggedCount': sum(1 for c in clips if c['flagged']),
    })

total_clips = sum(len(s['clips']) for s in sessions)
print(f"Sessions: {len(sessions)}, Total clips: {total_clips}")

# ─── 3. Build HTML ────────────────────────────────────────────────────────────
data_js = json.dumps(sessions, indent=None, separators=(',', ':'))

HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🛡️ Security Video Player</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:       #08090f;
  --surf:     #0f1320;
  --surf2:    #161c2e;
  --surf3:    #1e2640;
  --border:   rgba(255,255,255,0.07);
  --text:     #e2e8f0;
  --dim:      #64748b;
  --accent:   #6366f1;
  --ag:       rgba(99,102,241,0.18);
  --green:    #22c55e;
  --red:      #ef4444;
  --amber:    #f59e0b;
  --sidebar:  280px;
  --header:   52px;
  --queue:    128px;
}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Inter',-apple-system,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden}}

/* ── Top bar ── */
#topbar{{height:var(--header);background:var(--surf);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:16px;padding:0 16px;flex-shrink:0;z-index:10}}
#topbar h1{{font-size:16px;font-weight:700;background:linear-gradient(135deg,#c084fc,#6366f1,#38bdf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent;white-space:nowrap}}
#search{{flex:1;max-width:320px;background:var(--surf2);border:1px solid var(--border);border-radius:8px;padding:7px 12px;color:var(--text);font-size:13px;outline:none}}
#search::placeholder{{color:var(--dim)}}
#search:focus{{border-color:var(--accent)}}
.tb-stat{{font-size:12px;color:var(--dim);white-space:nowrap}}
.tb-stat span{{color:var(--text);font-weight:600}}
.speed-btns{{display:flex;gap:4px;margin-left:auto}}
.spd{{padding:4px 10px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--dim);font-size:12px;cursor:pointer;transition:all .15s}}
.spd.active,.spd:hover{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.kbd-hint{{font-size:11px;color:var(--dim);margin-left:8px}}

/* ── Main layout ── */
#main{{flex:1;display:flex;overflow:hidden}}

/* ── Sidebar ── */
#sidebar{{width:var(--sidebar);background:var(--surf);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;overflow:hidden}}
#sidebar-header{{padding:12px 16px;border-bottom:1px solid var(--border);font-size:12px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;display:flex;justify-content:space-between;align-items:center}}
#filter-flagged{{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--dim);cursor:pointer;padding:3px 8px;border-radius:6px;border:1px solid var(--border);background:transparent;transition:all .15s}}
#filter-flagged:hover,#filter-flagged.active{{color:var(--red);border-color:rgba(239,68,68,.4);background:rgba(239,68,68,.08)}}
#sess-list{{flex:1;overflow-y:auto;padding:8px 0}}
#sess-list::-webkit-scrollbar{{width:4px}}
#sess-list::-webkit-scrollbar-thumb{{background:var(--surf3);border-radius:2px}}

.day-group{{}}
.day-header{{padding:6px 16px;font-size:11px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;cursor:pointer;display:flex;align-items:center;gap:6px;user-select:none}}
.day-header:hover{{color:var(--text)}}
.day-arrow{{font-size:9px;transition:transform .15s}}
.day-collapsed .day-arrow{{transform:rotate(-90deg)}}
.day-sessions{{}}
.day-collapsed .day-sessions{{display:none}}

.sess-item{{padding:8px 16px 8px 24px;cursor:pointer;transition:background .15s;border-left:2px solid transparent;display:flex;flex-direction:column;gap:2px}}
.sess-item:hover{{background:var(--surf2)}}
.sess-item.active{{background:var(--surf3);border-left-color:var(--accent)}}
.sess-time{{font-size:13px;font-weight:600;color:var(--text)}}
.sess-meta{{font-size:11px;color:var(--dim);display:flex;gap:8px;align-items:center}}
.sess-badge{{padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600}}
.badge-ok{{background:rgba(34,197,94,.12);color:var(--green)}}
.badge-flag{{background:rgba(239,68,68,.12);color:var(--red)}}

/* ── Content ── */
#content{{flex:1;display:flex;flex-direction:column;overflow:hidden;background:var(--bg)}}

/* ── Video area ── */
#video-wrap{{flex:1;display:flex;align-items:center;justify-content:center;background:#000;position:relative;overflow:hidden}}
#player{{max-width:100%;max-height:100%;width:100%;height:100%;object-fit:contain}}
#no-video{{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:var(--dim);height:100%}}
#no-video .icon{{font-size:64px;opacity:.3}}
#no-video p{{font-size:14px}}

/* overlay info */
#vid-overlay{{position:absolute;bottom:0;left:0;right:0;padding:12px 16px;background:linear-gradient(transparent,rgba(0,0,0,.7));display:flex;align-items:center;gap:12px;pointer-events:none;opacity:0;transition:opacity .3s}}
#video-wrap:hover #vid-overlay{{opacity:1}}
#vid-name{{font-size:13px;font-weight:600;flex:1}}
#vid-pos{{font-size:12px;color:rgba(255,255,255,.6)}}

/* ── Clip queue ── */
#queue-wrap{{height:var(--queue);background:var(--surf);border-top:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0}}
#queue-bar{{padding:6px 16px;display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--border)}}
#queue-label{{font-size:11px;color:var(--dim);font-weight:600;text-transform:uppercase;letter-spacing:.07em}}
#queue-pos{{font-size:12px;color:var(--text)}}
#queue-eta{{font-size:11px;color:var(--dim);margin-left:auto}}
#queue{{display:flex;gap:6px;padding:8px 12px;overflow-x:auto;align-items:center;flex:1}}
#queue::-webkit-scrollbar{{height:3px}}
#queue::-webkit-scrollbar-thumb{{background:var(--surf3);border-radius:2px}}

.clip-item{{flex-shrink:0;width:80px;padding:6px 8px;border-radius:8px;background:var(--surf2);border:1px solid var(--border);cursor:pointer;transition:all .15s;display:flex;flex-direction:column;gap:3px}}
.clip-item:hover{{border-color:var(--accent);background:var(--surf3)}}
.clip-item.active{{border-color:var(--accent);background:var(--ag);box-shadow:0 0 12px var(--ag)}}
.clip-item.flagged-clip{{border-color:rgba(239,68,68,.3)}}
.clip-item.flagged-clip .clip-time{{color:var(--red)}}
.clip-time{{font-size:11px;font-weight:600;color:var(--text)}}
.clip-size{{font-size:10px;color:var(--dim)}}
.clip-flag{{font-size:9px;color:var(--red)}}

/* ── Autoplay indicator ── */
#autoplay-wrap{{display:flex;align-items:center;gap:8px}}
#autoplay-toggle{{width:32px;height:18px;border-radius:9px;background:var(--surf3);border:1px solid var(--border);cursor:pointer;position:relative;transition:background .2s;flex-shrink:0}}
#autoplay-toggle.on{{background:var(--accent);border-color:var(--accent)}}
#autoplay-toggle::after{{content:'';position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;background:#fff;transition:transform .2s}}
#autoplay-toggle.on::after{{transform:translateX(14px)}}
#autoplay-label{{font-size:11px;color:var(--dim)}}

@media(max-width:700px){{
  #sidebar{{width:220px}}
  :root{{--queue:100px}}
}}
</style>
</head>
<body>

<!-- Top bar -->
<div id="topbar">
  <h1>🛡️ Security Player</h1>
  <input id="search" type="text" placeholder="Search date or time… e.g. 2026-04-10 or 14:28">
  <div class="tb-stat">Day: <span id="stat-day">—</span></div>
  <div class="tb-stat">Session: <span id="stat-sess">—</span></div>
  <div class="tb-stat">Clip: <span id="stat-clip">—</span></div>
  <div id="autoplay-wrap">
    <div id="autoplay-toggle" class="on" title="Auto-advance to next clip"></div>
    <span id="autoplay-label">Auto</span>
  </div>
  <div class="speed-btns">
    <button class="spd active" data-spd="1">1×</button>
    <button class="spd" data-spd="2">2×</button>
    <button class="spd" data-spd="4">4×</button>
    <button class="spd" data-spd="8">8×</button>
  </div>
  <div class="kbd-hint">← → clips · ↑↓ sessions · Space · F fullscreen</div>
</div>

<!-- Main -->
<div id="main">

  <!-- Sidebar -->
  <div id="sidebar">
    <div id="sidebar-header">
      Sessions
      <button id="filter-flagged" title="Show only sessions with flagged clips">🚨 Flagged</button>
    </div>
    <div id="sess-list"></div>
  </div>

  <!-- Content -->
  <div id="content">

    <!-- Video -->
    <div id="video-wrap">
      <div id="no-video">
        <div class="icon">🎬</div>
        <p>Select a session from the sidebar</p>
      </div>
      <video id="player" style="display:none" preload="auto" playsinline></video>
      <div id="vid-overlay">
        <div id="vid-name"></div>
        <div id="vid-pos"></div>
      </div>
    </div>

    <!-- Queue -->
    <div id="queue-wrap">
      <div id="queue-bar">
        <span id="queue-label">Clip Queue</span>
        <span id="queue-pos">No session loaded</span>
        <span id="queue-eta"></span>
      </div>
      <div id="queue"></div>
    </div>

  </div>
</div>

<script>
// ─── Data ───────────────────────────────────────────────────────────────────
const SESSIONS = {data_js};

// ─── State ───────────────────────────────────────────────────────────────────
let curSessIdx = -1;
let curClipIdx = -1;
let playbackRate = 1;
let autoplay = true;
let flaggedOnly = false;
let searchQ = '';

// ─── DOM refs ────────────────────────────────────────────────────────────────
const player    = document.getElementById('player');
const noVideo   = document.getElementById('no-video');
const sessListEl= document.getElementById('sess-list');
const queueEl   = document.getElementById('queue');
const queuePos  = document.getElementById('queue-pos');
const queueEta  = document.getElementById('queue-eta');
const statDay   = document.getElementById('stat-day');
const statSess  = document.getElementById('stat-sess');
const statClip  = document.getElementById('stat-clip');
const vidName   = document.getElementById('vid-name');
const vidPos    = document.getElementById('vid-pos');
const searchEl  = document.getElementById('search');
const autoBtn   = document.getElementById('autoplay-toggle');
const flagBtn   = document.getElementById('filter-flagged');

// ─── Filter sessions ──────────────────────────────────────────────────────────
function visibleSessions() {{
  return SESSIONS.filter(s => {{
    if (flaggedOnly && s.flaggedCount === 0) return false;
    if (searchQ) {{
      const q = searchQ.toLowerCase();
      return s.id.toLowerCase().includes(q) || s.date.includes(q) || s.time.includes(q);
    }}
    return true;
  }});
}}

// ─── Build sidebar ────────────────────────────────────────────────────────────
function buildSidebar() {{
  sessListEl.innerHTML = '';
  const vis = visibleSessions();
  const byDate = {{}};
  vis.forEach(s => {{
    (byDate[s.date] = byDate[s.date] || []).push(s);
  }});

  Object.keys(byDate).sort().forEach(date => {{
    const group = document.createElement('div');
    group.className = 'day-group';

    const d = new Date(date + 'T12:00:00');
    const label = d.toLocaleDateString('en-US', {{weekday:'short',month:'short',day:'numeric'}});
    const daySessions = byDate[date];
    const dayFlagged  = daySessions.reduce((a,s) => a + s.flaggedCount, 0);
    const dayClips    = daySessions.reduce((a,s) => a + s.clips.length, 0);

    const header = document.createElement('div');
    header.className = 'day-header';
    header.innerHTML = `<span class="day-arrow">▾</span> ${{label}} <span style="color:var(--dim);font-weight:400">(${{dayClips}} clips${{dayFlagged ? ' · <span style=color:var(--red)>' + dayFlagged + '🚨</span>' : ''}})</span>`;
    header.addEventListener('click', () => {{
      group.classList.toggle('day-collapsed');
    }});
    group.appendChild(header);

    const sessionsDiv = document.createElement('div');
    sessionsDiv.className = 'day-sessions';
    daySessions.forEach(s => {{
      const globalIdx = SESSIONS.indexOf(s);
      const item = document.createElement('div');
      item.className = 'sess-item' + (globalIdx === curSessIdx ? ' active' : '');
      item.dataset.idx = globalIdx;
      const flagBadge = s.flaggedCount > 0
        ? `<span class="sess-badge badge-flag">🚨 ${{s.flaggedCount}}</span>`
        : `<span class="sess-badge badge-ok">✓</span>`;
      item.innerHTML = `
        <div class="sess-time">${{s.time}}</div>
        <div class="sess-meta">
          <span>${{s.clips.length}} clips</span>
          ${{flagBadge}}
        </div>`;
      item.addEventListener('click', () => loadSession(globalIdx));
      sessionsDiv.appendChild(item);
    }});
    group.appendChild(sessionsDiv);
    sessListEl.appendChild(group);
  }});
}}

// ─── Load session ─────────────────────────────────────────────────────────────
function loadSession(idx) {{
  curSessIdx = idx;
  curClipIdx = -1;
  buildSidebar();
  buildQueue();
  playClip(0);
}}

// ─── Build clip queue ─────────────────────────────────────────────────────────
function buildQueue() {{
  queueEl.innerHTML = '';
  if (curSessIdx < 0) return;
  const sess = SESSIONS[curSessIdx];
  sess.clips.forEach((c, i) => {{
    const el = document.createElement('div');
    el.className = 'clip-item' + (i === curClipIdx ? ' active' : '') + (c.flagged ? ' flagged-clip' : '');
    el.dataset.i = i;
    el.innerHTML = `
      <div class="clip-time">${{c.time}}</div>
      <div class="clip-size">${{fmtSize(c.size)}}</div>
      ${{c.flagged ? '<div class="clip-flag">🚨 flagged</div>' : ''}}`;
    el.addEventListener('click', () => playClip(i));
    queueEl.appendChild(el);
  }});
  updateQueuePos();
}}

function updateQueuePos() {{
  if (curSessIdx < 0) {{ queuePos.textContent = 'No session loaded'; return; }}
  const sess = SESSIONS[curSessIdx];
  queuePos.textContent = curClipIdx >= 0
    ? `Clip ${{curClipIdx+1}} of ${{sess.clips.length}}`
    : `${{sess.clips.length}} clips`;
  // ETA at current speed
  const remaining = sess.clips.length - Math.max(curClipIdx, 0);
  const secPerClip = 61;
  const eta = Math.round(remaining * secPerClip / playbackRate);
  queueEta.textContent = eta > 0 ? `~${{fmtDur(eta)}} remaining at ${{playbackRate}}×` : '';
}}

// ─── Play clip ────────────────────────────────────────────────────────────────
function playClip(i) {{
  if (curSessIdx < 0) return;
  const sess = SESSIONS[curSessIdx];
  if (i < 0 || i >= sess.clips.length) return;
  curClipIdx = i;
  const clip = sess.clips[i];

  noVideo.style.display = 'none';
  player.style.display = 'block';
  player.src = clip.path;
  player.playbackRate = playbackRate;
  player.play().catch(() => {{}});

  vidName.textContent = clip.name;
  vidPos.textContent  = `Clip ${{i+1}} / ${{sess.clips.length}}`;

  // update stats bar
  const d = new Date(clip.date + 'T12:00:00');
  statDay.textContent  = d.toLocaleDateString('en-US', {{month:'short',day:'numeric'}});
  statSess.textContent = sess.time;
  statClip.textContent = `${{i+1}} / ${{sess.clips.length}}`;

  // scroll clip into view in queue
  const clipEls = queueEl.querySelectorAll('.clip-item');
  clipEls.forEach((el,j) => el.classList.toggle('active', j === i));
  if (clipEls[i]) clipEls[i].scrollIntoView({{inline:'center', behavior:'smooth', block:'nearest'}});

  updateQueuePos();
}}

// ─── Auto-advance ─────────────────────────────────────────────────────────────
player.addEventListener('ended', () => {{
  if (!autoplay) return;
  const sess = SESSIONS[curSessIdx];
  if (curClipIdx + 1 < sess.clips.length) {{
    playClip(curClipIdx + 1);
  }} else {{
    // try next session
    const nextIdx = curSessIdx + 1;
    if (nextIdx < SESSIONS.length) loadSession(nextIdx);
  }}
}});

// ─── Speed controls ───────────────────────────────────────────────────────────
document.querySelectorAll('.spd').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.spd').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    playbackRate = parseFloat(btn.dataset.spd);
    player.playbackRate = playbackRate;
    updateQueuePos();
  }});
}});

// ─── Autoplay toggle ──────────────────────────────────────────────────────────
autoBtn.addEventListener('click', () => {{
  autoplay = !autoplay;
  autoBtn.classList.toggle('on', autoplay);
}});

// ─── Flagged filter ───────────────────────────────────────────────────────────
flagBtn.addEventListener('click', () => {{
  flaggedOnly = !flaggedOnly;
  flagBtn.classList.toggle('active', flaggedOnly);
  buildSidebar();
}});

// ─── Search ───────────────────────────────────────────────────────────────────
let searchTimer;
searchEl.addEventListener('input', () => {{
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {{
    searchQ = searchEl.value.trim();
    buildSidebar();
  }}, 200);
}});

// ─── Keyboard shortcuts ───────────────────────────────────────────────────────
document.addEventListener('keydown', e => {{
  if (e.target === searchEl) return;
  switch(e.key) {{
    case 'ArrowRight':
      e.preventDefault();
      if (curSessIdx >= 0) playClip(curClipIdx + 1);
      break;
    case 'ArrowLeft':
      e.preventDefault();
      if (curSessIdx >= 0) playClip(curClipIdx - 1);
      break;
    case 'ArrowUp':
      e.preventDefault();
      if (curSessIdx > 0) loadSession(curSessIdx - 1);
      break;
    case 'ArrowDown':
      e.preventDefault();
      if (curSessIdx < SESSIONS.length - 1) loadSession(curSessIdx + 1);
      break;
    case ' ':
      e.preventDefault();
      if (player.paused) player.play();
      else player.pause();
      break;
    case 'f': case 'F':
      if (!document.fullscreenElement) player.requestFullscreen?.();
      else document.exitFullscreen?.();
      break;
    case '1': setSpeed(1); break;
    case '2': setSpeed(2); break;
    case '3': setSpeed(4); break;
    case '4': setSpeed(8); break;
  }}
}});

function setSpeed(s) {{
  document.querySelectorAll('.spd').forEach(b => b.classList.toggle('active', parseFloat(b.dataset.spd) === s));
  playbackRate = s;
  player.playbackRate = s;
  updateQueuePos();
}}

// ─── Utils ────────────────────────────────────────────────────────────────────
function fmtSize(bytes) {{
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes/1024).toFixed(0) + ' KB';
  return (bytes/1048576).toFixed(1) + ' MB';
}}
function fmtDur(sec) {{
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  if (h) return `${{h}}h ${{m}}m`;
  if (m) return `${{m}}m ${{s}}s`;
  return `${{s}}s`;
}}

// ─── Init ─────────────────────────────────────────────────────────────────────
buildSidebar();
// Auto-load first session for immediate WOW
if (SESSIONS.length > 0) loadSession(SESSIONS.length - 1); // newest first
</script>
</body>
</html>
"""

with open('player.html', 'w') as f:
    f.write(HTML)
print("✅  player.html written!")
print("   Open it in Safari or Chrome: open player.html")
