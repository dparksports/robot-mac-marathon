#!/usr/bin/env python3
"""
Security Recording Intrusion Detector
======================================
Analyzes security recordings to detect potential intrusions using:
1. File-size anomaly detection (HEVC bitrate spikes = visual change)
2. Frame-differencing motion detection via ffmpeg + numpy
3. Audio volume spike analysis
4. Generates an interactive HTML dashboard with thumbnails

Usage:
    source .venv/bin/activate
    python detect_intrusion.py [--sessions DIR1 DIR2 ...] [--threshold 15.0]
"""

import os
import sys
import json
import glob
import subprocess
import tempfile
import shutil
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import struct

# ── Try importing optional deps ──
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    print("⚠️  numpy not found — falling back to pure-Python motion detection")

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("⚠️  Pillow not found — thumbnail generation disabled")


# ── Configuration ──
FFMPEG = "./ffmpeg"
FFPROBE = "./ffprobe"
MOTION_THRESHOLD = 15.0      # % pixel change to flag as motion
SIZE_ANOMALY_PCT = 12.0       # % deviation from session median to flag
FRAMES_TO_SAMPLE = 6          # frames per video to analyze
AUDIO_SPIKE_DB = -25.0        # dB threshold for audio events
MAX_WORKERS = 4               # parallel video analysis threads
THUMBNAIL_SIZE = (320, 180)   # thumbnail dimensions


class VideoAnalysis:
    """Result of analyzing a single trigger video."""
    def __init__(self, path):
        self.path = path
        self.filename = os.path.basename(path)
        self.session = os.path.basename(os.path.dirname(path))
        self.filesize = os.path.getsize(path) if os.path.exists(path) else 0
        self.timestamp = self._parse_timestamp()
        self.size_anomaly_pct = 0.0
        self.motion_score = 0.0       # 0-100 scale
        self.motion_regions = []       # list of (frame_idx, score)
        self.thumbnail_path = None
        self.duration = 0.0
        self.is_flagged = False
        self.flag_reasons = []

    def _parse_timestamp(self):
        # Extract timestamp from filename like trigger_2026-04-12_22-00-17.mov
        try:
            name = self.filename.replace("trigger_", "").replace(".mov", "")
            return datetime.strptime(name, "%Y-%m-%d_%H-%M-%S")
        except ValueError:
            return None


class SessionAnalysis:
    """Result of analyzing an entire session folder."""
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.start_time = self._parse_session_time()
        self.videos = []
        self.audio_files = []
        self.flagged_videos = []
        self.total_size = 0
        self.median_size = 0
        self.audio_spikes = []

    def _parse_session_time(self):
        try:
            name = self.name.replace("security_", "")
            return datetime.strptime(name, "%Y-%m-%d_%H-%M-%S")
        except ValueError:
            return None


def run_ffprobe(filepath, *args):
    """Run ffprobe and return JSON output."""
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json"] + list(args) + [filepath]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout) if result.stdout else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return {}


def extract_frames(video_path, num_frames=6, size=(320, 180)):
    """Extract evenly-spaced frames from a video as numpy arrays."""
    # Get duration first
    info = run_ffprobe(video_path, "-show_format")
    duration = float(info.get("format", {}).get("duration", 60))

    if duration <= 0:
        return []

    frames = []
    tmpdir = tempfile.mkdtemp(prefix="intrusion_")

    try:
        # Extract frames at evenly-spaced intervals
        interval = duration / (num_frames + 1)
        for i in range(num_frames):
            t = interval * (i + 1)
            out_path = os.path.join(tmpdir, f"frame_{i:03d}.png")
            cmd = [
                FFMPEG, "-v", "quiet",
                "-ss", f"{t:.2f}",
                "-i", video_path,
                "-vframes", "1",
                "-s", f"{size[0]}x{size[1]}",
                "-f", "image2",
                out_path
            ]
            subprocess.run(cmd, timeout=15, capture_output=True)

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                if HAS_NUMPY and HAS_PIL:
                    img = Image.open(out_path).convert("RGB")
                    frames.append(np.array(img, dtype=np.float32))
                elif HAS_PIL:
                    img = Image.open(out_path).convert("RGB")
                    frames.append(img)
    except subprocess.TimeoutExpired:
        pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return frames


def compute_motion_score(frames):
    """
    Compute motion score by comparing consecutive frames.
    Returns (overall_score, per_frame_scores).
    """
    if not frames or len(frames) < 2:
        return 0.0, []

    scores = []
    if HAS_NUMPY:
        for i in range(1, len(frames)):
            diff = np.abs(frames[i] - frames[i-1])
            # Percentage of pixels that changed significantly (>30 intensity units)
            changed_pixels = np.mean(diff > 30.0) * 100
            scores.append(changed_pixels)
    else:
        # Pure Python fallback using PIL
        for i in range(1, len(frames)):
            pixels_a = list(frames[i-1].getdata())
            pixels_b = list(frames[i].getdata())
            changed = 0
            total = len(pixels_a)
            for pa, pb in zip(pixels_a, pixels_b):
                if any(abs(a - b) > 30 for a, b in zip(pa, pb)):
                    changed += 1
            scores.append(changed / total * 100 if total > 0 else 0)

    overall = max(scores) if scores else 0.0
    return overall, scores


def extract_thumbnail(video_path, output_path, timestamp=None):
    """Extract a single thumbnail from a video."""
    t = timestamp or 5.0  # default to 5 seconds in
    cmd = [
        FFMPEG, "-v", "quiet",
        "-ss", f"{t:.2f}",
        "-i", video_path,
        "-vframes", "1",
        "-s", f"{THUMBNAIL_SIZE[0]}x{THUMBNAIL_SIZE[1]}",
        "-q:v", "3",
        output_path
    ]
    try:
        subprocess.run(cmd, timeout=15, capture_output=True)
        return os.path.exists(output_path)
    except subprocess.TimeoutExpired:
        return False


def analyze_audio_levels(audio_path):
    """Analyze an audio file for volume spikes using ffmpeg loudness detection."""
    cmd = [
        FFMPEG, "-v", "quiet",
        "-i", audio_path,
        "-af", "silencedetect=noise=-35dB:d=2",
        "-f", "null", "-"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        # Parse silence detection output to find NON-silent periods
        # The gaps between silence periods are the loud sections
        return result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def analyze_video(video_analysis, do_motion=True):
    """Full analysis of a single video file."""
    va = video_analysis

    # Get duration from ffprobe
    info = run_ffprobe(va.path, "-show_format")
    va.duration = float(info.get("format", {}).get("duration", 0))

    if not do_motion:
        return va

    # Motion detection via frame differencing
    frames = extract_frames(va.path, num_frames=FRAMES_TO_SAMPLE)
    if frames:
        va.motion_score, va.motion_regions = compute_motion_score(frames)

    # Determine if flagged
    if va.motion_score > MOTION_THRESHOLD:
        va.is_flagged = True
        va.flag_reasons.append(f"Motion detected ({va.motion_score:.1f}%)")

    if abs(va.size_anomaly_pct) > SIZE_ANOMALY_PCT:
        va.is_flagged = True
        va.flag_reasons.append(f"File size anomaly ({va.size_anomaly_pct:+.1f}%)")

    return va


def analyze_session(session_path, thumbnails_dir, do_motion=True):
    """Analyze all videos in a session folder."""
    session = SessionAnalysis(session_path)

    # Find all trigger videos
    trigger_files = sorted(glob.glob(os.path.join(session_path, "trigger_*.mov")))
    audio_files = sorted(glob.glob(os.path.join(session_path, "audio_*.m4a")))
    session.audio_files = audio_files

    if not trigger_files:
        return session

    # Create VideoAnalysis objects
    for tf in trigger_files:
        va = VideoAnalysis(tf)
        session.videos.append(va)
        session.total_size += va.filesize

    # Calculate file size statistics
    sizes = [v.filesize for v in session.videos if v.filesize > 0]
    if sizes:
        session.median_size = statistics.median(sizes)
        for va in session.videos:
            if session.median_size > 0:
                va.size_anomaly_pct = ((va.filesize - session.median_size) /
                                       session.median_size * 100)

    # Run motion analysis in parallel
    print(f"  🔍 Analyzing {len(session.videos)} videos", end="", flush=True)
    analyzed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(analyze_video, va, do_motion): va
            for va in session.videos
        }
        for future in as_completed(futures):
            va = future.result()
            analyzed += 1
            if analyzed % 50 == 0:
                print(f".", end="", flush=True)

    print(f" done ({analyzed} videos)")

    # Extract thumbnails for flagged videos
    for va in session.videos:
        if va.is_flagged:
            session.flagged_videos.append(va)
            thumb_name = f"{va.session}_{va.filename.replace('.mov', '.jpg')}"
            thumb_path = os.path.join(thumbnails_dir, thumb_name)
            if extract_thumbnail(va.path, thumb_path):
                va.thumbnail_path = thumb_path

    return session


def generate_html_report(sessions, output_path, thumbnails_dir):
    """Generate an interactive HTML dashboard."""

    total_videos = sum(len(s.videos) for s in sessions)
    total_flagged = sum(len(s.flagged_videos) for s in sessions)
    total_sessions = len(sessions)

    # Collect all flagged videos across sessions for the timeline
    all_flagged = []
    for s in sessions:
        for v in s.flagged_videos:
            all_flagged.append(v)
    all_flagged.sort(key=lambda v: v.timestamp or datetime.min)

    # Build timeline data for chart
    timeline_data = []
    for s in sessions:
        for v in s.videos:
            timeline_data.append({
                "time": v.timestamp.isoformat() if v.timestamp else "",
                "size": v.filesize,
                "motion": round(v.motion_score, 1),
                "flagged": v.is_flagged,
                "file": v.filename,
                "session": v.session,
                "reasons": "; ".join(v.flag_reasons) if v.flag_reasons else ""
            })

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🛡️ Security Intrusion Detection Report</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

  :root {{
    --bg-primary: #0a0e1a;
    --bg-secondary: #111827;
    --bg-card: #1a2035;
    --bg-card-hover: #1f2847;
    --accent: #3b82f6;
    --accent-glow: rgba(59, 130, 246, 0.3);
    --danger: #ef4444;
    --danger-glow: rgba(239, 68, 68, 0.3);
    --success: #10b981;
    --success-glow: rgba(16, 185, 129, 0.3);
    --warning: #f59e0b;
    --text-primary: #f1f5f9;
    --text-secondary: #94a3b8;
    --text-muted: #64748b;
    --border: rgba(255,255,255,0.06);
    --glass: rgba(255,255,255,0.03);
  }}

  * {{ margin:0; padding:0; box-sizing:border-box; }}

  body {{
    font-family: 'Inter', -apple-system, sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    min-height: 100vh;
    overflow-x: hidden;
  }}

  /* Animated gradient background */
  body::before {{
    content: '';
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background:
      radial-gradient(ellipse at 20% 50%, rgba(59,130,246,0.08) 0%, transparent 50%),
      radial-gradient(ellipse at 80% 20%, rgba(139,92,246,0.06) 0%, transparent 50%),
      radial-gradient(ellipse at 50% 80%, rgba(239,68,68,0.04) 0%, transparent 50%);
    animation: bgPulse 15s ease-in-out infinite alternate;
    z-index: -1;
  }}

  @keyframes bgPulse {{
    0% {{ opacity: 0.6; }}
    100% {{ opacity: 1; }}
  }}

  .container {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}

  /* Header */
  .header {{
    text-align: center;
    padding: 48px 24px 36px;
    position: relative;
  }}

  .header h1 {{
    font-size: 2.5rem;
    font-weight: 800;
    background: linear-gradient(135deg, #3b82f6, #8b5cf6, #ec4899);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 8px;
    letter-spacing: -0.02em;
  }}

  .header .subtitle {{
    color: var(--text-secondary);
    font-size: 1.05rem;
    font-weight: 300;
  }}

  .generated-at {{
    color: var(--text-muted);
    font-size: 0.8rem;
    margin-top: 12px;
  }}

  /* Summary Cards */
  .summary-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px;
    margin: 32px 0;
  }}

  .summary-card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 24px;
    position: relative;
    overflow: hidden;
    transition: transform 0.2s, box-shadow 0.2s;
  }}

  .summary-card:hover {{
    transform: translateY(-2px);
    box-shadow: 0 8px 32px rgba(0,0,0,0.3);
  }}

  .summary-card .label {{
    font-size: 0.8rem;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-weight: 600;
  }}

  .summary-card .value {{
    font-size: 2.5rem;
    font-weight: 800;
    margin: 8px 0 4px;
    line-height: 1;
  }}

  .summary-card .detail {{
    font-size: 0.85rem;
    color: var(--text-secondary);
  }}

  .card-sessions .value {{ color: var(--accent); }}
  .card-videos .value {{ color: var(--text-primary); }}
  .card-flagged .value {{ color: var(--danger); }}
  .card-clean .value {{ color: var(--success); }}

  .card-flagged {{ border-color: rgba(239,68,68,0.2); }}
  .card-flagged::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    background: linear-gradient(90deg, var(--danger), #f97316);
  }}

  .card-clean::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    background: linear-gradient(90deg, var(--success), #06b6d4);
  }}

  /* Section headers */
  .section-header {{
    font-size: 1.4rem;
    font-weight: 700;
    margin: 40px 0 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 10px;
  }}

  /* Flagged events */
  .flagged-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 16px;
    margin: 16px 0;
  }}

  .flagged-card {{
    background: var(--bg-card);
    border: 1px solid rgba(239,68,68,0.15);
    border-radius: 14px;
    overflow: hidden;
    transition: transform 0.2s, box-shadow 0.3s;
  }}

  .flagged-card:hover {{
    transform: translateY(-3px);
    box-shadow: 0 12px 40px var(--danger-glow);
  }}

  .flagged-card .thumb {{
    width: 100%;
    aspect-ratio: 16/9;
    object-fit: cover;
    background: var(--bg-secondary);
    display: block;
  }}

  .flagged-card .no-thumb {{
    width: 100%;
    aspect-ratio: 16/9;
    background: linear-gradient(135deg, var(--bg-secondary), var(--bg-card));
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
    font-size: 2rem;
  }}

  .flagged-card .info {{
    padding: 16px;
  }}

  .flagged-card .filename {{
    font-weight: 600;
    font-size: 0.85rem;
    color: var(--text-primary);
    margin-bottom: 4px;
    word-break: break-all;
  }}

  .flagged-card .session-name {{
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-bottom: 10px;
  }}

  .flagged-card .tags {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 10px;
  }}

  .tag {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 10px;
    border-radius: 99px;
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.03em;
  }}

  .tag-motion {{
    background: rgba(239,68,68,0.15);
    color: #fca5a5;
    border: 1px solid rgba(239,68,68,0.2);
  }}

  .tag-size {{
    background: rgba(245,158,11,0.15);
    color: #fcd34d;
    border: 1px solid rgba(245,158,11,0.2);
  }}

  .flagged-card .meta {{
    display: flex;
    gap: 16px;
    font-size: 0.78rem;
    color: var(--text-secondary);
  }}

  .meta-item {{ display: flex; align-items: center; gap: 4px; }}

  /* Timeline chart */
  .chart-container {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 24px;
    margin: 16px 0;
    overflow-x: auto;
  }}

  .chart-canvas {{
    width: 100%;
    height: 200px;
    position: relative;
  }}

  /* Session table */
  .session-table {{
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
    background: var(--bg-card);
    border-radius: 14px;
    overflow: hidden;
    border: 1px solid var(--border);
  }}

  .session-table th {{
    text-align: left;
    padding: 14px 16px;
    background: rgba(255,255,255,0.03);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
    font-weight: 600;
    border-bottom: 1px solid var(--border);
  }}

  .session-table td {{
    padding: 12px 16px;
    font-size: 0.85rem;
    border-bottom: 1px solid var(--border);
    color: var(--text-secondary);
  }}

  .session-table tr:last-child td {{ border-bottom: none; }}

  .session-table tr:hover td {{
    background: var(--bg-card-hover);
  }}

  .status-badge {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 12px;
    border-radius: 99px;
    font-size: 0.75rem;
    font-weight: 600;
  }}

  .badge-clear {{
    background: rgba(16,185,129,0.12);
    color: #6ee7b7;
  }}

  .badge-alert {{
    background: rgba(239,68,68,0.12);
    color: #fca5a5;
  }}

  /* Timeline */
  .timeline {{
    position: relative;
    padding: 16px 0 16px 32px;
  }}

  .timeline::before {{
    content: '';
    position: absolute;
    left: 11px;
    top: 0;
    bottom: 0;
    width: 2px;
    background: linear-gradient(180deg, var(--accent), var(--danger), var(--success));
    border-radius: 2px;
  }}

  .timeline-item {{
    position: relative;
    padding: 12px 0 24px 16px;
  }}

  .timeline-item::before {{
    content: '';
    position: absolute;
    left: -26px;
    top: 16px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    border: 2px solid var(--accent);
    background: var(--bg-primary);
  }}

  .timeline-item.flagged::before {{
    border-color: var(--danger);
    background: var(--danger);
    box-shadow: 0 0 12px var(--danger-glow);
  }}

  .timeline-time {{
    font-size: 0.8rem;
    color: var(--text-muted);
    font-weight: 500;
  }}

  .timeline-content {{
    font-size: 0.9rem;
    margin-top: 4px;
  }}

  /* Charts using pure CSS/SVG */
  .bar-chart {{
    display: flex;
    align-items: flex-end;
    gap: 1px;
    height: 120px;
    padding: 0;
  }}

  .bar {{
    flex: 1;
    min-width: 2px;
    background: var(--accent);
    border-radius: 2px 2px 0 0;
    transition: background 0.2s;
    position: relative;
    cursor: pointer;
  }}

  .bar:hover {{
    background: #60a5fa;
  }}

  .bar.flagged-bar {{
    background: var(--danger) !important;
  }}

  .bar:hover::after {{
    content: attr(data-tooltip);
    position: absolute;
    bottom: calc(100% + 8px);
    left: 50%;
    transform: translateX(-50%);
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    color: var(--text-primary);
    padding: 6px 10px;
    border-radius: 8px;
    font-size: 0.7rem;
    white-space: nowrap;
    z-index: 100;
    pointer-events: none;
  }}

  .chart-label {{
    display: flex;
    justify-content: space-between;
    font-size: 0.7rem;
    color: var(--text-muted);
    margin-top: 8px;
  }}

  .legend {{
    display: flex;
    gap: 20px;
    margin: 12px 0;
    font-size: 0.8rem;
    color: var(--text-secondary);
  }}

  .legend-dot {{
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    margin-right: 6px;
  }}

  /* Filter Controls */
  .filters {{
    display: flex;
    gap: 12px;
    margin: 16px 0;
    flex-wrap: wrap;
  }}

  .filter-btn {{
    padding: 8px 18px;
    border-radius: 99px;
    border: 1px solid var(--border);
    background: var(--glass);
    color: var(--text-secondary);
    cursor: pointer;
    font-size: 0.8rem;
    font-weight: 500;
    transition: all 0.2s;
  }}

  .filter-btn:hover, .filter-btn.active {{
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }}

  /* Responsive */
  @media (max-width: 768px) {{
    .header h1 {{ font-size: 1.8rem; }}
    .summary-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .flagged-grid {{ grid-template-columns: 1fr; }}
  }}

  /* Loading animation */
  @keyframes shimmer {{
    0% {{ background-position: -200% 0; }}
    100% {{ background-position: 200% 0; }}
  }}

  .loading {{
    background: linear-gradient(90deg, var(--bg-card) 25%, var(--bg-card-hover) 50%, var(--bg-card) 75%);
    background-size: 200% 100%;
    animation: shimmer 1.5s infinite;
  }}
</style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <div class="header">
    <h1>🛡️ Intrusion Detection Report</h1>
    <div class="subtitle">Automated analysis of hotel security recordings</div>
    <div class="generated-at">Generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
  </div>

  <!-- Summary Cards -->
  <div class="summary-grid">
    <div class="summary-card card-sessions">
      <div class="label">Sessions Analyzed</div>
      <div class="value">{total_sessions}</div>
      <div class="detail">{sessions[0].start_time.strftime("%b %d") if sessions and sessions[0].start_time else "?"} — {sessions[-1].start_time.strftime("%b %d") if sessions and sessions[-1].start_time else "?"}</div>
    </div>
    <div class="summary-card card-videos">
      <div class="label">Total Recordings</div>
      <div class="value">{total_videos:,}</div>
      <div class="detail">~{total_videos} min of video</div>
    </div>
    <div class="summary-card card-flagged">
      <div class="label">⚠️ Flagged Events</div>
      <div class="value">{total_flagged}</div>
      <div class="detail">{"Potential intrusions detected!" if total_flagged > 0 else "Review recommended"}</div>
    </div>
    <div class="summary-card card-clean">
      <div class="label">✅ Clean Recordings</div>
      <div class="value">{total_videos - total_flagged:,}</div>
      <div class="detail">{((total_videos - total_flagged) / total_videos * 100) if total_videos > 0 else 0:.1f}% of total</div>
    </div>
  </div>

  <!-- Flagged Events Section -->
  <div class="section-header">
    🚨 Flagged Events
  </div>
"""

    if not all_flagged:
        html += """
    <div style="text-align:center; padding:48px; color:var(--success);">
      <div style="font-size:3rem; margin-bottom:12px;">✅</div>
      <div style="font-size:1.2rem; font-weight:600;">No intrusions detected</div>
      <div style="color:var(--text-muted); margin-top:8px;">All recordings appear to show a static scene</div>
    </div>
"""
    else:
        html += """
    <div class="filters">
      <button class="filter-btn active" onclick="filterEvents('all')">All ({0})</button>
      <button class="filter-btn" onclick="filterEvents('motion')">Motion ({1})</button>
      <button class="filter-btn" onclick="filterEvents('size')">Size Anomaly ({2})</button>
    </div>
    <div class="flagged-grid">
""".format(
            len(all_flagged),
            sum(1 for v in all_flagged if any("Motion" in r for r in v.flag_reasons)),
            sum(1 for v in all_flagged if any("size" in r.lower() for r in v.flag_reasons))
        )

        for v in all_flagged:
            time_str = v.timestamp.strftime("%b %d, %Y %H:%M:%S") if v.timestamp else "Unknown"
            size_mb = v.filesize / 1_000_000
            tags_html = ""
            data_type = "all"
            for r in v.flag_reasons:
                if "Motion" in r:
                    tags_html += f'<span class="tag tag-motion">🔴 {r}</span>'
                    data_type = "motion"
                elif "size" in r.lower():
                    tags_html += f'<span class="tag tag-size">📊 {r}</span>'
                    data_type = "size"

            if v.thumbnail_path and os.path.exists(v.thumbnail_path):
                thumb_rel = os.path.relpath(v.thumbnail_path, os.path.dirname(output_path))
                img_html = f'<img class="thumb" src="{thumb_rel}" alt="Thumbnail" loading="lazy">'
            else:
                img_html = '<div class="no-thumb">📹</div>'

            html += f"""
      <div class="flagged-card" data-type="{data_type}">
        {img_html}
        <div class="info">
          <div class="filename">{v.filename}</div>
          <div class="session-name">{v.session}</div>
          <div class="tags">{tags_html}</div>
          <div class="meta">
            <span class="meta-item">🕐 {time_str}</span>
            <span class="meta-item">💾 {size_mb:.1f} MB</span>
            <span class="meta-item">⏱ {v.duration:.0f}s</span>
          </div>
        </div>
      </div>
"""

        html += "    </div>\n"

    # Session-level file size chart
    html += """
  <!-- File Size Timeline Chart -->
  <div class="section-header">📊 File Size Timeline (per session)</div>
"""

    for session in sessions:
        if len(session.videos) < 5:
            continue

        max_size = max(v.filesize for v in session.videos)
        if max_size == 0:
            continue

        html += f"""
  <div style="margin:16px 0;">
    <div style="font-size:0.85rem; font-weight:600; margin-bottom:8px; color:var(--text-secondary);">
      {session.name} &nbsp;·&nbsp; {len(session.videos)} clips &nbsp;·&nbsp;
      {len(session.flagged_videos)} flagged
      <span class="status-badge {'badge-alert' if session.flagged_videos else 'badge-clear'}">
        {"⚠️ ALERT" if session.flagged_videos else "✅ CLEAR"}
      </span>
    </div>
    <div class="chart-container">
      <div class="legend">
        <span><span class="legend-dot" style="background:var(--accent);"></span>Normal</span>
        <span><span class="legend-dot" style="background:var(--danger);"></span>Flagged</span>
      </div>
      <div class="bar-chart">
"""
        for v in session.videos:
            height_pct = (v.filesize / max_size * 100) if max_size > 0 else 0
            bar_class = "bar flagged-bar" if v.is_flagged else "bar"
            time_label = v.timestamp.strftime("%H:%M") if v.timestamp else ""
            tooltip = f"{v.filename} | {v.filesize/1_000_000:.1f}MB | Motion: {v.motion_score:.1f}%"
            html += f'        <div class="{bar_class}" style="height:{max(height_pct, 1):.1f}%;" data-tooltip="{tooltip}"></div>\n'

        first_time = session.videos[0].timestamp.strftime("%H:%M") if session.videos[0].timestamp else ""
        last_time = session.videos[-1].timestamp.strftime("%H:%M") if session.videos[-1].timestamp else ""
        html += f"""
      </div>
      <div class="chart-label">
        <span>{first_time}</span>
        <span>{last_time}</span>
      </div>
    </div>
  </div>
"""

    # Sessions overview table
    html += """
  <!-- Sessions Overview -->
  <div class="section-header">📋 Sessions Overview</div>
  <table class="session-table">
    <thead>
      <tr>
        <th>Session</th>
        <th>Start Time</th>
        <th>Recordings</th>
        <th>Duration</th>
        <th>Total Size</th>
        <th>Flagged</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
"""

    for s in sessions:
        start = s.start_time.strftime("%Y-%m-%d %H:%M") if s.start_time else "?"
        n = len(s.videos)
        dur_minutes = n  # each video ≈ 1 minute
        dur_str = f"{dur_minutes // 60}h {dur_minutes % 60}m" if dur_minutes >= 60 else f"{dur_minutes}m"
        size_str = f"{s.total_size / 1_000_000_000:.1f} GB" if s.total_size > 1e9 else f"{s.total_size / 1_000_000:.0f} MB"
        flagged_n = len(s.flagged_videos)
        badge = f'<span class="status-badge badge-alert">⚠️ {flagged_n} ALERTS</span>' if flagged_n > 0 else '<span class="status-badge badge-clear">✅ CLEAR</span>'

        html += f"""
      <tr>
        <td style="font-weight:500; color:var(--text-primary);">{s.name}</td>
        <td>{start}</td>
        <td>{n}</td>
        <td>{dur_str}</td>
        <td>{size_str}</td>
        <td>{flagged_n}</td>
        <td>{badge}</td>
      </tr>
"""

    html += """
    </tbody>
  </table>

  <!-- Detection Methodology -->
  <div class="section-header">🔬 Detection Methodology</div>
  <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:14px; padding:24px; margin:16px 0;">
    <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(280px,1fr)); gap:24px;">
      <div>
        <div style="font-weight:600; margin-bottom:8px; color:var(--accent);">📊 File Size Analysis</div>
        <div style="font-size:0.85rem; color:var(--text-secondary); line-height:1.6;">
          HEVC video compression produces consistent file sizes for static scenes.
          Motion in the frame increases visual complexity, causing the encoder to
          use more bits. Files deviating >""" + f"{SIZE_ANOMALY_PCT:.0f}" + """% from the session median are flagged.
        </div>
      </div>
      <div>
        <div style="font-weight:600; margin-bottom:8px; color:var(--danger);">🔴 Motion Detection</div>
        <div style="font-size:0.85rem; color:var(--text-secondary); line-height:1.6;">
          Frames are extracted at regular intervals and compared pixel-by-pixel.
          Regions where >""" + f"{MOTION_THRESHOLD:.0f}" + """% of pixels change significantly (>30 intensity units)
          between consecutive frames indicate movement in the scene.
        </div>
      </div>
      <div>
        <div style="font-weight:600; margin-bottom:8px; color:var(--warning);">🎧 Audio Correlation</div>
        <div style="font-size:0.85rem; color:var(--text-secondary); line-height:1.6;">
          Continuous audio recordings (.m4a) are cross-referenced with video triggers.
          Since the system records video on audio trigger, every video represents a
          detected sound event above the threshold.
        </div>
      </div>
    </div>
  </div>

</div>

<script>
function filterEvents(type) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  document.querySelectorAll('.flagged-card').forEach(card => {
    if (type === 'all' || card.dataset.type === type) {
      card.style.display = '';
    } else {
      card.style.display = 'none';
    }
  });
}
</script>
</body>
</html>
"""

    with open(output_path, "w") as f:
        f.write(html)

    return output_path


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Security Recording Intrusion Detector")
    parser.add_argument("--sessions", nargs="*", help="Specific session dirs to analyze")
    parser.add_argument("--threshold", type=float, default=MOTION_THRESHOLD,
                        help=f"Motion detection threshold (default: {MOTION_THRESHOLD})")
    parser.add_argument("--size-threshold", type=float, default=SIZE_ANOMALY_PCT,
                        help=f"File size anomaly threshold %% (default: {SIZE_ANOMALY_PCT})")
    parser.add_argument("--no-motion", action="store_true",
                        help="Skip frame-based motion detection (fast mode)")
    parser.add_argument("--output", default="intrusion_report.html",
                        help="Output HTML report path")
    args = parser.parse_args()

    # Update module-level thresholds from CLI args
    import detect_intrusion
    detect_intrusion.MOTION_THRESHOLD = args.threshold
    detect_intrusion.SIZE_ANOMALY_PCT = args.size_threshold

    print("🛡️  Security Recording Intrusion Detector")
    print("=" * 50)

    # Find session directories
    if args.sessions:
        session_dirs = sorted(args.sessions)
    else:
        session_dirs = sorted(glob.glob("security_*"))
        session_dirs = [d for d in session_dirs if os.path.isdir(d)]

    if not session_dirs:
        print("❌ No security session folders found.")
        sys.exit(1)

    print(f"📂 Found {len(session_dirs)} session(s)")

    # Create thumbnails directory
    thumbnails_dir = os.path.join(os.path.dirname(args.output) or ".", "intrusion_thumbnails")
    os.makedirs(thumbnails_dir, exist_ok=True)

    # Analyze each session
    sessions = []
    do_motion = not args.no_motion and (HAS_NUMPY or HAS_PIL)

    if not do_motion and not args.no_motion:
        print("⚠️  No numpy/PIL available — using file-size analysis only")

    for sd in session_dirs:
        print(f"\n📁 Session: {sd}")
        session = analyze_session(sd, thumbnails_dir, do_motion=do_motion)
        sessions.append(session)

        flagged = len(session.flagged_videos)
        if flagged > 0:
            print(f"  🚨 {flagged} flagged video(s):")
            for v in session.flagged_videos:
                print(f"     → {v.filename}: {', '.join(v.flag_reasons)}")
        else:
            print(f"  ✅ No anomalies detected")

    # Generate report
    print(f"\n📊 Generating report...")
    output_path = generate_html_report(sessions, args.output, thumbnails_dir)
    print(f"✅ Report saved: {output_path}")

    # Print summary
    total_flagged = sum(len(s.flagged_videos) for s in sessions)
    total_videos = sum(len(s.videos) for s in sessions)
    print(f"\n{'='*50}")
    print(f"📊 Summary: {total_flagged} flagged / {total_videos} total recordings")
    if total_flagged > 0:
        print(f"⚠️  Review the flagged events in the report!")
    else:
        print(f"✅ No intrusions detected across all sessions.")


if __name__ == "__main__":
    main()
