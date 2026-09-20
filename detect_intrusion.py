#!/usr/bin/env python3
"""
Security Recording Intrusion Detector
=====================================
Analyzes recordings to detect potential intrusions using:

Security sessions (sound-triggered clips + continuous audio):
1. Dense frame analysis: movement (consecutive-frame diffing) plus presence
   (deviation from the clip's median frame — catches a person standing still)
2. Audio correlation: trigger clips are cross-referenced against loud periods
   in the independent continuous .m4a recordings
3. File-size anomaly as a corroborating signal only (HEVC targets a fixed
   bitrate, so size alone is not trusted to flag a clip)
4. Clustering: flagged clips within --cluster-window seconds of each other are
   grouped into correlated intrusion events

Timelapse recordings (--timelapse, camera running continuously):
5. Background subtraction: each sampled frame is compared against the pixelwise
   median frame (the empty scene); sustained deviations become presence events

Generates an interactive HTML dashboard with thumbnails.

Usage:
    source .venv/bin/activate
    python detect_intrusion.py [--sessions DIR1 DIR2 ...] [--threshold 15.0]
    python detect_intrusion.py --timelapse timelapse_2026-09-19_08-51-54.mov
"""

import os
import re
import sys
import json
import glob
import subprocess
import tempfile
import shutil
import statistics
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

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
def _resolve_binary(name):
    """Prefer the repo-local binary (installed by install_ffmpeg.sh), then $PATH."""
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return shutil.which(name) or os.path.join(".", name)


FFMPEG = _resolve_binary("ffmpeg")
FFPROBE = _resolve_binary("ffprobe")
MOTION_THRESHOLD = 15.0       # % pixel change to flag as motion
SIZE_ANOMALY_PCT = 12.0       # % deviation from session median to flag
FRAMES_TO_SAMPLE = 12         # frames per trigger video to analyze
AUDIO_EVENT_DB = -35.0        # audio above this level counts as a loud event
MAX_WORKERS = 4               # parallel video analysis threads
THUMBNAIL_SIZE = (320, 180)   # thumbnail dimensions
CLUSTER_WINDOW = 300.0        # flagged clips within this window group into one event
TL_FRAME_INTERVAL = 30.0      # seconds between sampled frames in timelapse analysis
TL_MAX_FRAMES = 600           # cap on frames sampled from one timelapse file


def _parse_media_timestamp(filename, prefix):
    """Parse 'YYYY-MM-DD_HH-MM-SS' from a recording filename like trigger_... or audio_..."""
    try:
        name = filename.replace(prefix, "").rsplit(".", 1)[0]
        return datetime.strptime(name, "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


class VideoAnalysis:
    """Result of analyzing a single trigger video."""
    def __init__(self, path):
        self.path = path
        self.filename = os.path.basename(path)
        self.session = os.path.basename(os.path.dirname(path))
        self.filesize = os.path.getsize(path) if os.path.exists(path) else 0
        self.timestamp = self._parse_timestamp()
        self.size_anomaly_pct = 0.0
        self.motion_score = 0.0       # overall max of movement/presence, 0-100 scale
        self.presence_score = 0.0     # % pixels differing from the clip's median frame
        self.motion_pairs = 0         # sampled frame gaps with movement above threshold
        self.motion_flag = False
        self.size_flag = False
        self.audio_correlated = False
        self.motion_regions = []       # movement score per sampled frame gap
        self.thumbnail_path = None
        self.duration = 0.0
        self.is_flagged = False
        self.flag_reasons = []

    def _parse_timestamp(self):
        # Extract timestamp from filename like trigger_2026-04-12_22-00-17.mov
        return _parse_media_timestamp(self.filename, "trigger_")


class SessionAnalysis:
    """Result of analyzing an entire session folder."""
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.start_time = self._parse_session_time()
        self.videos = []
        self.audio_files = []
        self.flagged_videos = []
        self.intrusion_events = []   # clusters of temporally-adjacent flagged clips
        self.audio_events = []       # absolute (start, end) loud periods in continuous audio
        self.total_size = 0
        self.median_size = 0

    def _parse_session_time(self):
        return _parse_media_timestamp(self.name, "security_")


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
    Compute motion signals by comparing sampled frames.

    Two complementary signals:
    - movement: % of pixels changing between consecutive sampled frames
      (detects a person moving through the scene)
    - presence: % of pixels differing from the median frame (detects a person
      standing still, which consecutive-frame diffing misses)

    Returns (overall_score, presence_score, movement_scores). Without numpy,
    presence is measured against the first frame instead of the median.
    """
    if not frames or len(frames) < 2:
        return 0.0, 0.0, []

    movement = []
    if HAS_NUMPY:
        background = np.median(np.stack(frames), axis=0)
        presence_scores = [float(np.mean(np.abs(f - background) > 30.0) * 100)
                           for f in frames]
        for i in range(1, len(frames)):
            diff = np.abs(frames[i] - frames[i-1])
            movement.append(float(np.mean(diff > 30.0) * 100))
    else:
        # Pure Python fallback using PIL
        reference = list(frames[0].getdata())
        presence_scores = []
        for f in frames:
            pixels = list(f.getdata())
            changed = sum(1 for pa, pb in zip(reference, pixels)
                          if any(abs(a - b) > 30 for a, b in zip(pa, pb)))
            presence_scores.append(changed / len(reference) * 100 if reference else 0)
        for i in range(1, len(frames)):
            pixels_a = list(frames[i-1].getdata())
            pixels_b = list(frames[i].getdata())
            changed = 0
            total = len(pixels_a)
            for pa, pb in zip(pixels_a, pixels_b):
                if any(abs(a - b) > 30 for a, b in zip(pa, pb)):
                    changed += 1
            movement.append(changed / total * 100 if total > 0 else 0)

    overall = max(max(presence_scores, default=0.0), max(movement, default=0.0))
    return overall, (max(presence_scores) if presence_scores else 0.0), movement


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
    """Analyze an audio file for volume spikes using ffmpeg silence detection."""
    # silencedetect reports at info log level; -v quiet would suppress its output
    cmd = [
        FFMPEG, "-v", "info",
        "-i", audio_path,
        "-af", f"silencedetect=noise={AUDIO_EVENT_DB}dB:d=2",
        "-f", "null", "-"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        # Parse silence detection output to find NON-silent periods
        # The gaps between silence periods are the loud sections
        return result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def parse_silencedetect(stderr_text):
    """Parse ffmpeg silencedetect output into [(start_sec, end_sec), ...] silence intervals."""
    silences = []
    start = None
    for line in stderr_text.splitlines():
        m = re.search(r"silence_start:\s*(-?[0-9.]+)", line)
        if m:
            start = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*(-?[0-9.]+)", line)
        if m and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    return silences


def find_loud_periods(audio_path):
    """Return [(start_sec, end_sec)] periods louder than AUDIO_EVENT_DB in an audio file."""
    info = run_ffprobe(audio_path, "-show_format")
    duration = float(info.get("format", {}).get("duration", 0))
    if duration <= 0:
        return []
    silences = parse_silencedetect(analyze_audio_levels(audio_path))
    # Loud periods are the gaps between silence intervals
    loud, cursor = [], 0.0
    for s_start, s_end in silences:
        if s_start > cursor:
            loud.append((cursor, min(s_start, duration)))
        cursor = max(cursor, s_end)
    if cursor < duration:
        loud.append((cursor, duration))
    return [(s, e) for s, e in loud if e - s >= 1.0]


def correlate_audio(session, candidates):
    """
    Cross-reference candidate trigger clips with loud periods in the continuous
    audio recordings. The .m4a files are recorded independently of the trigger
    pipeline, so a clip whose sound also shows up there is much more likely to
    be a real event than an artifact.
    """
    ts_list = [v.timestamp for v in candidates if v.timestamp]
    if not ts_list:
        return
    session.audio_events = []
    for af in session.audio_files:
        a_start = _parse_media_timestamp(os.path.basename(af), "audio_")
        if a_start is None:
            continue
        # Only decode the audio files whose time window covers a candidate clip
        info = run_ffprobe(af, "-show_format")
        dur = float(info.get("format", {}).get("duration", 0))
        window_end = a_start + timedelta(seconds=max(dur, 0) + 5)
        if not any(a_start <= ts <= window_end for ts in ts_list):
            continue
        loud = find_loud_periods(af)
        abs_loud = [(a_start + timedelta(seconds=s), a_start + timedelta(seconds=e))
                    for s, e in loud]
        session.audio_events.extend(abs_loud)
        for va in candidates:
            if va.timestamp and not va.audio_correlated:
                va.audio_correlated = any(s <= va.timestamp <= e for s, e in abs_loud)


def finalize_flags(session):
    """
    Combine detection signals into the final flag decision.

    Motion (dense frame analysis) flags on its own. File-size anomalies only
    contribute when the continuous audio corroborates the clip, because the
    HEVC encoder targets a fixed bitrate and size alone is a weak signal.
    """
    for va in session.videos:
        va.is_flagged = va.motion_flag or (va.size_flag and va.audio_correlated)
        va.flag_reasons = []
        if va.motion_flag:
            va.flag_reasons.append(f"Motion detected ({va.motion_score:.1f}%)")
        if va.size_flag:
            va.flag_reasons.append(f"File size anomaly ({va.size_anomaly_pct:+.1f}%)")
        if va.audio_correlated:
            va.flag_reasons.append("Audio confirmed in continuous recording")
    session.flagged_videos = [va for va in session.videos if va.is_flagged]


def cluster_flagged(session):
    """
    Group flagged clips that are close in time into intrusion events.
    A person in the room usually trips several clips in a row; isolated single
    clips are more likely one-off noise.
    """
    session.intrusion_events = []
    current = []
    for va in sorted(session.flagged_videos, key=lambda v: v.timestamp or datetime.min):
        if current and va.timestamp and current[-1].timestamp and \
           (va.timestamp - current[-1].timestamp).total_seconds() <= CLUSTER_WINDOW:
            current.append(va)
        else:
            if len(current) >= 2:
                session.intrusion_events.append(current)
            current = [va]
    if len(current) >= 2:
        session.intrusion_events.append(current)


def analyze_video(video_analysis, do_motion=True):
    """Full analysis of a single video file."""
    va = video_analysis

    # Get duration from ffprobe
    info = run_ffprobe(va.path, "-show_format")
    va.duration = float(info.get("format", {}).get("duration", 0))

    if not do_motion:
        return va

    # Dense frame analysis: movement between consecutive frames + presence
    # relative to the clip's median frame (see compute_motion_score)
    frames = extract_frames(va.path, num_frames=FRAMES_TO_SAMPLE)
    if frames:
        va.motion_score, va.presence_score, movement = compute_motion_score(frames)
        va.motion_regions = movement
        va.motion_pairs = sum(1 for m in movement if m > MOTION_THRESHOLD)

    va.motion_flag = va.motion_score > MOTION_THRESHOLD
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

    # Size signal is recorded but only trusted with audio corroboration
    # (see finalize_flags)
    for va in session.videos:
        va.size_flag = abs(va.size_anomaly_pct) > SIZE_ANOMALY_PCT

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

    # Corroborate candidate clips against the continuous audio recordings,
    # then decide the final flags and group them into events
    candidates = [va for va in session.videos if va.motion_flag or va.size_flag]
    correlate_audio(session, candidates)
    finalize_flags(session)
    cluster_flagged(session)

    # Extract thumbnails for flagged videos (flagged_videos was populated by finalize_flags)
    for va in session.flagged_videos:
        thumb_name = f"{va.session}_{va.filename.replace('.mov', '.jpg')}"
        thumb_path = os.path.join(thumbnails_dir, thumb_name)
        if extract_thumbnail(va.path, thumb_path):
            va.thumbnail_path = thumb_path

    return session


class TimelapseAnalysis:
    """Result of background-subtraction analysis of one continuous timelapse recording."""
    def __init__(self, path):
        self.path = path
        self.filename = os.path.basename(path)
        self.start_time = _parse_media_timestamp(self.filename, "timelapse_")
        self.duration = 0.0
        self.frames_sampled = 0
        self.frame_times = []       # relative seconds of each sampled frame
        self.scores = []            # % pixels differing from the background, per frame
        self.max_score = 0.0
        self.sampling_note = ""
        self.flagged_segments = []  # dicts: start, end, score, thumbnail_path


def compute_background_scores(frames):
    """
    Deviation of each frame from the scene background.

    The background is the pixelwise median across all sampled frames: for a
    scene that is empty most of the time, the median is the empty room, so any
    frame containing a person stands out. Without numpy, falls back to
    comparing against the first sampled frame.
    """
    if not frames or len(frames) < 2:
        return []
    if HAS_NUMPY:
        background = np.median(np.stack(frames), axis=0)
        return [float(np.mean(np.abs(f - background) > 30.0) * 100) for f in frames]
    reference = list(frames[0].getdata())
    scores = []
    for f in frames:
        pixels = list(f.getdata())
        changed = sum(1 for pa, pb in zip(reference, pixels)
                      if any(abs(a - b) > 30 for a, b in zip(pa, pb)))
        scores.append(changed / len(reference) * 100 if reference else 0)
    return scores


def find_persistent_segments(times, scores, threshold):
    """
    Group consecutive above-threshold samples into segments. A segment must
    persist across two or more samples (or exceed 2x the threshold on its own)
    so single-sample flickers from auto-exposure don't become events.
    """
    segments = []
    i = 0
    while i < len(scores):
        if scores[i] > threshold:
            j = i
            while j + 1 < len(scores) and scores[j + 1] > threshold:
                j += 1
            if j > i or scores[i] > threshold * 2:
                segments.append((times[i], times[j], max(scores[i:j + 1])))
            i = j + 1
        else:
            i += 1
    return segments


def analyze_timelapse(path, thumbnails_dir, interval=TL_FRAME_INTERVAL):
    """
    Background-subtraction analysis of a continuous timelapse recording.
    Frames are sampled every `interval` seconds; sustained deviations from the
    empty-scene background are flagged as presence events.
    """
    tl = TimelapseAnalysis(path)
    info = run_ffprobe(path, "-show_format")
    tl.duration = float(info.get("format", {}).get("duration", 0))
    if tl.duration <= 0:
        return tl

    count = int(tl.duration // interval)
    if count < 2:
        return tl
    if count > TL_MAX_FRAMES:
        count = TL_MAX_FRAMES
        interval = tl.duration / (count + 1)
        tl.sampling_note = (f"sampling stretched to {interval:.0f}s to stay under "
                            f"the {TL_MAX_FRAMES}-frame cap")
    if not HAS_NUMPY and count > 150:
        count = 150
        interval = tl.duration / (count + 1)
        tl.sampling_note = f"pure-Python mode: sampling stretched to {interval:.0f}s"

    times, frames = [], []
    tmpdir = tempfile.mkdtemp(prefix="tl_")
    try:
        for i in range(count):
            t = interval * (i + 0.5)
            out_path = os.path.join(tmpdir, f"frame_{i:04d}.png")
            cmd = [
                FFMPEG, "-v", "quiet",
                "-ss", f"{t:.2f}",
                "-i", path,
                "-vframes", "1",
                "-s", f"{THUMBNAIL_SIZE[0]}x{THUMBNAIL_SIZE[1]}",
                "-f", "image2",
                out_path
            ]
            subprocess.run(cmd, timeout=15, capture_output=True)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                img = Image.open(out_path).convert("RGB")
                frames.append(np.array(img, dtype=np.float32) if HAS_NUMPY else img)
                times.append(t)
            if (i + 1) % 50 == 0:
                print(f"  … sampled {i + 1}/{count} frames", flush=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    tl.frame_times = times
    tl.frames_sampled = len(frames)
    tl.scores = compute_background_scores(frames)
    tl.max_score = max(tl.scores, default=0.0)

    for start, end, score in find_persistent_segments(times, tl.scores, MOTION_THRESHOLD):
        segment = {"start": start, "end": end, "score": score, "thumbnail_path": None}
        thumb_name = f"tl_{tl.filename.replace('.mov', '')}_{start:.0f}s.jpg"
        thumb_path = os.path.join(thumbnails_dir, thumb_name)
        if extract_thumbnail(path, thumb_path, timestamp=start):
            segment["thumbnail_path"] = thumb_path
        tl.flagged_segments.append(segment)
    return tl


def build_intrusion_events_html(sessions):
    """HTML for correlated intrusion events (clusters of temporally-adjacent flagged clips)."""
    all_events = [(s, ev) for s in sessions for ev in s.intrusion_events]
    html = """
  <!-- Correlated Intrusion Events -->
  <div class="section-header">🔥 Correlated Intrusion Events</div>
"""
    if not all_events:
        html += f"""
  <div style="color:var(--text-muted); margin:8px 0 24px; font-size:0.9rem;">
    None — no flagged clips occurred within {CLUSTER_WINDOW:.0f}s of each other.
  </div>
"""
        return html

    html += '  <div class="flagged-grid">\n'
    for s, ev in all_events:
        start = ev[0].timestamp
        end = ev[-1].timestamp
        start_str = start.strftime("%b %d, %Y %H:%M:%S") if start else "?"
        end_str = end.strftime("%H:%M:%S") if end else "?"
        confirmed = sum(1 for v in ev if v.audio_correlated)
        clips_html = "".join(
            f'<div style="font-size:0.78rem; color:var(--text-secondary); margin-top:4px;">'
            f'• {v.filename} — {", ".join(v.flag_reasons) or "flagged"}</div>'
            for v in ev
        )
        html += f"""
    <div class="flagged-card" style="border-color:rgba(239,68,68,0.35);">
      <div class="info">
        <div class="filename">🔥 {start_str} — {end_str}</div>
        <div class="session-name">{s.name}</div>
        <div class="tags">
          <span class="tag tag-motion">{len(ev)} clips close in time</span>
          <span class="tag tag-audio">🎧 {confirmed} audio-confirmed</span>
        </div>
        {clips_html}
      </div>
    </div>
"""
    html += "  </div>\n"
    return html


def build_timelapse_html(timelapses, output_path):
    """HTML for timelapse background-subtraction results."""
    html = """
  <!-- Timelapse Analysis -->
  <div class="section-header">🎬 Timelapse Background-Subtraction Analysis</div>
"""
    for tl in timelapses:
        dur_str = f"{tl.duration / 3600:.1f} h" if tl.duration >= 3600 else f"{tl.duration / 60:.1f} min"
        note = f" · {tl.sampling_note}" if tl.sampling_note else ""
        badge = (f'<span class="status-badge badge-alert">⚠️ {len(tl.flagged_segments)} EVENTS</span>'
                 if tl.flagged_segments else '<span class="status-badge badge-clear">✅ CLEAR</span>')
        html += f"""
  <div style="margin:16px 0;">
    <div style="font-size:0.85rem; font-weight:600; margin-bottom:8px; color:var(--text-secondary);">
      {tl.filename} &nbsp;·&nbsp; {dur_str} &nbsp;·&nbsp; {tl.frames_sampled} frames sampled{note}
      {badge}
    </div>
    <div class="chart-container">
      <div class="legend">
        <span><span class="legend-dot" style="background:var(--accent);"></span>Background deviation</span>
        <span><span class="legend-dot" style="background:var(--danger);"></span>Presence event</span>
      </div>
      <div class="bar-chart">
"""
        max_score = max(tl.max_score, MOTION_THRESHOLD)
        for t, score in zip(tl.frame_times, tl.scores):
            flagged = score > MOTION_THRESHOLD
            height_pct = (score / max_score * 100) if max_score > 0 else 0
            bar_class = "bar flagged-bar" if flagged else "bar"
            tooltip = f"t={t:.0f}s | deviation {score:.1f}%"
            html += f'        <div class="{bar_class}" style="height:{max(height_pct, 1):.1f}%;" data-tooltip="{tooltip}"></div>\n'
        html += f"""
      </div>
      <div class="chart-label">
        <span>0s</span>
        <span>{tl.duration:.0f}s</span>
      </div>
    </div>
  </div>
"""
        if tl.flagged_segments:
            html += '    <div class="flagged-grid" style="margin-top:-8px;">\n'
            for seg in tl.flagged_segments:
                abs_start = (tl.start_time + timedelta(seconds=seg["start"])
                             if tl.start_time else None)
                time_str = (abs_start.strftime("%b %d, %Y %H:%M:%S") if abs_start
                            else f"t={seg['start']:.0f}s")
                if seg["thumbnail_path"] and os.path.exists(seg["thumbnail_path"]):
                    thumb_rel = os.path.relpath(seg["thumbnail_path"], os.path.dirname(output_path))
                    img_html = f'<img class="thumb" src="{thumb_rel}" alt="Thumbnail" loading="lazy">'
                else:
                    img_html = '<div class="no-thumb">📹</div>'
                html += f"""
      <div class="flagged-card">
        {img_html}
        <div class="info">
          <div class="filename">🎬 {time_str}</div>
          <div class="session-name">{tl.filename}</div>
          <div class="tags"><span class="tag tag-motion">🔴 Presence {seg['score']:.1f}%</span></div>
          <div class="meta">
            <span class="meta-item">⏱ {seg['start']:.0f}s–{seg['end']:.0f}s</span>
          </div>
        </div>
      </div>
"""
            html += "    </div>\n"
    return html


def generate_html_report(sessions, timelapses, output_path, thumbnails_dir):
    """Generate an interactive HTML dashboard."""

    total_videos = sum(len(s.videos) for s in sessions)
    total_flagged = sum(len(s.flagged_videos) for s in sessions)
    total_sessions = len(sessions)
    audio_confirmed = sum(1 for s in sessions for v in s.flagged_videos
                          if v.audio_correlated)
    total_tl_segments = sum(len(t.flagged_segments) for t in timelapses)
    total_tl_frames = sum(t.frames_sampled for t in timelapses)

    timelapse_card = f"""
    <div class="summary-card card-flagged">
      <div class="label">🎬 Timelapse Events</div>
      <div class="value">{total_tl_segments}</div>
      <div class="detail">{total_tl_frames:,} frames analyzed across {len(timelapses)} recording(s)</div>
    </div>""" if timelapses else ""

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
                "presence": round(v.presence_score, 1),
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

  .tag-audio {{
    background: rgba(59,130,246,0.15);
    color: #93c5fd;
    border: 1px solid rgba(59,130,246,0.2);
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
    <div class="subtitle">Automated analysis of security and timelapse recordings</div>
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
    <div class="summary-card card-sessions">
      <div class="label">🎧 Audio Confirmed</div>
      <div class="value">{audio_confirmed}</div>
      <div class="detail">flagged clips with matching sound in the continuous audio</div>
    </div>{timelapse_card}
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
      <button class="filter-btn" onclick="filterEvents('audio')">Audio Confirmed ({2})</button>
      <button class="filter-btn" onclick="filterEvents('size')">Size Anomaly ({3})</button>
    </div>
    <div class="flagged-grid">
""".format(
            len(all_flagged),
            sum(1 for v in all_flagged if any("Motion" in r for r in v.flag_reasons)),
            sum(1 for v in all_flagged if v.audio_correlated),
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
                elif "size" in r.lower():
                    tags_html += f'<span class="tag tag-size">📊 {r}</span>'
                elif "Audio confirmed" in r:
                    tags_html += f'<span class="tag tag-audio">🎧 {r}</span>'
            # Filter category: motion takes priority, then audio, then size
            if any("Motion" in r for r in v.flag_reasons):
                data_type = "motion"
            elif v.audio_correlated:
                data_type = "audio"
            elif any("size" in r.lower() for r in v.flag_reasons):
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

    # Correlated intrusion events (clusters of temporally-adjacent flagged clips)
    html += build_intrusion_events_html(sessions)

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
            tooltip = f"{v.filename} | {v.filesize/1_000_000:.1f}MB | Motion: {v.motion_score:.1f}% | Presence: {v.presence_score:.1f}%"
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

    # Timelapse background-subtraction analysis section
    if timelapses:
        html += build_timelapse_html(timelapses, output_path)

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
        <div style="font-weight:600; margin-bottom:8px; color:var(--danger);">🔴 Motion &amp; Presence Detection</div>
        <div style="font-size:0.85rem; color:var(--text-secondary); line-height:1.6;">
          """ + f"{FRAMES_TO_SAMPLE}" + """ evenly-spaced frames are extracted per clip and compared pixel-by-pixel.
          Two signals are scored: movement (pixels changing between consecutive
          frames) and presence (pixels deviating from the clip's median frame —
          catches a person standing still). Clips where &gt;""" + f"{MOTION_THRESHOLD:.0f}" + """% of pixels
          changed are flagged for movement.
        </div>
      </div>
      <div>
        <div style="font-weight:600; margin-bottom:8px; color:var(--warning);">🎧 Audio Correlation</div>
        <div style="font-size:0.85rem; color:var(--text-secondary); line-height:1.6;">
          Continuous audio recordings (.m4a) are analyzed with silencedetect to find
          loud periods (above """ + f"{AUDIO_EVENT_DB:.0f}" + """ dB). Trigger clips whose timestamps fall inside a
          loud period are marked audio-confirmed — the independent mic recording
          corroborates the event the camera captured.
        </div>
      </div>
      <div>
        <div style="font-weight:600; margin-bottom:8px; color:var(--accent);">📊 File Size (corroborating only)</div>
        <div style="font-size:0.85rem; color:var(--text-secondary); line-height:1.6;">
          HEVC encoding targets a fixed bitrate, so file size alone is a weak
          signal. Size anomalies (&gt;""" + f"{SIZE_ANOMALY_PCT:.0f}" + """% from the session median) are only
          trusted to flag a clip when the continuous audio also confirms it.
        </div>
      </div>
      <div>
        <div style="font-weight:600; margin-bottom:8px; color:var(--success);">🔥 Event Clustering</div>
        <div style="font-size:0.85rem; color:var(--text-secondary); line-height:1.6;">
          A person in the room trips several clips in a row. Flagged clips within
          """ + f"{CLUSTER_WINDOW:.0f}" + """ seconds of each other are grouped into a correlated
          intrusion event; isolated single clips are more likely one-off noise.
        </div>
      </div>
      <div>
        <div style="font-weight:600; margin-bottom:8px; color:#c084fc;">🎬 Timelapse Background Subtraction</div>
        <div style="font-size:0.85rem; color:var(--text-secondary); line-height:1.6;">
          Continuous timelapse recordings are sampled every """ + f"{TL_FRAME_INTERVAL:.0f}" + """s. Each frame is
          compared against the pixelwise median frame (the empty scene);
          sustained deviations across consecutive samples become presence events.
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
    # CLI overrides of module thresholds (main runs as __main__, so `global`
    # — not a re-import — is what the analysis functions see)
    global MOTION_THRESHOLD, SIZE_ANOMALY_PCT, CLUSTER_WINDOW
    parser = argparse.ArgumentParser(description="Security Recording Intrusion Detector")
    parser.add_argument("--sessions", nargs="*", help="Specific session dirs to analyze")
    parser.add_argument("--timelapse", nargs="+", metavar="MOV",
                        help="Continuous timelapse recording(s) to analyze via background subtraction")
    parser.add_argument("--tl-interval", type=float, default=TL_FRAME_INTERVAL,
                        help=f"Seconds between sampled frames for timelapse analysis "
                             f"(default: {TL_FRAME_INTERVAL})")
    parser.add_argument("--threshold", type=float, default=MOTION_THRESHOLD,
                        help=f"Motion detection threshold (default: {MOTION_THRESHOLD})")
    parser.add_argument("--size-threshold", type=float, default=SIZE_ANOMALY_PCT,
                        help=f"File size anomaly threshold %% (default: {SIZE_ANOMALY_PCT})")
    parser.add_argument("--cluster-window", type=float, default=CLUSTER_WINDOW,
                        help=f"Seconds between flagged clips to group into one event "
                             f"(default: {CLUSTER_WINDOW})")
    parser.add_argument("--no-motion", action="store_true",
                        help="Skip frame-based motion detection (fast mode)")
    parser.add_argument("--output", default="intrusion_report.html",
                        help="Output HTML report path")
    args = parser.parse_args()

    MOTION_THRESHOLD = args.threshold
    SIZE_ANOMALY_PCT = args.size_threshold
    CLUSTER_WINDOW = args.cluster_window

    print("🛡️  Security Recording Intrusion Detector")
    print("=" * 50)

    # Find session directories
    if args.sessions:
        session_dirs = sorted(args.sessions)
    else:
        session_dirs = sorted(glob.glob("security_*"))
        session_dirs = [d for d in session_dirs if os.path.isdir(d)]

    timelapse_files = [p for p in (args.timelapse or []) if os.path.isfile(p)]
    missing_tl = [p for p in (args.timelapse or []) if not os.path.isfile(p)]
    for p in missing_tl:
        print(f"⚠️  Timelapse file not found: {p}")

    if not session_dirs and not timelapse_files:
        print("❌ No security session folders or timelapse files found.")
        print("   Run from the folder containing security_*/ recordings,")
        print("   or pass --timelapse <file.mov> / --sessions <dir>.")
        sys.exit(1)

    if session_dirs:
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
        if session.intrusion_events:
            print(f"  🔥 {len(session.intrusion_events)} correlated event(s):")
            for ev in session.intrusion_events:
                start = ev[0].timestamp.strftime("%H:%M:%S") if ev[0].timestamp else "?"
                end = ev[-1].timestamp.strftime("%H:%M:%S") if ev[-1].timestamp else "?"
                print(f"     → {start}–{end} ({len(ev)} clips)")

    # Analyze continuous timelapse recordings
    timelapses = []
    for tl_path in timelapse_files:
        print(f"\n🎬 Timelapse: {tl_path}")
        tl = analyze_timelapse(tl_path, thumbnails_dir, interval=args.tl_interval)
        timelapses.append(tl)
        if tl.flagged_segments:
            print(f"  🚨 {len(tl.flagged_segments)} presence event(s):")
            for seg in tl.flagged_segments:
                print(f"     → t={seg['start']:.0f}s–{seg['end']:.0f}s "
                      f"(deviation {seg['score']:.1f}%)")
        else:
            print(f"  ✅ No sustained deviations from the background "
                  f"(max {tl.max_score:.1f}%, {tl.frames_sampled} frames sampled)")

    # Generate report
    print(f"\n📊 Generating report...")
    output_path = generate_html_report(sessions, timelapses, args.output, thumbnails_dir)
    print(f"✅ Report saved: {output_path}")

    # Print summary
    total_flagged = sum(len(s.flagged_videos) for s in sessions)
    total_videos = sum(len(s.videos) for s in sessions)
    total_events = sum(len(s.intrusion_events) for s in sessions)
    total_tl_segments = sum(len(t.flagged_segments) for t in timelapses)
    print(f"\n{'='*50}")
    print(f"📊 Summary: {total_flagged} flagged / {total_videos} trigger recordings "
          f"({total_events} correlated events)")
    if timelapses:
        print(f"🎬 Timelapse: {total_tl_segments} presence event(s) across "
              f"{len(timelapses)} recording(s)")
    if total_flagged or total_tl_segments:
        print(f"⚠️  Review the flagged events in the report!")
    else:
        print(f"✅ No intrusions detected across all recordings.")


if __name__ == "__main__":
    main()
