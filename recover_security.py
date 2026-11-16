#!/usr/bin/env python3
"""
Recover corrupted security MOV files (HEVC/H.265 + AAC interleaved).

These files have ftyp + mdat but are missing the moov atom.
The mdat contains interleaved video (HEVC) and audio (AAC) chunks.
Without the moov we can't tell which bytes are which.

Strategy:
1. Extract VPS/SPS/PPS from a working reference file's hvcC box
2. For each corrupt file, scan the mdat parsing 4-byte length-prefixed NALs
3. Accept NALs that look like valid HEVC, skip forward when we hit audio data
4. Write valid HEVC NALs as Annex-B stream
5. Use ffmpeg to remux into a playable MOV
"""

import struct
import sys
import os
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(SCRIPT_DIR, "ffmpeg")
FFPROBE = os.path.join(SCRIPT_DIR, "ffprobe")

REFERENCE = os.path.join(
    SCRIPT_DIR,
    "security_2026-04-10_14-14-23",
    "trigger_2026-04-10_14-28-13.mov"
)

CORRUPT_FILES = [
    "security_2026-04-03_23-34-01/trigger_2026-04-03_23-34-02.mov",
    "security_2026-04-04_00-34-05/trigger_2026-04-04_00-34-05.mov",
    "security_2026-04-04_03-34-08/trigger_2026-04-04_03-34-08.mov",
    "security_2026-04-07_22-10-21/trigger_2026-04-07_22-10-21.mov",
    "security_2026-04-08_08-33-02/trigger_2026-04-08_08-33-02.mov",
    "security_2026-04-08_22-41-13/trigger_2026-04-09_10-13-07.mov",
    "security_2026-04-09_14-45-42/trigger_2026-04-09_14-45-42.mov",
    "security_2026-04-09_15-45-45/trigger_2026-04-09_15-45-45.mov",
    "security_2026-04-09_16-45-48/trigger_2026-04-09_16-45-49.mov",
    "security_2026-04-09_19-51-12/trigger_2026-04-09_19-51-12.mov",
    "security_2026-04-09_20-51-01/trigger_2026-04-09_20-51-02.mov",
    "security_2026-04-09_22-51-05/trigger_2026-04-09_22-51-05.mov",
]

# HEVC NAL types
HEVC_NAL_TYPES = {
    0: "TRAIL_N", 1: "TRAIL_R", 2: "TSA_N", 3: "TSA_R",
    4: "STSA_N", 5: "STSA_R", 6: "RADL_N", 7: "RADL_R",
    8: "RASL_N", 9: "RASL_R",
    16: "BLA_W_LP", 17: "BLA_W_RADL", 18: "BLA_N_LP",
    19: "IDR_W_RADL", 20: "IDR_N_LP", 21: "CRA",
    32: "VPS", 33: "SPS", 34: "PPS",
    35: "AUD", 36: "EOS", 37: "EOB", 38: "FILLER",
    39: "SEI_PREFIX", 40: "SEI_SUFFIX",
}

# Valid HEVC NAL types for video slices and parameter sets
VALID_VIDEO_TYPES = set(range(0, 22)) | {32, 33, 34, 35, 39, 40}


def extract_hevc_params(ref_path):
    """Extract VPS, SPS, PPS from reference file's hvcC box."""
    with open(ref_path, 'rb') as f:
        data = f.read()

    moov_idx = data.find(b'moov')
    if moov_idx < 0:
        print("ERROR: No moov in reference")
        return None

    hvcc_idx = data.find(b'hvcC', moov_idx)
    if hvcc_idx < 0:
        print("ERROR: No hvcC in reference")
        return None

    hvcc_atom_start = hvcc_idx - 4
    hvcc_size = struct.unpack('>I', data[hvcc_atom_start:hvcc_atom_start+4])[0]
    hvcc_data = data[hvcc_idx+4:hvcc_atom_start+hvcc_size]

    if len(hvcc_data) < 23:
        print("ERROR: hvcC too short")
        return None

    num_arrays = hvcc_data[22]
    params = {}
    pos = 23

    for _ in range(num_arrays):
        if pos >= len(hvcc_data):
            break
        nal_type = hvcc_data[pos] & 0x3F
        pos += 1
        num_nalus = struct.unpack('>H', hvcc_data[pos:pos+2])[0]
        pos += 2
        nal_list = []
        for _ in range(num_nalus):
            nal_len = struct.unpack('>H', hvcc_data[pos:pos+2])[0]
            pos += 2
            nal_list.append(hvcc_data[pos:pos+nal_len])
            pos += nal_len
        params[nal_type] = nal_list
        tname = HEVC_NAL_TYPES.get(nal_type, f"type_{nal_type}")
        print(f"  {tname}: {[len(n) for n in nal_list]} bytes")

    return params


def make_annexb_header(params):
    """Create Annex-B VPS+SPS+PPS header."""
    out = b''
    for nal_type in [32, 33, 34]:  # VPS, SPS, PPS
        for nal in params.get(nal_type, []):
            out += b'\x00\x00\x00\x01' + nal
    return out


def is_valid_hevc_nal(data, offset, nal_size):
    """Check if bytes at offset look like a valid HEVC NAL unit."""
    if nal_size < 2 or nal_size > 20_000_000:
        return False

    byte0 = data[offset]
    byte1 = data[offset + 1]

    forbidden = (byte0 >> 7) & 1
    if forbidden != 0:
        return False

    nal_type = (byte0 >> 1) & 0x3F
    nuh_tid = byte1 & 0x07

    if nuh_tid == 0:  # temporal_id must be >= 1
        return False

    if nal_type not in VALID_VIDEO_TYPES:
        return False

    return True


def extract_and_recover(input_path, output_path, params):
    """Extract HEVC NALs from interleaved mdat, handling audio chunks."""
    fsize = os.path.getsize(input_path)
    print(f"\n{'='*60}")
    print(f"Recovering: {input_path} ({fsize / 1_000_000:.1f} MB)")

    if fsize < 1000:
        print(f"  ✗ Too small ({fsize} bytes)")
        return False

    hevc_path = output_path + ".hevc"
    annexb_header = make_annexb_header(params)

    MDAT_OFFSET = 36  # ftyp(20) + wide(8) + mdat_header(8)

    nal_counts = {}
    total_nals = 0
    audio_skips = 0
    bytes_written = 0

    with open(input_path, 'rb') as fin:
        fin.seek(0, 2)
        file_size = fin.tell()
        fin.seek(MDAT_OFFSET)

        # Read entire mdat into memory for faster processing
        mdat = fin.read()

    with open(hevc_path, 'wb') as fout:
        # Write initial VPS+SPS+PPS
        fout.write(annexb_header)
        bytes_written += len(annexb_header)

        pos = 0
        end = len(mdat)
        last_progress = 0

        while pos < end - 6:
            # Try to read a 4-byte length-prefixed NAL
            if pos + 4 > end:
                break

            nal_size = struct.unpack('>I', mdat[pos:pos+4])[0]

            # Check if this looks like a valid HEVC NAL
            if (nal_size >= 2 and
                nal_size <= 20_000_000 and
                pos + 4 + nal_size <= end and
                is_valid_hevc_nal(mdat, pos + 4, nal_size)):

                nal_data = mdat[pos+4:pos+4+nal_size]
                nal_type = (nal_data[0] >> 1) & 0x3F

                nal_counts[nal_type] = nal_counts.get(nal_type, 0) + 1

                # Re-inject VPS+SPS+PPS before IDR frames
                if nal_type in (19, 20):
                    fout.write(annexb_header)
                    bytes_written += len(annexb_header)

                # Write as Annex-B
                fout.write(b'\x00\x00\x00\x01')
                fout.write(nal_data)
                bytes_written += 4 + len(nal_data)
                total_nals += 1

                pos += 4 + nal_size
            else:
                # Not a valid HEVC NAL — this is likely audio data.
                # Scan forward to find the next valid HEVC NAL.
                # Audio chunks in these files are typically a few KB.
                # We scan byte-by-byte looking for a valid NAL pattern.
                scan_start = pos
                pos += 1
                found = False

                # Scan up to 1MB forward looking for next valid NAL
                scan_limit = min(pos + 1_000_000, end - 6)
                while pos < scan_limit:
                    candidate_size = struct.unpack('>I', mdat[pos:pos+4])[0]
                    if (candidate_size >= 2 and
                        candidate_size <= 20_000_000 and
                        pos + 4 + candidate_size <= end and
                        is_valid_hevc_nal(mdat, pos + 4, candidate_size)):

                        # Verify: after this NAL, is there another valid NAL?
                        # This reduces false positives
                        next_pos = pos + 4 + candidate_size
                        if next_pos + 6 <= end:
                            next_size = struct.unpack('>I', mdat[next_pos:next_pos+4])[0]
                            if (next_size >= 2 and
                                next_size <= 20_000_000 and
                                next_pos + 4 + next_size <= end and
                                is_valid_hevc_nal(mdat, next_pos + 4, next_size)):
                                found = True
                                audio_skips += 1
                                break
                        # Also accept if we're near end of file
                        elif next_pos >= end - 100:
                            found = True
                            audio_skips += 1
                            break

                    pos += 1

                if not found:
                    # Couldn't find next NAL within 1MB — try bigger skip
                    pos = scan_start + 1
                    # Jump forward in larger steps
                    while pos < end - 6:
                        candidate_size = struct.unpack('>I', mdat[pos:pos+4])[0]
                        if (candidate_size >= 2 and
                            candidate_size <= 20_000_000 and
                            pos + 4 + candidate_size <= end and
                            is_valid_hevc_nal(mdat, pos + 4, candidate_size)):
                            next_pos = pos + 4 + candidate_size
                            if next_pos + 6 <= end:
                                next_size = struct.unpack('>I', mdat[next_pos:next_pos+4])[0]
                                if (next_size >= 2 and
                                    next_size <= 20_000_000 and
                                    next_pos + 4 + next_size <= end and
                                    is_valid_hevc_nal(mdat, next_pos + 4, next_size)):
                                    audio_skips += 1
                                    break
                        pos += 1

            # Progress reporting
            mb_done = (pos + MDAT_OFFSET) / 1_000_000
            if int(mb_done / 50) > last_progress:
                last_progress = int(mb_done / 50)
                print(f"    {mb_done:.0f} MB processed, {total_nals} NALs, {audio_skips} audio skips")

    named_counts = {HEVC_NAL_TYPES.get(k, f"type_{k}"): v
                    for k, v in sorted(nal_counts.items())}
    print(f"  NAL types: {named_counts}")
    print(f"  Total video NALs: {total_nals}, audio chunks skipped: {audio_skips}")
    print(f"  Video data extracted: {bytes_written / 1_000_000:.1f} MB")

    if total_nals < 10:
        print("  ✗ Too few NAL units.")
        if os.path.exists(hevc_path):
            os.remove(hevc_path)
        return False

    # Remux with ffmpeg
    print(f"  Remuxing with ffmpeg → {output_path}")
    result = subprocess.run([
        FFMPEG, '-y',
        '-analyzeduration', '500M',
        '-probesize', '500M',
        '-r', '30',
        '-f', 'hevc',
        '-i', hevc_path,
        '-c:v', 'copy',
        '-movflags', '+faststart',
        '-tag:v', 'hvc1',
        output_path
    ], capture_output=True, text=True, timeout=1200)

    if os.path.exists(hevc_path):
        os.remove(hevc_path)

    if result.returncode == 0 and os.path.exists(output_path):
        out_size = os.path.getsize(output_path)
        if out_size > 1000:
            probe = subprocess.run(
                [FFPROBE, '-v', 'error', '-show_entries',
                 'format=duration', '-of', 'csv=p=0', output_path],
                capture_output=True, text=True, timeout=30
            )
            duration = probe.stdout.strip() if probe.returncode == 0 else "?"
            print(f"  ✓ RECOVERED! ({out_size / 1_000_000:.1f} MB, {duration}s)")
            return True

    print(f"  ✗ Recovery failed.")
    if result.stderr:
        print(f"    {result.stderr[-400:]}")
    return False


if __name__ == '__main__':
    os.chdir(SCRIPT_DIR)

    print("Step 1: Extracting HEVC params from reference...")
    params = extract_hevc_params(REFERENCE)
    if not params:
        sys.exit(1)

    print(f"\nStep 2: Recovering {len(CORRUPT_FILES)} files...")
    recovered = 0
    failed = []

    for mov in CORRUPT_FILES:
        full_path = os.path.join(SCRIPT_DIR, mov)
        if not os.path.exists(full_path):
            print(f"\n  ⚠ Not found: {mov}")
            continue

        dirn = os.path.dirname(full_path)
        base = os.path.basename(full_path)
        out_path = os.path.join(dirn, f"recovered_{base}")

        if extract_and_recover(full_path, out_path, params):
            recovered += 1
        else:
            failed.append(mov)

    print(f"\n{'='*60}")
    print(f"Done: {recovered}/{len(CORRUPT_FILES)} recovered")
    if failed:
        print(f"Failed: {failed}")
