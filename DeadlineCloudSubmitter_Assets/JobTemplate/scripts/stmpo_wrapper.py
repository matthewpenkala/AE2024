# stmpo_wrapper.py
"""
STMPO After Effects Orchestrator

- Splits a single Deadline Cloud task frame range into N sub-ranges.
- Spawns N aerender.exe processes in parallel.
- Optionally assigns CPU affinity per process based on a NUMA/CPU map.
- Aggregates child logs into parent log with PID-prefixing.
- Fail-fast: if any child fails (and kill_on_fail is enabled), terminates siblings.

This script is invoked by call_aerender.py, not directly.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil


# -----------------------------
# Defaults / Constants
# -----------------------------

DEFAULT_AERENDER = r"E:\DCC\Adobe\Adobe After Effects 2024\Support Files\aerender.exe"
DEFAULT_NUMA_MAP = ""  # Job-attached or caller-provided path is preferred.
HEARTBEAT_SECONDS = 30


# -----------------------------
# Data structures
# -----------------------------

@dataclass
class ChildProc:
    popen: subprocess.Popen
    frame_range: Tuple[int, int]
    affinity: Optional[List[int]]
    psutil_proc: Optional[psutil.Process]
    start_time: float


# -----------------------------
# Utility helpers
# -----------------------------

def split_ranges(start: int, end: int, parts: int) -> List[Tuple[int, int]]:
    total = end - start + 1
    if parts <= 0:
        parts = 1
    if parts > total:
        parts = total

    base = total // parts
    rem = total % parts
    ranges: List[Tuple[int, int]] = []
    cur = start
    for i in range(parts):
        span = base + (1 if i < rem else 0)
        s = cur
        e = cur + span - 1
        ranges.append((s, e))
        cur = e + 1
    return ranges


def load_numa_nodes(json_path: str) -> Dict[str, List[int]]:
    """
    Load a simple JSON mapping of NUMA nodes/groups to CPU indices.
    Expected shapes that we tolerate:
        { "0": [0,1,2,...], "1": [64,65,...] }
        { "group_0": [0,1,...], "group_1": [64,65,...] }
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    out: Dict[str, List[int]] = {}
    for k, v in data.items():
        out[str(k)] = [int(x) for x in v]
    return out


def numa_nodes_to_pools(numa_nodes: Dict[str, List[int]]) -> List[List[int]]:
    """
    Convert NUMA nodes mapping into a list of CPU pools.
    Each pool is a list of CPU indices that should be kept together.
    """
    pools: List[List[int]] = []
    # Sort by numeric node id if possible, or lexicographically as fallback.
    def key_fn(item):
        name, _ = item
        try:
            return int(name.replace("group_", ""))
        except ValueError:
            return name

    for node_id, cpus in sorted(numa_nodes.items(), key=key_fn):
        if not cpus:
            continue
        pools.append(sorted(cpus))
    return pools


def build_affinity_blocks(concurrency: int, pools: List[List[int]]) -> List[List[int]]:
    """
    Build per-child affinity blocks by walking across pools and slicing CPUs.
    This is intentionally simple and conservative; if psutil rejects a mask,
    we just disable affinity gracefully.
    """
    if concurrency <= 0 or not pools:
        return []

    all_cpus: List[int] = []
    for p in pools:
        all_cpus.extend(p)

    if not all_cpus:
        return []

    total_cpus = len(all_cpus)
    base = total_cpus // concurrency
    rem = total_cpus % concurrency
    blocks: List[List[int]] = []

    cur = 0
    for i in range(concurrency):
        span = base + (1 if i < rem else 0)
        if span <= 0:
            span = 1
        slice_cpus = all_cpus[cur:cur + span]
        if not slice_cpus:
            slice_cpus = [all_cpus[-1]]
        blocks.append(slice_cpus)
        cur += span
    return blocks


def resolve_output(args: argparse.Namespace, concurrency: int, logger: logging.Logger) -> str:
    """
    Derive an output pattern; if the user specified a non-pattern video output and concurrency>1,
    refuse parallel video rendering.
    """
    out = args.output
    lower = out.lower()
    video_exts = (".mov", ".mp4", ".avi", ".mxf", ".mkv")
    if concurrency > 1 and lower.endswith(video_exts):
        logger.error(
            f"Refusing parallel render of single video file: {out} with concurrency={concurrency}. "
            "Use an image sequence or concurrency=1 instead."
        )
        sys.exit(2)
    return out


def auto_concurrency(args: argparse.Namespace, logger: logging.Logger) -> int:
    """
    Rough heuristic for AE:
      - If MFR is disabled: concurrency ~ logical_cores / 4
      - If MFR is enabled: concurrency ~ logical_cores / (2 * mfr_threads)

    Clamped by args.max_concurrency if provided.
    """
    logical = psutil.cpu_count(logical=True) or 8
    if args.disable_mfr:
        base = max(1, logical // 4)
    else:
        denom = max(1, 2 * args.mfr_threads)
        base = max(1, logical // denom)

    if args.max_concurrency > 0:
        base = min(base, args.max_concurrency)

    logger.info(f"Auto concurrency chose {base} (logical={logical}, disable_mfr={args.disable_mfr})")
    return base


def setup_logging(log_file: Optional[str]) -> logging.Logger:
    logger = logging.getLogger("stmpo")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler: logging.Handler
    if log_file:
        handler = logging.FileHandler(log_file, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


def stream_reader(pid: int, stream, out_q: queue.Queue, tag: str):
    """
    Read lines from child stream, prefix them with PID, and put into out_q.
    """
    for line in iter(stream.readline, ""):
        line = line.rstrip("\n")
        out_q.put((pid, tag, line))
    stream.close()


def load_env_overrides(env_file: Optional[str]) -> Dict[str, str]:
    if not env_file:
        return {}
    p = Path(env_file)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {str(k): str(v) for k, v in data.items()}


# -----------------------------
# Aerender command builder
# -----------------------------

def build_aerender_cmd(
    args: argparse.Namespace,
    s: int,
    e: int,
    output_pattern: str,
) -> List[str]:
    """
    Build the aerender command line for a single child process.

    Notes on MFR:
    Adobe aerender expects: `-mfr mfr_flag max_cpu_percent`
      - mfr_flag: "ON" or "OFF"
      - max_cpu_percent: 1–100 (required even when OFF; ignored in that case)
    """
    cmd: List[str] = [
        args.aerender_path,
        "-project", args.project,
        "-output", output_pattern,
        "-sound", "OFF",
        "-s", str(s),
        "-e", str(e),
    ]

    if args.comp:
        cmd += ["-comp", args.comp]
    if args.rqindex is not None:
        cmd += ["-rqindex", str(args.rqindex)]
    if getattr(args, "rs_template", None):
        cmd += ["-RStemplate", args.rs_template]
    if getattr(args, "om_template", None):
        cmd += ["-OMtemplate", args.om_template]

    mfr_flag = "OFF" if args.disable_mfr else "ON"
    max_cpu_percent = 100  # safe default; ignored when MFR is OFF

    cmd += ["-mfr", mfr_flag, str(max_cpu_percent)]

    return cmd


# -----------------------------
# Argument parsing
# -----------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="STMPO After Effects Orchestrator")

    p.add_argument("--project", required=True, help="Path to .aep project")
    p.add_argument("--comp", default=None, help="Comp name (optional if using render queue index)")
    p.add_argument("--rqindex", type=int, default=None, help="Render queue index")
    p.add_argument("--output", required=True, help="Output pattern")
    p.add_argument("--start", type=int, required=True, help="Start frame")
    p.add_argument("--end", type=int, required=True, help="End frame")

    p.add_argument("--concurrency", type=int, default=0, help="Number of aerender instances (0 = auto)")
    p.add_argument("--max_concurrency", type=int, default=32, help="Upper bound on auto concurrency")

    p.add_argument("--spawn_delay", type=float, default=2.0,
                   help="Seconds between spawning children.")
    p.add_argument("--child_grace_sec", type=float, default=10.0,
                   help="Grace period when shutting down children (seconds).")

    p.add_argument("--kill_on_fail", action="store_true",
                   help="If set, kill all children when any fails.")
    p.add_argument("--no_kill_on_fail", dest="kill_on_fail", action="store_false",
                   help="If set, do NOT kill all children on a single failure.")
    p.set_defaults(kill_on_fail=True)

    p.add_argument("--env_file", default=None,
                   help="Optional JSON file with env overrides.")
    p.add_argument("--log_file", default=None,
                   help="Optional path to a log file.")

    p.add_argument("--ram_per_process_gb", type=float, default=10.0,
                   help="Auto-mode RAM budget per aerender instance (for heuristics only).")

    # MFR options
    p.add_argument("--mfr_threads", type=int, default=2,
                   help="Threads per aerender instance when MFR on.")
    p.add_argument("--disable_mfr", action="store_true",
                   help="Disable MFR for each aerender instance.")

    # Affinity / topology
    p.add_argument("--aerender_path", default=DEFAULT_AERENDER,
                   help="Override path to aerender.exe.")
    p.add_argument("--numa_map", default=DEFAULT_NUMA_MAP,
                   help="Path to numa_map.json produced from Coreinfo -n (optional).")
    p.add_argument("--disable_affinity", action="store_true",
                   help="Do not set CPU affinity.")

    # Optional templates (some call_aerender variants may pass these)
    p.add_argument("--rs_template", default=None, help="Render settings template name.")
    p.add_argument("--om_template", default=None, help="Output module template name.")

    return p.parse_args()


# -----------------------------
# Main orchestration
# -----------------------------

def main():
    args = parse_args()
    logger = setup_logging(args.log_file)

    # Basic validations
    if args.end < args.start:
        logger.error(f"Invalid frame range: start={args.start}, end={args.end}")
        sys.exit(2)

    proj = Path(args.project)
    if not proj.exists():
        logger.error(f"Project not found: {proj}")
        sys.exit(2)

    aer = Path(args.aerender_path)
    if not aer.exists():
        logger.error(f"aerender.exe not found: {aer}")
        sys.exit(2)

    total_frames = args.end - args.start + 1

    requested = args.concurrency if args.concurrency >= 1 else auto_concurrency(args, logger)
    concurrency = min(requested, total_frames)

    output_pattern = resolve_output(args, concurrency, logger)

    # Pre-create output directory to prevent race conditions
    try:
        out_dir = Path(output_pattern).parent
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning(f"Could not pre-create output dir: {e}")

    logger.info(f"Resolved output pattern: {output_pattern}")
    logger.info(f"Using concurrency={concurrency}")

    # Affinity computation
    affinities: List[List[int]] = []
    if not args.disable_affinity:
        try:
            numa_path = args.numa_map

            # If numa_map is not provided or blank, fall back to a job-attached
            # numa_map.json in the same folder as this script, if present.
            if not numa_path:
                candidate = Path(__file__).with_name("numa_map.json")
                if candidate.exists():
                    numa_path = str(candidate)

            if numa_path and Path(numa_path).exists():
                numa_nodes = load_numa_nodes(numa_path)
                pools = numa_nodes_to_pools(numa_nodes)
                if pools:
                    affinities = build_affinity_blocks(concurrency, pools)
                    logger.info(f"Affinity active: {len(affinities)} blocks built across {len(pools)} pools.")
                else:
                    logger.warning("No CPU pools found in numa_map; affinity disabled.")
            else:
                if numa_path:
                    logger.warning(f"NUMA map not found at {numa_path}; affinity disabled.")
        except Exception as e:
            logger.error(f"Topology/affinity error: {e}. Affinity disabled.")

    else:
        logger.info("Affinity disabled by flag.")

    ranges = split_ranges(args.start, args.end, concurrency)
    env_overrides = load_env_overrides(args.env_file)
    child_env = os.environ.copy()
    child_env.update(env_overrides)

    if args.env_file:
        logger.info(f"Environment overrides loaded from {args.env_file}: {len(env_overrides)} entries")
    else:
        logger.info("No env_file provided; using process environment only.")

    stop_event = threading.Event()

    def on_signal(signum, frame):
        logger.warning(f"Received signal {signum}; stopping children.")
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    children: List[ChildProc] = []
    out_q: queue.Queue = queue.Queue()

    logger.info(f"Starting spawn sequence with {args.spawn_delay}s stagger delay...")
    logger.info(
        f"Spawn plan: {len(ranges)} children across frames {args.start}-{args.end} "
        f"(per-child ~{math.ceil(total_frames/len(ranges))} frames)"
    )

    affinity_applicable = bool(affinities) and not args.disable_affinity
    last_heartbeat = time.time()
    completion_logged = set()

    # Helper to kill all children
    def kill_all_children():
        for ch in children:
            if ch.popen.poll() is None:
                try:
                    logger.warning(f"Terminating PID {ch.popen.pid}")
                    ch.popen.terminate()
                except Exception as ex:
                    logger.warning(f"Error terminating PID {ch.popen.pid}: {ex}")
        time.sleep(args.child_grace_sec)
        for ch in children:
            if ch.popen.poll() is None:
                try:
                    logger.warning(f"Killing PID {ch.popen.pid}")
                    ch.popen.kill()
                except Exception as ex:
                    logger.warning(f"Error killing PID {ch.popen.pid}: {ex}")

    # Spawn children
    for i, (s, e) in enumerate(ranges):
        if stop_event.is_set():
            break
        if i > 0 and args.spawn_delay > 0:
            time.sleep(args.spawn_delay)

        cmd = build_aerender_cmd(args, s, e, output_pattern)
        aff = affinities[i] if (affinity_applicable and affinities and i < len(affinities)) else None

        logger.info(f"Launching #{i} for frames {s}-{e}")
        try:
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=child_env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

            # Apply affinity after spawn using psutil, if requested and available.
            if aff is not None and affinity_applicable and not args.disable_affinity:
                try:
                    proc_handle = psutil.Process(p.pid)
                    proc_handle.cpu_affinity(aff)
                    logger.info(f"Affinity applied for PID {p.pid}: {aff}")
                except Exception as ex:
                    logger.warning(f"Affinity warning PID {p.pid}: {ex}")
                    affinity_applicable = False
                    proc_handle = None
            else:
                try:
                    proc_handle = psutil.Process(p.pid)
                except Exception:
                    proc_handle = None

            if proc_handle:
                # Prime cpu_percent so later heartbeats have deltas
                try:
                    proc_handle.cpu_percent(None)
                except Exception:
                    pass

            threading.Thread(
                target=stream_reader,
                args=(p.pid, p.stdout, out_q, "LOG"),
                daemon=True,
            ).start()

            logger.info(f"Spawned PID {p.pid} frames {s}-{e} affinity={aff}")
            children.append(
                ChildProc(
                    popen=p,
                    frame_range=(s, e),
                    affinity=aff,
                    psutil_proc=proc_handle,
                    start_time=time.time(),
                )
            )

        except Exception as spawn_ex:
            logger.error(f"Failed to spawn child for frames {s}-{e}: {spawn_ex}")
            stop_event.set()
            break

    if not children:
        logger.error("No children were spawned; aborting.")
        sys.exit(1)

    # Main loop: aggregate logs, check heartbeats, and watch exit codes.
    while True:
        # Drain log queue
        try:
            while True:
                pid, tag, line = out_q.get_nowait()
                logger.info(f"[PID {pid} {tag}] {line}")
        except queue.Empty:
            pass

        now = time.time()
        # Heartbeat
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            last_heartbeat = now
            hb_lines = []
            for ch in children:
                rc = ch.popen.poll()
                status = "running" if rc is None else f"exit={rc}"
                try:
                    cpu = ch.psutil_proc.cpu_percent(None) if ch.psutil_proc else 0.0
                    rss_mb = (
                        ch.psutil_proc.memory_info().rss / (1024 * 1024)
                        if ch.psutil_proc else 0.0
                    )
                except Exception:
                    cpu = 0.0
                    rss_mb = 0.0
                elapsed = now - ch.start_time
                hb_lines.append(
                    f"PID {ch.popen.pid} frames {ch.frame_range[0]}-{ch.frame_range[1]} "
                    f"elapsed={elapsed:.1f}s status={status} cpu%={cpu:.1f} rss_mb={rss_mb:.1f}"
                )
            if hb_lines:
                logger.info("Heartbeat: " + "; ".join(hb_lines))

        # Check completion / failure
        all_done = True
        any_failed = False
        for ch in children:
            rc = ch.popen.poll()
            if rc is None:
                all_done = False
            else:
                if ch.popen.pid not in completion_logged:
                    completion_logged.add(ch.popen.pid)
                    logger.info(
                        f"Child PID {ch.popen.pid} completed frames "
                        f"{ch.frame_range[0]}-{ch.frame_range[1]} with code {rc}"
                    )
                if rc != 0:
                    any_failed = True

        if any_failed and args.kill_on_fail:
            logger.error("One or more children failed; killing siblings due to kill_on_fail.")
            kill_all_children()
            sys.exit(1)

        if all_done:
            break

        if stop_event.is_set():
            logger.warning("Stop event set; killing all children.")
            kill_all_children()
            sys.exit(1)

        time.sleep(1)

    # Final check
    codes = [(ch.popen.pid, ch.popen.returncode) for ch in children]
    bad = [(pid, c) for pid, c in codes if c != 0]

    if bad:
        logger.error(f"STMPO failure; bad children: {bad}")
        sys.exit(1)

    logger.info("STMPO complete; all children succeeded.")
    sys.exit(0)


if __name__ == "__main__":
    main()