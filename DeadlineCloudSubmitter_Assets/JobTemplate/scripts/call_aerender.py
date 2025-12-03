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

import argparse
import logging
import os
import pathlib
import subprocess
import sys
from typing import Tuple

_logger = logging.getLogger("call_aerender")
_logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_logger.addHandler(_handler)
_logger.propagate = False


def str_to_bool(val: str) -> bool:
    return str(val).lower() in ("1", "true", "yes", "on")


def select_task_range(frames: str, chunk_size: int, index: int) -> Tuple[int, int]:
    """
    Given the full frames string (e.g. "0-1000"), and the chunk size + index, compute
    the subset range for this task. If chunk_size/index are None, return the full range.
    """
    if not frames:
        raise ValueError("frames string cannot be empty")
    if "-" not in frames:
        start = end = int(frames)
        return start, end

    full_start_str, full_end_str = frames.split("-", 1)
    full_start = int(full_start_str)
    full_end = int(full_end_str)
    if chunk_size is None or index is None:
        return full_start, full_end

    total_frames = full_end - full_start + 1
    chunks = max(1, (total_frames + chunk_size - 1) // chunk_size)
    if index < 0 or index >= chunks:
        raise ValueError(f"Invalid chunk index {index} for {chunks} chunks")

    start = full_start + index * chunk_size
    end = min(full_end, start + chunk_size - 1)
    return start, end


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deadline Cloud After Effects call_aerender wrapper")

    # existing submitter args
    p.add_argument("--deadline-cloud", action="store_true")
    p.add_argument("--project", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--frames", required=True)
    p.add_argument("--chunk-size", type=int, default=None)
    p.add_argument("--index", type=int, default=None)
    p.add_argument("--render_queue_index", type=int, default=1)
    p.add_argument("--ignore_missing_dependencies", default="OFF")
    p.add_argument("--multi_frame_rendering", default="ON")
    p.add_argument("--max-cpu-usage-percentage", type=int, default=90)
    p.add_argument("--graphics_memory_usage", type=int, default=100)
    p.add_argument("--disable_multi_frame_rendering", default="OFF")
    p.add_argument("--job_script_dir", default=None)
    p.add_argument("--conda_packages", default=None)

    # NEW STMPO args (values are passed as strings from OpenJD)
    p.add_argument("--concurrency", default="-1")
    p.add_argument("--max_concurrency", default="24")
    p.add_argument("--ram_per_process_gb", default="32.0")
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
        sys.executable,
        str(stmpo_path),
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