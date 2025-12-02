# stmpo_wrapper.py
"""
STMPO After Effects Orchestrator (Optimized for NVMe Caching)

- Splits a single Deadline Cloud task frame range into N sub-ranges.
- Spawns N aerender.exe processes in parallel.
- Renders to LOCAL NVMe scratch to eliminate NAS I/O bottlenecks.
- Asynchronously offloads finished frames to the final NAS destination.
- Optionally assigns CPU affinity per process based on a NUMA/CPU map.
- Fail-fast: if any child fails, terminates siblings.

This script is invoked by call_aerender.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil


# -----------------------------
# Configuration / Defaults
# -----------------------------

# USER CORRECTED PATH
DEFAULT_AERENDER = r"E:\DCC\Adobe\Adobe After Effects 2024\Support Files\aerender.exe"

# NVMe Scratch Location (Ensure this drive exists and is fast)
LOCAL_SCRATCH_ROOT = r"C:\Deadline\InterimRenderFrames"

DEFAULT_NUMA_MAP = "" 
HEARTBEAT_SECONDS = 15


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


def summarize_proc_tree(root: psutil.Process) -> Tuple[float, float]:
    """
    Return (cpu_percent, rss_mb) for a process and all of its children.
    """
    procs: List[psutil.Process] = [root]
    try:
        procs.extend(root.children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    total_cpu = 0.0
    total_rss_bytes = 0

    for p in procs:
        try:
            total_cpu += p.cpu_percent(None)
            total_rss_bytes += p.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    rss_mb = total_rss_bytes / (1024 * 1024)
    return total_cpu, rss_mb


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
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    out: Dict[str, List[int]] = {}
    for k, v in data.items():
        out[str(k)] = [int(x) for x in v]
    return out


def numa_nodes_to_pools(numa_nodes: Dict[str, List[int]]) -> List[List[int]]:
    pools: List[List[int]] = []
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


def auto_concurrency(args: argparse.Namespace, logger: logging.Logger) -> int:
    """
    Heuristic for Concurrency.
    Refined for High-Core Count Machines (EPYC/Threadripper).
    Target: ~16 threads per AE instance when MFR is ON.
    """
    logical = psutil.cpu_count(logical=True) or 8
    
    if args.disable_mfr:
        # Classical AE: One core per instance roughly.
        # Cap conservatively to avoid OS thrashing on 128+ core systems.
        base = max(1, logical // 4)
    else:
        # MFR Enabled:
        # If user didn't specify mfr_threads, assume we want ~16 threads per instance.
        # This keeps the "Thundering Herd" small while keeping CPUs busy.
        target_threads_per_proc = max(16, args.mfr_threads)
        base = max(1, logical // target_threads_per_proc)

    if args.max_concurrency > 0:
        base = min(base, args.max_concurrency)

    logger.info(f"Auto concurrency chose {base} (logical={logical}, target_threads_per_proc={target_threads_per_proc if not args.disable_mfr else 'N/A'})")
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
# File Offloader (The "Sidecar")
# -----------------------------

def is_file_stable(filepath: Path) -> bool:
    """
    Checks if a file is ready to be moved.
    1. Checks if it exists.
    2. Attempts to rename it to itself. This is an atomic check on Windows
       that fails if ANY process (like aerender) has a write handle open.
    """
    if not filepath.exists():
        return False
    try:
        filepath.rename(filepath)
        return True
    except OSError:
        return False
    except Exception:
        return False

def offload_worker(local_dir: Path, remote_dir: Path, stop_event: threading.Event, logger: logging.Logger):
    """
    Watches local_dir for files. Moves them to remote_dir if they are stable.
    Runs until stop_event is set AND local_dir is empty (or we time out on final pass).
    """
    logger.info(f"Offloader started: {local_dir} -> {remote_dir}")
    
    # Ensure remote dir exists
    try:
        remote_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Offloader could not create remote dir {remote_dir}: {e}")
        # Continue; maybe it exists and we just can't mkdir

    def process_files():
        # Iterate over all files in local scratch
        for local_file in local_dir.glob("*"):
            if not local_file.is_file():
                continue
            
            # Skip if we can't lock/rename it (AE is writing)
            if not is_file_stable(local_file):
                continue
            
            dest_path = remote_dir / local_file.name
            
            try:
                # Copy with metadata (stat) then delete.
                # This is safer than shutil.move across file systems.
                shutil.copy2(local_file, dest_path)
                os.remove(local_file)
                logger.info(f"[Offload] Moved {local_file.name}")
            except Exception as e:
                logger.error(f"[Offload] Failed to move {local_file.name}: {e}")

    # Main Loop
    while not stop_event.is_set():
        process_files()
        time.sleep(1.0) # Check every second

    # Final Cleanup Pass
    logger.info("Offloader received stop signal. Performing final pass...")
    # Try a few times to clear the buffer in case of stragglers
    for _ in range(3):
        process_files()
        if not any(local_dir.glob("*")):
            break
        time.sleep(1)

    # Check if anything remains
    remaining = list(local_dir.glob("*"))
    if remaining:
        logger.warning(f"Offloader finishing with {len(remaining)} files left in scratch (likely stuck/locked): {remaining}")
    else:
        logger.info("Offloader finished clean.")


# -----------------------------
# Aerender command builder
# -----------------------------

def build_aerender_cmd(
    args: argparse.Namespace,
    s: int,
    e: int,
    output_path: str, # This is the LOCAL path
) -> List[str]:
    
    cmd: List[str] = [
        args.aerender_path,
        "-project", args.project,
        "-output", output_path,
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
    
    # MFR Logic
    # Adobe expects: -mfr [ON|OFF] [percent_cpu]
    mfr_flag = "OFF" if args.disable_mfr else "ON"
    # Even if OFF, we provide 100 to satisfy syntax.
    # If ON, we usually want 100% allowed per process, relying on OS scheduling or Affinity to limit it.
    cmd += ["-mfr", mfr_flag, "100"]

    return cmd


# -----------------------------
# Main Orchestrator
# -----------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="STMPO After Effects Orchestrator")
    p.add_argument("--project", required=True)
    p.add_argument("--comp", default=None)
    p.add_argument("--rqindex", type=int, default=None)
    p.add_argument("--output", required=True)
    p.add_argument("--start", type=int, required=True)
    p.add_argument("--end", type=int, required=True)
    
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--max_concurrency", type=int, default=32)
    
    p.add_argument("--spawn_delay", type=float, default=2.0)
    p.add_argument("--child_grace_sec", type=float, default=10.0)
    
    p.add_argument("--kill_on_fail", action="store_true")
    p.add_argument("--no_kill_on_fail", dest="kill_on_fail", action="store_false")
    p.set_defaults(kill_on_fail=True)
    
    p.add_argument("--env_file", default=None)
    p.add_argument("--log_file", default=None)
    
    p.add_argument("--ram_per_process_gb", type=float, default=10.0)
    p.add_argument("--mfr_threads", type=int, default=16, help="Target MFR threads per process")
    p.add_argument("--disable_mfr", action="store_true")
    
    p.add_argument("--aerender_path", default=DEFAULT_AERENDER)
    p.add_argument("--numa_map", default=DEFAULT_NUMA_MAP)
    p.add_argument("--disable_affinity", action="store_true")
    
    # Optional templates
    p.add_argument("--rs_template", default=None)
    p.add_argument("--om_template", default=None)
    p.add_argument("--output_is_pattern", action="store_true")
    
    return p.parse_args()


def main():
    args = parse_args()
    logger = setup_logging(args.log_file)

    # 1. Setup Local Scratch Architecture
    # -----------------------------------
    # Original output arg is the FINAL destination (NAS)
    final_output_path = Path(args.output)
    final_output_dir = final_output_path.parent
    output_filename = final_output_path.name

    # Create unique local scratch folder
    # We use a UUID to prevent collisions if multiple jobs run on same machine
    job_uuid = str(uuid.uuid4())[:8]
    local_scratch_dir = Path(LOCAL_SCRATCH_ROOT) / f"job_{job_uuid}"
    
    try:
        local_scratch_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created local scratch: {local_scratch_dir}")
    except Exception as e:
        logger.critical(f"Failed to create local scratch {local_scratch_dir}: {e}")
        sys.exit(1)

    # The output path we pass to aerender is LOCAL
    local_output_path = local_scratch_dir / output_filename
    
    # 2. Concurrency & Affinity Logic
    # -------------------------------
    requested = args.concurrency if args.concurrency >= 1 else auto_concurrency(args, logger)
    total_frames = args.end - args.start + 1
    concurrency = min(requested, total_frames)
    
    logger.info(f"Orchestration: Concurrency={concurrency}, MFR={'OFF' if args.disable_mfr else 'ON'}")
    logger.info(f"Pipeline: NVMe [{local_output_path}] -> NAS [{final_output_path}]")

    # Load NUMA/Affinity
    affinities: List[List[int]] = []
    if not args.disable_affinity:
        try:
            numa_path = args.numa_map
            if not numa_path:
                candidate = Path(__file__).with_name("numa_map.json")
                if candidate.exists():
                    numa_path = str(candidate)
            
            if numa_path and Path(numa_path).exists():
                numa_nodes = load_numa_nodes(numa_path)
                pools = numa_nodes_to_pools(numa_nodes)
                if pools:
                    affinities = build_affinity_blocks(concurrency, pools)
                    logger.info(f"Affinity active: {len(affinities)} blocks.")
                else:
                    logger.warning("No CPU pools in numa_map.")
            else:
                logger.info("No NUMA map found, affinity disabled.")
        except Exception as e:
            logger.error(f"Affinity config error: {e}")

    # 3. Start Offloader Thread
    # -------------------------
    stop_offload_event = threading.Event()
    offloader_thread = threading.Thread(
        target=offload_worker, 
        args=(local_scratch_dir, final_output_dir, stop_offload_event, logger),
        daemon=False # We want it to finish cleaning up even if main exits weirdly
    )
    offloader_thread.start()

    # 4. Spawn Children
    # -----------------
    ranges = split_ranges(args.start, args.end, concurrency)
    env_overrides = load_env_overrides(args.env_file)
    child_env = os.environ.copy()
    child_env.update(env_overrides)

    children: List[ChildProc] = []
    out_q: queue.Queue = queue.Queue()
    stop_children_event = threading.Event()

    # Cleanup Helper
    def cleanup_resources():
        logger.info("Shutting down...")
        # 1. Stop Children
        stop_children_event.set()
        for ch in children:
            if ch.popen.poll() is None:
                try:
                    ch.popen.terminate()
                except:
                    pass
        # 2. Stop Offloader
        stop_offload_event.set()
        if offloader_thread.is_alive():
            offloader_thread.join()
        # 3. Clean Scratch
        try:
            if local_scratch_dir.exists():
                shutil.rmtree(local_scratch_dir, ignore_errors=True)
                logger.info("Scratch cleared.")
        except Exception as e:
            logger.warning(f"Error clearing scratch: {e}")

    # Register signals for clean shutdown
    signal.signal(signal.SIGINT, lambda s, f: cleanup_resources())
    signal.signal(signal.SIGTERM, lambda s, f: cleanup_resources())

    for i, (s, e) in enumerate(ranges):
        if stop_children_event.is_set(): break
        if i > 0 and args.spawn_delay > 0: 
            time.sleep(args.spawn_delay)

        cmd = build_aerender_cmd(args, s, e, str(local_output_path))
        aff = affinities[i] if (affinities and i < len(affinities)) else None

        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=child_env, text=True, encoding="utf-8", errors="replace", bufsize=1
            )
            
            # Apply Affinity
            if aff:
                try:
                    psutil.Process(p.pid).cpu_affinity(aff)
                except Exception:
                    pass
            
            threading.Thread(target=stream_reader, args=(p.pid, p.stdout, out_q, "LOG"), daemon=True).start()
            
            children.append(ChildProc(p, (s, e), aff, psutil.Process(p.pid), time.time()))
            logger.info(f"Launched Worker #{i} (PID {p.pid}) Frames {s}-{e}")

        except Exception as ex:
            logger.error(f"Spawn failed: {ex}")
            cleanup_resources()
            sys.exit(1)

    # 5. Monitor Loop
    # ---------------
    last_heartbeat = time.time()
    
    while True:
        # Drain Logs
        try:
            while True:
                pid, tag, line = out_q.get_nowait()
                logger.info(f"[PID {pid}] {line}")
        except queue.Empty:
            pass

        # Heartbeat
        if time.time() - last_heartbeat > HEARTBEAT_SECONDS:
            last_heartbeat = time.time()
            running_count = sum(1 for c in children if c.popen.poll() is None)
            logger.info(f"Heartbeat: {running_count}/{len(children)} workers running.")

        # Check Status
        all_done = True
        any_failed = False
        
        for ch in children:
            rc = ch.popen.poll()
            if rc is None:
                all_done = False
            elif rc != 0:
                any_failed = True
                logger.error(f"Worker PID {ch.popen.pid} failed with RC {rc}")

        if any_failed and args.kill_on_fail:
            logger.error("Critical Failure in worker. Aborting job.")
            cleanup_resources()
            sys.exit(1)

        if all_done:
            logger.info("All render processes completed successfully.")
            break
        
        if stop_children_event.is_set():
            cleanup_resources()
            sys.exit(1)
        
        time.sleep(1)

    # 6. Final Sync
    # -------------
    logger.info("Render complete. Waiting for final offload...")
    stop_offload_event.set()
    offloader_thread.join()
    
    # Remove scratch dir
    try:
        shutil.rmtree(local_scratch_dir)
        logger.info("Scratch cleared.")
    except Exception as e:
        logger.warning(f"Could not delete scratch dir: {e}")

    sys.exit(0)

if __name__ == "__main__":
    main()
