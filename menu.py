#!/usr/bin/env python3
"""
Robot Mac Marathon — Interactive Feature Menu
==============================================
Single entry point that lists every tool in the project, shows the options
available for each one, lets you customize them with prompted defaults, and
runs the tool for you.

Usage:
    python3 menu.py            # interactive menu
    python3 menu.py --list     # print all features + options, no prompts
"""

import glob
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(SCRIPT_DIR, ".venv", "bin", "python")


# ── Option types ──────────────────────────────────────────────────────────
# "str"   → prompted, value passed as:      --flag VALUE
# "float" / "int" → same, validated
# "bool"  → prompted as y/n, adds bare flag when yes (e.g. --no-motion)
# "files" → prompted as space-separated list, flag repeated per value
#           (used for --timelapse mov1.mov mov2.mov)
#
# An empty answer always accepts the default; default=None means "omit flag".

def newest(pattern):
    """Newest file matching pattern, or empty string."""
    files = sorted(glob.glob(os.path.join(SCRIPT_DIR, pattern)), key=os.path.getmtime)
    return os.path.basename(files[-1]) if files else ""


def python_for_analysis():
    """Prefer the venv python (numpy/pillow) for analysis tools."""
    return VENV_PYTHON if os.path.exists(VENV_PYTHON) else sys.executable


FEATURES = [
    # ── Record ────────────────────────────────────────────────────────────
    {
        "category": "Record",
        "title": "Continuous timelapse recording",
        "desc": ("Camera + mic record continuously into one timelapse_*.mov "
                 "(1 frame/sec + synchronized audio). Best when power is "
                 "plugged in. Compile-and-loop wrapper; Ctrl+C stops."),
        "cmd": ["./run_timelapse.sh"],
        "options": [],
    },
    {
        "category": "Record",
        "title": "Security mode recording (low power)",
        "desc": ("Mic records continuous hourly audio (~0.3W); any sound above "
                 "the threshold wakes the camera for a triggered clip. Runs "
                 "5-7+ days on battery. Ctrl+C stops gracefully."),
        "cmd": ["./run_security.sh", "--pass-through"],
        "options": [
            {"flag": "--threshold", "type": "float", "default": "-30",
             "prompt": "Sound trigger level in dB (more negative = more sensitive)"},
            {"flag": "--duration", "type": "int", "default": "60",
             "prompt": "Seconds of video to record per trigger"},
            {"flag": "--audio-chunk", "type": "int", "default": "60",
             "prompt": "Continuous audio chunk length in minutes"},
            {"flag": "--min-battery", "type": "int", "default": "5",
             "prompt": "Auto-shutdown battery percent"},
        ],
    },

    # ── Analyze ───────────────────────────────────────────────────────────
    {
        "category": "Analyze",
        "title": "Intrusion detection — security sessions",
        "desc": ("Scores every trigger_*.mov clip: motion & presence from "
                 "dense frame sampling, audio correlation against the "
                 "continuous .m4a recordings, file-size anomalies (corroborating "
                 "only), and clusters nearby flags into intrusion events. "
                 "Produces an HTML dashboard."),
        "cmd": None,  # python tool — resolved at run time
        "tool": "detect_intrusion.py",
        "options": [
            {"flag": "--sessions", "type": "files", "default": "",
             "prompt": "Session dirs (blank = auto-discover security_*/)"},
            {"flag": "--threshold", "type": "float", "default": "15",
             "prompt": "Changed-pixel % that flags motion/presence"},
            {"flag": "--size-threshold", "type": "float", "default": "12",
             "prompt": "File-size anomaly % (only trusted with audio confirmation)"},
            {"flag": "--cluster-window", "type": "float", "default": "300",
             "prompt": "Seconds between flagged clips to group into one event"},
            {"flag": "--no-motion", "type": "bool", "default": "n",
             "prompt": "Skip frame analysis (fast, file-size/audio only) [y/n]"},
            {"flag": "--output", "type": "str", "default": "intrusion_report.html",
             "prompt": "Report output path"},
        ],
    },
    {
        "category": "Analyze",
        "title": "Intrusion detection — timelapse recording",
        "desc": ("Background subtraction on a continuous timelapse_*.mov: "
                 "frames are sampled every N seconds and compared against the "
                 "median (empty-scene) frame; sustained deviations become "
                 "presence events with thumbnails."),
        "cmd": None,
        "tool": "detect_intrusion.py",
        "options": [
            {"flag": "--timelapse", "type": "files",
             "default": newest("timelapse_*.mov"),
             "prompt": "Timelapse .mov file(s), space-separated"},
            {"flag": "--tl-interval", "type": "float", "default": "30",
             "prompt": "Seconds between sampled frames"},
            {"flag": "--threshold", "type": "float", "default": "15",
             "prompt": "Deviation % that flags presence"},
            {"flag": "--output", "type": "str", "default": "intrusion_report.html",
             "prompt": "Report output path"},
        ],
    },
    {
        "category": "Analyze",
        "title": "Recording coverage summary",
        "desc": "Scans the folder for recordings and prints what time ranges are covered.",
        "cmd": None,
        "tool": "summarize_coverage.py",
        "options": [],
    },
    {
        "category": "Analyze",
        "title": "Update calendar view",
        "desc": "Rebuilds calendar.html with every session/recording it finds.",
        "cmd": None,
        "tool": "update_calendar.py",
        "options": [],
    },
    {
        "category": "Analyze",
        "title": "Generate DVR-style player",
        "desc": ("Parses flagged events from intrusion_report.html and rebuilds "
                 "player.html, a dashboard for scrubbing through recordings."),
        "cmd": None,
        "tool": "gen_player.py",
        "options": [],
    },

    # ── Repair ────────────────────────────────────────────────────────────
    {
        "category": "Repair",
        "title": "Recover corrupted timelapse MOV (H.264)",
        "desc": ("For recordings missing their moov atom after a crash: rebuilds "
                 "a playable stream using SPS/PPS from a healthy reference clip "
                 "plus ffmpeg. Needs a short reference .mov from the fixed script."),
        "cmd": None,
        "tool": "recover_mov.py",
        "options": [],
    },
    {
        "category": "Repair",
        "title": "Recover corrupted security MOV (HEVC)",
        "desc": ("Same idea for security-mode trigger clips (HEVC + AAC "
                 "interleaved): extracts VPS/SPS/PPS from a reference file and "
                 "reassembles valid recordings from raw mdat data."),
        "cmd": None,
        "tool": "recover_security.py",
        "options": [],
    },
    {
        "category": "Repair",
        "title": "Mux recovered video with its audio",
        "desc": "Pairs each recovered_trigger_*.mov with the covering audio .m4a and merges both tracks.",
        "cmd": None,
        "tool": "mux_audio_video.py",
        "options": [],
    },

    # ── View ──────────────────────────────────────────────────────────────
    {
        "category": "View",
        "title": "Open intrusion report",
        "desc": "Opens the latest intrusion_report*.html dashboard in your browser.",
        "cmd": None,
        "open_pattern": "intrusion_report*.html",
        "options": [],
    },
    {
        "category": "View",
        "title": "Open video player dashboard",
        "desc": "Opens player.html in your browser.",
        "cmd": ["open", "player.html"],
        "options": [],
    },
    {
        "category": "View",
        "title": "Open recording calendar",
        "desc": "Opens calendar.html in your browser.",
        "cmd": ["open", "calendar.html"],
        "options": [],
    },

    # ── Setup ─────────────────────────────────────────────────────────────
    {
        "category": "Setup",
        "title": "Install FFmpeg binaries (local, no Homebrew)",
        "desc": "Unpacks the bundled ffmpeg/ffprobe from ffmpeg-mac.zip into this folder.",
        "cmd": ["./install_ffmpeg.sh"],
        "options": [],
    },
    {
        "category": "Setup",
        "title": "Create/refresh Python venv (numpy + pillow)",
        "desc": "Creates .venv and installs the analysis dependencies used by detect_intrusion.py.",
        "cmd": None,
        "setup_venv": True,
        "options": [],
    },
]


# ── Helpers ───────────────────────────────────────────────────────────────

def feature_command(feature):
    """Full argv for a feature, or None for special-handled ones."""
    if feature.get("tool"):
        return [python_for_analysis(), feature["tool"]]
    return feature.get("cmd")


def needs_venv(feature):
    return bool(feature.get("tool") == "detect_intrusion.py")


def needs_ffmpeg(feature):
    return feature.get("tool") in ("detect_intrusion.py", "recover_mov.py",
                                   "recover_security.py", "mux_audio_video.py")


def prompt_option(opt):
    """Prompt for one option; return the list of argv pieces it contributes."""
    kind = opt.get("type", "str")
    default = opt.get("default", "")
    shown = default if default != "" else "(none)"
    if kind == "bool":
        answer = input(f"  {opt['prompt']} [{default}]: ").strip().lower() or default
        return [opt["flag"]] if answer in ("y", "yes") else []
    while True:
        raw = input(f"  {opt['prompt']} [{shown}]: ").strip()
        if not raw:
            raw = default
        if kind in ("float", "int"):
            try:
                (float if kind == "float" else int)(raw)
            except ValueError:
                print(f"    ✗ please enter a number")
                continue
        values = raw.split() if kind == "files" else [raw]
        pieces = []
        for v in values:
            if not v:
                continue
            pieces.extend([opt["flag"], v])
        return pieces


def run_feature(feature):
    # Special actions first
    if feature.get("setup_venv"):
        subprocess.run([sys.executable, "-m", "venv", ".venv"], cwd=SCRIPT_DIR)
        subprocess.run([VENV_PYTHON, "-m", "pip", "install", "numpy", "pillow"], cwd=SCRIPT_DIR)
        return
    if feature.get("open_pattern"):
        files = sorted(glob.glob(os.path.join(SCRIPT_DIR, feature["open_pattern"])),
                       key=os.path.getmtime)
        if not files:
            print("  ✗ no matching file found — run the analysis first")
            return
        subprocess.run(["open", os.path.basename(files[-1])], cwd=SCRIPT_DIR)
        return

    if needs_ffmpeg(feature) and not os.path.exists(os.path.join(SCRIPT_DIR, "ffmpeg")):
        print("  ⚠ FFmpeg not installed — it is required for this feature.")
        answer = input("  Install it now from the bundled zip? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            subprocess.run(["./install_ffmpeg.sh"], cwd=SCRIPT_DIR)
        else:
            return
    if needs_venv(feature) and not os.path.exists(VENV_PYTHON):
        print("  ⚠ .venv not found — analysis is much faster with numpy/pillow.")
        answer = input("  Create it now? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            subprocess.run([sys.executable, "-m", "venv", ".venv"], cwd=SCRIPT_DIR)
            subprocess.run([VENV_PYTHON, "-m", "pip", "install", "numpy", "pillow"], cwd=SCRIPT_DIR)
        else:
            print(f"  → continuing with {sys.executable} (slower pure-Python mode)")

    argv = feature_command(feature)
    if "--pass-through" in argv:
        argv = [a for a in argv if a != "--pass-through"]

    print(f"\n  ── {feature['title']} ──", flush=True)
    if feature["options"]:
        print("  Options — press Enter to accept each default:\n", flush=True)
        for opt in feature["options"]:
            argv.extend(prompt_option(opt))
    else:
        print("  (this tool takes no options)")
        answer = input("  Run it? [Y/n]: ").strip().lower()
        if answer not in ("", "y", "yes"):
            return

    print(f"\n  $ {' '.join(argv)}\n", flush=True)
    try:
        subprocess.run(argv, cwd=SCRIPT_DIR)
    except FileNotFoundError as e:
        print(f"  ✗ could not start: {e}")
    input("\n  [Enter] back to menu ")


# ── Menu rendering ────────────────────────────────────────────────────────

def print_list():
    print("Robot Mac Marathon — available features\n")
    category = None
    for i, f in enumerate(FEATURES, 1):
        if f["category"] != category:
            category = f["category"]
            print(f"{category}:")
        print(f"  {i}) {f['title']}")
        print(f"     {f['desc']}")
        if f["options"]:
            for opt in f["options"]:
                default = opt.get("default", "")
                if opt.get("type") == "bool":
                    hint = "on/off flag"
                elif default:
                    hint = f"default {default}"
                else:
                    hint = "no default"
                print(f"       • {opt['flag']:<18} {hint}")
        print()
    print("Everything is also runnable directly — see README.md for details.")


def main():
    os.chdir(SCRIPT_DIR)
    if "--list" in sys.argv:
        print_list()
        return

    while True:
        print("\n" + "=" * 60)
        print("  🤖 Robot Mac Marathon — Feature Menu")
        print("=" * 60)
        category = None
        for i, f in enumerate(FEATURES, 1):
            if f["category"] != category:
                category = f["category"]
                print(f"\n  {category}:")
            print(f"   {i:2d}) {f['title']}")
        print(f"\n    0) Quit")
        try:
            choice = input("\n  Choose a feature (number): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if choice == "0" or choice == "":
            if choice == "":
                continue
            break
        if not choice.isdigit() or not (1 <= int(choice) <= len(FEATURES)):
            print("  ✗ invalid choice")
            continue
        try:
            run_feature(FEATURES[int(choice) - 1])
        except KeyboardInterrupt:
            print("\n  (cancelled — back to menu)")
        except EOFError:
            break


if __name__ == "__main__":
    main()
