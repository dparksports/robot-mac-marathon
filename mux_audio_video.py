#!/usr/bin/env python3
"""
Mux recovered video files with their corresponding audio .m4a files.

For each recovered_trigger_*.mov, find the audio .m4a file from the same
session that covers the video's time range, compute the offset, and combine
them into a single MOV with both tracks.
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(SCRIPT_DIR, "ffmpeg")
FFPROBE = os.path.join(SCRIPT_DIR, "ffprobe")


def parse_timestamp(name):
    """Parse a timestamp like '2026-04-03_23-34-02' into datetime."""
    try:
        return datetime.strptime(name, "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def get_duration(path):
    """Get file duration in seconds via ffprobe."""
    result = subprocess.run(
        [FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', path],
        capture_output=True, text=True, timeout=10
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return None


def find_matching_audio(session_dir, trigger_time, video_duration):
    """
    Find the audio .m4a file that covers the trigger time.
    Returns (audio_path, offset_seconds) or (None, None).
    """
    audio_files = []
    for f in sorted(os.listdir(session_dir)):
        if f.startswith("audio_") and f.endswith(".m4a"):
            path = os.path.join(session_dir, f)
            size = os.path.getsize(path)
            if size < 100:  # skip empty/corrupt
                continue

            ts_str = f.replace("audio_", "").replace(".m4a", "")
            audio_start = parse_timestamp(ts_str)
            if audio_start is None:
                continue

            dur = get_duration(path)
            if dur is None or dur < 1:
                continue

            audio_files.append({
                'path': path,
                'start': audio_start,
                'duration': dur,
                'end': audio_start + timedelta(seconds=dur),
            })

    if not audio_files:
        return None, None

    # Find the audio file whose time range contains the trigger time
    for af in audio_files:
        if af['start'] <= trigger_time < af['end']:
            offset = (trigger_time - af['start']).total_seconds()
            return af['path'], offset

    # Fallback: if trigger matches audio start exactly (common case)
    for af in audio_files:
        diff = abs((trigger_time - af['start']).total_seconds())
        if diff < 5:  # within 5 seconds
            return af['path'], diff

    # Last resort: use the first audio file with 0 offset
    if audio_files:
        return audio_files[0]['path'], 0

    return None, None


def mux_video_audio(video_path, audio_path, audio_offset, output_path):
    """Combine video and audio into a single MOV."""
    video_dur = get_duration(video_path)
    if video_dur is None:
        print(f"    ✗ Can't get video duration")
        return False

    cmd = [
        FFMPEG, '-y',
        '-i', video_path,
        '-ss', str(audio_offset),
        '-i', audio_path,
        '-t', str(video_dur),
        '-map', '0:v:0',
        '-map', '1:a:0',
        '-c', 'copy',
        '-movflags', '+faststart',
        '-tag:v', 'hvc1',
        output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode == 0 and os.path.exists(output_path):
        out_size = os.path.getsize(output_path)
        out_dur = get_duration(output_path)
        if out_size > 1000:
            print(f"    ✓ Muxed! {out_size / 1e6:.1f} MB, {out_dur:.1f}s")
            return True

    print(f"    ✗ Mux failed")
    if result.stderr:
        print(f"      {result.stderr[-300:]}")
    return False


if __name__ == '__main__':
    os.chdir(SCRIPT_DIR)

    # Find all recovered video files
    recovered = []
    for direntry in sorted(os.listdir(SCRIPT_DIR)):
        if not direntry.startswith("security_") or not os.path.isdir(direntry):
            continue
        for f in os.listdir(direntry):
            if f.startswith("recovered_trigger_") and f.endswith(".mov"):
                recovered.append(os.path.join(direntry, f))

    print(f"Found {len(recovered)} recovered video files\n")

    success = 0
    skipped = 0

    for video_path in sorted(recovered):
        session_dir = os.path.dirname(video_path)
        basename = os.path.basename(video_path)

        # Extract trigger timestamp from filename
        ts_str = basename.replace("recovered_trigger_", "").replace(".mov", "")
        trigger_time = parse_timestamp(ts_str)
        if trigger_time is None:
            print(f"⚠ Can't parse timestamp: {basename}")
            skipped += 1
            continue

        video_dur = get_duration(video_path)
        print(f"{'='*60}")
        print(f"Video: {video_path}")
        print(f"  Trigger: {trigger_time}, Duration: {video_dur:.1f}s")

        # Find matching audio
        audio_path, offset = find_matching_audio(
            session_dir, trigger_time, video_dur
        )

        if audio_path is None:
            print(f"  ⚠ No matching audio found — skipping")
            skipped += 1
            continue

        print(f"  Audio: {os.path.basename(audio_path)}, offset: {offset:.1f}s")

        # Output: final_trigger_*.mov
        output_path = os.path.join(session_dir, basename.replace("recovered_", "final_"))
        if mux_video_audio(video_path, audio_path, offset, output_path):
            success += 1
        else:
            skipped += 1

    print(f"\n{'='*60}")
    print(f"Done: {success} muxed, {skipped} skipped")
