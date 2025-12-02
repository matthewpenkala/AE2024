"""
call_aerender.py (STMPO-enabled)

This script is invoked by the Deadline Cloud After Effects submitter job templates.

Original behavior:
- Compute the frame range for this task (supports chunking).
- Call aerender directly.

New behavior:
- Compute the frame range for this task (same as before).
- Invoke stmpo_wrapper.py with that range to spawn N parallel aerender processes locally.

Notes:
- Existing submitter parameters (IgnoreMissingDependencies, MaxCpuUsagePercentage, GraphicsMemoryUsage)
  are preserved for backward compatibility, but are NOT used by STMPO unless you extend stmpo_wrapper.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import pathlib
import platform
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

_logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# -----------------------------
# Helpers for frame parsing
# -----------------------------

@dataclass
class Chunk:
    index: int
    start_frame: int
    end_frame: int

def parse_frames(frames_spec: str) -> List[int]:
    """
    Parse a Deadline/OpenJD framespec like:
      "0-99" or "0-99,120-200"
    into a sorted unique list of ints.
    """
    frames: List[int] = []
    for part in frames_spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = int(a)
            end = int(b)
            frames.extend(range(start, end + 1))
        else:
            frames.append(int(part))
    return sorted(set(frames))

def build_chunks(frames: List[int], chunk_size: int) -> List[Chunk]:
    """
    Chunks a list of frames into sequential ranges, each up to chunk_size frames.
    """
    chunks: List[Chunk] = []
    if not frames:
        return chunks
    start = frames[0]
    prev = start
    count = 1
    chunk_start = start
    chunk_index = 0
    for f in frames[1:]:
        if count >= chunk_size or f != prev + 1:
            chunks.append(Chunk(chunk_index, chunk_start, prev))
            chunk_index += 1
            chunk_start = f
            count = 1
        else:
            count += 1
        prev = f
    chunks.append(Chunk(chunk_index, chunk_start, prev))
    return chunks

def select_task_range(frames_spec: str, chunk_size: Optional[int], index: Optional[int]) -> Tuple[int, int]:
    frames = parse_frames(frames_spec)
    if not frames:
        raise ValueError(f"No frames parsed from spec: {frames_spec}")

    if chunk_size and index is not None:
        chunks = build_chunks(frames, int(chunk_size))
        match = next((c for c in chunks if c.index == int(index)), None)
        if not match:
            raise ValueError(f"Index {index} not found in chunks. Frames={frames_spec}, chunk_size={chunk_size}")
        return match.start_frame, match.end_frame

    # No chunking => full range
    return frames[0], frames[-1]

def str_to_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")

# -----------------------------
# Main
# -----------------------------

def parse_args():
    p = argparse.ArgumentParser()

    # existing submitter args
    p.add_argument("--deadline-cloud", action="store_true")
    p.add_argument("--project", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--frames", required=True)
    p.add_argument("--chunk-size", type=int, default=None)
    p.add_argument("--index", type=int, default=None)
    p.add_argument("--render_queue_index", type=int, default=1)
    p.add_argument("--ignore_missing_dependencies", default="OFF")
    p.add_argument("--multi-frame-rendering", default="ON")
    p.add_argument("--max-cpu-usage-percentage", type=int, default=90)
    p.add_argument("--graphics_memory_usage", type=int, default=100)
    p.add_argument("--disable_multi_frame_rendering", default="OFF")
    p.add_argument("--job_script_dir", default=None)
    p.add_argument("--conda_packages", default=None)

    # NEW STMPO args (values are passed as strings from OpenJD)
    p.add_argument("--concurrency", default="-1")
    p.add_argument("--max_concurrency", default="48")
    p.add_argument("--ram_per_process_gb", default="10.0")
    p.add_argument("--mfr_threads", default="2")
    p.add_argument("--disable_mfr", default="false")
    p.add_argument("--numa_map", default="")
    p.add_argument("--disable_affinity", default="true")
    p.add_argument("--spawn_delay", default="2.0")
    p.add_argument("--child_grace_sec", default="10")
    p.add_argument("--no_kill_on_fail", default="false")
    p.add_argument("--output_is_pattern", default="false")
    p.add_argument("--aerender_path", default=None)
    p.add_argument("--env_file", default=None)
    p.add_argument("--log_file", default=None)

    return p.parse_args()

def main():
    args = parse_args()

    # Validate stmpo_wrapper location (same folder as this script, unless overridden)
    script_dir = pathlib.Path(__file__).parent
    stmpo_path = script_dir / "stmpo_wrapper.py"
    if not stmpo_path.exists():
        raise FileNotFoundError(f"stmpo_wrapper.py not found next to call_aerender.py: {stmpo_path}")

    # Compute frame range for this task
    start_frame, end_frame = select_task_range(args.frames, args.chunk_size, args.index)
    _logger.info(f"[call_aerender] Task frames {start_frame}-{end_frame} (index={args.index}, chunk_size={args.chunk_size})")

    # Decide MFR enable/disable
    disable_mfr = (
        str_to_bool(args.disable_mfr)
        or str(args.disable_multi_frame_rendering).upper() == "ON"
        or str(args.multi_frame_rendering).upper() == "OFF"
    )

    logical_cpus = os.cpu_count() or 0
    auto_disable_affinity = logical_cpus > 64
    user_disable_affinity = str_to_bool(args.disable_affinity)
    if auto_disable_affinity:
        _logger.info(
            f"[call_aerender] Auto-disabling affinity (logical_cpus={logical_cpus} > 64)."
        )

    disable_affinity = user_disable_affinity or auto_disable_affinity
    enable_affinity = not disable_affinity and not auto_disable_affinity

    # Build STMPO command
    # Resolve NUMA map path: prefer explicit, else job-attached map.
    numa_path = args.numa_map
    try:
        if (not numa_path) or (not pathlib.Path(numa_path).exists()):
            if args.job_script_dir:
                cand = pathlib.Path(args.job_script_dir) / 'numa_map.json'
                if cand.exists():
                    _logger.info(f'[call_aerender] Using job-attached NUMA map: {cand}')
                    numa_path = str(cand)
    except Exception:
        pass
    cmd = [
        sys.executable, str(stmpo_path),
        "--project", args.project,
        "--rqindex", str(args.render_queue_index),
        "--output", args.output,
        "--start", str(start_frame),
        "--end", str(end_frame),
        "--concurrency", str(args.concurrency),
        "--max_concurrency", str(args.max_concurrency),
        "--ram_per_process_gb", str(args.ram_per_process_gb),
        "--mfr_threads", str(args.mfr_threads),
        "--numa_map", str(numa_path),
        "--spawn_delay", str(args.spawn_delay),
        "--child_grace_sec", str(args.child_grace_sec),
    ]

    if disable_mfr:
        cmd += ["--disable_mfr"]

    if disable_affinity:
        cmd += ["--disable_affinity"]
    elif enable_affinity:
        cmd += ["--enable_affinity"]

    if str_to_bool(args.no_kill_on_fail):
        cmd += ["--no_kill_on_fail"]

    if str_to_bool(args.output_is_pattern):
        cmd += ["--output_is_pattern"]

    if args.aerender_path:
        cmd += ["--aerender_path", args.aerender_path]

    if args.env_file and str(args.env_file).upper() not in ("NONE","__NONE__","NULL","FALSE","0"):
        cmd += ["--env_file", args.env_file]
    if args.log_file and str(args.log_file).upper() not in ("NONE","__NONE__","NULL","FALSE","0"):
        cmd += ["--log_file", args.log_file]
    _logger.info("[call_aerender] Launching STMPO wrapper:")
    _logger.info(" ".join(cmd))

    # Run STMPO wrapper. Let it stream logs to stdout, which Deadline captures.
    rc = subprocess.call(cmd)
    if rc != 0:
        _logger.error(f"[call_aerender] STMPO wrapper returned {rc}")
    sys.exit(rc)

if __name__ == "__main__":
    main()