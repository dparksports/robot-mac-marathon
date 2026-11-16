#!/usr/bin/env python3
"""
Recover corrupted MOV files using a working reference MOV file.

Strategy:
1. Parse the reference file's moov atom to extract SPS and PPS NAL units
   from the avcC (H.264 decoder configuration) box
2. For each corrupted file, extract AVCC NAL units from mdat
3. Prepend the SPS + PPS (as Annex-B) before the video data
4. Use ffmpeg to remux the Annex-B stream into a valid MOV container
"""

import struct
import sys
import os
import subprocess

FFMPEG = "/Users/f20/Downloads/ffmpeg-darwin-arm64"
REFERENCE = "timelapse_2026-03-10_00-17-09.mov"


def parse_atoms(data, offset=0, end=None):
    """Recursively parse MOV/MP4 atoms."""
    if end is None:
        end = len(data)
    atoms = []
    pos = offset
    while pos < end - 8:
        sz = struct.unpack('>I', data[pos:pos+4])[0]
        atype = data[pos+4:pos+8]
        if sz == 0:
            sz = end - pos
        elif sz == 1:
            if pos + 16 > end:
                break
            sz = struct.unpack('>Q', data[pos+8:pos+16])[0]
        if sz < 8 or pos + sz > end:
            break
        atoms.append({
            'type': atype,
            'offset': pos,
            'size': sz,
            'data_offset': pos + 8,
            'data': data[pos+8:pos+sz]
        })
        pos += sz
    return atoms


# Container atoms that hold child atoms
CONTAINER_ATOMS = {b'moov', b'trak', b'mdia', b'minf', b'stbl'}


def find_atom(data, path, offset=0, end=None):
    """Find a nested atom by path, e.g. [b'moov', b'trak', b'mdia', ...]"""
    if end is None:
        end = len(data)
    atoms = parse_atoms(data, offset, end)
    target = path[0]
    for atom in atoms:
        if atom['type'] == target:
            if len(path) == 1:
                return atom
            else:
                # Parse children (skip 8-byte header for container atoms)
                return find_atom(data, path[1:], atom['data_offset'], atom['offset'] + atom['size'])
    return None


def extract_sps_pps_from_reference(ref_path):
    """Extract SPS and PPS NAL units from a reference MOV file's avcC box."""
    with open(ref_path, 'rb') as f:
        data = f.read()

    print(f"Reference file: {ref_path} ({len(data)} bytes)")

    # Find moov atom
    moov = find_atom(data, [b'moov'])
    if not moov:
        print("ERROR: No moov atom in reference file!")
        return None, None

    # Find the stsd atom (sample description) in the video track
    # Path: moov → trak → mdia → minf → stbl → stsd
    stsd = find_atom(data, [b'moov', b'trak', b'mdia', b'minf', b'stbl', b'stsd'])
    if not stsd:
        print("ERROR: No stsd atom found in reference file!")
        return None, None

    # The stsd atom contains sample entries. For H.264, there's an 'avc1' entry
    # stsd format: version(1) + flags(3) + entry_count(4) + entries
    stsd_data = stsd['data']
    print(f"  stsd data length: {len(stsd_data)}")

    # Search for 'avcC' box anywhere in stsd data
    avcC_pos = stsd_data.find(b'avcC')
    if avcC_pos < 0:
        print("ERROR: No avcC box found in stsd!")
        # Try searching the entire moov
        moov_data = data[moov['data_offset']:moov['offset']+moov['size']]
        avcC_pos = moov_data.find(b'avcC')
        if avcC_pos < 0:
            print("ERROR: No avcC box found anywhere in moov!")
            return None, None
        # Found in moov — adjust
        avcC_atom_start = moov['data_offset'] + avcC_pos - 4
        avcC_size = struct.unpack('>I', data[avcC_atom_start:avcC_atom_start+4])[0]
        avcC_data = data[avcC_atom_start+8:avcC_atom_start+avcC_size]
    else:
        # avcC_pos points to the 'avcC' type field; the size is 4 bytes before
        avcC_atom_start = stsd['data_offset'] + avcC_pos - 4
        avcC_size = struct.unpack('>I', data[avcC_atom_start:avcC_atom_start+4])[0]
        avcC_data = data[avcC_atom_start+8:avcC_atom_start+avcC_size]

    print(f"  avcC box: {avcC_size} bytes")

    # Parse AVC Decoder Configuration Record
    # configurationVersion(1), AVCProfileIndication(1), profile_compatibility(1),
    # AVCLevelIndication(1), lengthSizeMinusOne(1, lower 2 bits), numOfSPS(1, lower 5 bits)
    if len(avcC_data) < 7:
        print("ERROR: avcC data too short!")
        return None, None

    config_version = avcC_data[0]
    profile = avcC_data[1]
    compat = avcC_data[2]
    level = avcC_data[3]
    length_size = (avcC_data[4] & 0x03) + 1
    num_sps = avcC_data[5] & 0x1F

    print(f"  AVC config: version={config_version}, profile={profile}, level={level}, "
          f"NAL length size={length_size}, num SPS={num_sps}")

    pos = 6
    sps_list = []
    for i in range(num_sps):
        sps_len = struct.unpack('>H', avcC_data[pos:pos+2])[0]
        sps = avcC_data[pos+2:pos+2+sps_len]
        sps_list.append(sps)
        sps_type = sps[0] & 0x1F
        print(f"  SPS #{i}: {sps_len} bytes, NAL type={sps_type}")
        pos += 2 + sps_len

    num_pps = avcC_data[pos]
    pos += 1
    pps_list = []
    for i in range(num_pps):
        pps_len = struct.unpack('>H', avcC_data[pos:pos+2])[0]
        pps = avcC_data[pos+2:pos+2+pps_len]
        pps_list.append(pps)
        pps_type = pps[0] & 0x1F
        print(f"  PPS #{i}: {pps_len} bytes, NAL type={pps_type}")
        pos += 2 + pps_len

    if not sps_list or not pps_list:
        print("ERROR: No SPS/PPS found!")
        return None, None

    return sps_list, pps_list


def extract_and_recover(input_path, output_path, sps_list, pps_list):
    """Extract AVCC NALs from corrupted file, prepend SPS/PPS, remux with ffmpeg."""
    fsize = os.path.getsize(input_path)
    print(f"\n{'='*60}")
    print(f"Recovering: {input_path} ({fsize / 1_000_000:.1f} MB)")

    if fsize < 50000:
        print(f"  ✗ Too small ({fsize} bytes) — skipping.")
        return False

    h264_path = output_path + ".h264"
    MDAT_OFFSET = 36  # ftyp(20) + wide(8) + mdat_header(8)

    nal_counts = {}
    total_nals = 0

    with open(input_path, 'rb') as fin, open(h264_path, 'wb') as fout:
        # Write SPS and PPS first (Annex-B format)
        for sps in sps_list:
            fout.write(b'\x00\x00\x00\x01')
            fout.write(sps)
            print(f"  Wrote SPS ({len(sps)} bytes)")
        for pps in pps_list:
            fout.write(b'\x00\x00\x00\x01')
            fout.write(pps)
            print(f"  Wrote PPS ({len(pps)} bytes)")

        # Now extract all NAL units from mdat
        fin.seek(MDAT_OFFSET)
        buf = b''
        chunk_size = 16 * 1024 * 1024
        bytes_processed = 0

        while True:
            new_data = fin.read(chunk_size)
            if not new_data and len(buf) < 4:
                break
            buf = buf + new_data

            pos = 0
            while pos < len(buf) - 4:
                nal_size = struct.unpack('>I', buf[pos:pos+4])[0]

                if nal_size == 0:
                    pos += 4
                    continue
                if nal_size > 50_000_000:
                    pos += 1
                    continue

                if pos + 4 + nal_size > len(buf):
                    break

                nal_data = buf[pos+4:pos+4+nal_size]
                nal_type = nal_data[0] & 0x1F
                forbidden = (nal_data[0] >> 7) & 1

                if forbidden == 0 and 1 <= nal_type <= 23:
                    nal_counts[nal_type] = nal_counts.get(nal_type, 0) + 1

                    # For IDR frames (type 5), re-inject SPS+PPS before them
                    # to create proper random access points
                    if nal_type == 5:
                        for sps in sps_list:
                            fout.write(b'\x00\x00\x00\x01')
                            fout.write(sps)
                        for pps in pps_list:
                            fout.write(b'\x00\x00\x00\x01')
                            fout.write(pps)

                    fout.write(b'\x00\x00\x00\x01')
                    fout.write(nal_data)
                    total_nals += 1

                pos += 4 + nal_size

            buf = buf[pos:]
            bytes_processed += pos

            if bytes_processed > 0 and bytes_processed % (100 * 1024 * 1024) < chunk_size:
                print(f"    Processed {bytes_processed / 1_000_000:.0f} MB, {total_nals} NALs...")

            if not new_data:
                break

    print(f"  NAL types found: {dict(sorted(nal_counts.items()))}")
    print(f"  (1=P-slice, 5=IDR/keyframe, 6=SEI)")
    print(f"  Total NALs: {total_nals}")

    if total_nals < 2:
        print("  ✗ Too few NAL units.")
        os.remove(h264_path)
        return False

    # Remux with ffmpeg
    print(f"  Remuxing → {output_path}")
    result = subprocess.run([
        FFMPEG, '-y',
        '-analyzeduration', '200M',
        '-probesize', '200M',
        '-f', 'h264',
        '-framerate', '1',
        '-i', h264_path,
        '-c:v', 'copy',
        '-movflags', '+faststart',
        '-tag:v', 'avc1',
        output_path
    ], capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        # Try re-encode as fallback
        print(f"  Copy failed, trying re-encode...")
        stderr_short = result.stderr[-200:] if result.stderr else ""
        print(f"    Error: {stderr_short}")

        result = subprocess.run([
            FFMPEG, '-y',
            '-analyzeduration', '200M',
            '-probesize', '200M',
            '-f', 'h264',
            '-framerate', '1',
            '-i', h264_path,
            '-c:v', 'libx264',
            '-crf', '18',
            '-preset', 'fast',
            '-movflags', '+faststart',
            output_path
        ], capture_output=True, text=True, timeout=600)

    os.remove(h264_path)

    if result.returncode == 0 and os.path.exists(output_path):
        out_size = os.path.getsize(output_path)
        if out_size > 1000:
            print(f"  ✓ RECOVERED! {output_path} ({out_size / 1_000_000:.1f} MB)")
            return True

    print(f"  ✗ Recovery failed.")
    if result.stderr:
        print(f"    {result.stderr[-300:]}")
    return False


if __name__ == '__main__':
    # Step 1: Extract SPS/PPS from reference file
    print("Step 1: Extracting SPS/PPS from reference file...")
    sps_list, pps_list = extract_sps_pps_from_reference(REFERENCE)
    if not sps_list or not pps_list:
        print("FATAL: Could not extract SPS/PPS from reference file.")
        sys.exit(1)

    # Step 2: Recover each corrupted file
    corrupted = [f for f in sorted(os.listdir('.'))
                 if f.startswith('timelapse_') and f.endswith('.mov') and f != REFERENCE]

    print(f"\nStep 2: Attempting recovery of {len(corrupted)} file(s)...")
    recovered = 0

    for mov in corrupted:
        out = f"recovered_{mov}"
        if extract_and_recover(mov, out, sps_list, pps_list):
            recovered += 1

    print(f"\n{'='*60}")
    print(f"Recovery complete: {recovered}/{len(corrupted)} files recovered.")
