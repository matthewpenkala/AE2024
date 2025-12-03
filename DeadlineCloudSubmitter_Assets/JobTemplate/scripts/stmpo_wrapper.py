# stmpo_wrapper.py
"""
STMPO After Effects Orchestrator

- Splits a single Deadline Cloud task frame range into N sub-ranges.
- Spawns N aerender.exe processes in parallel.
- Renders to LOCAL NVMe scratch to eliminate NAS I/O bottlenecks.
- Asynchronously offloads finished frames to the final NAS destination.
- Applies CPU affinity (NUMA) with robust error handling (WinError 87 detection).
- Monitors heartbeats with active CPU sampling to detect "stuck" renderers.

Usage:
  This script is invoked by call_aerender.py.
  Set env var STMPO_DEBUG=1 for full command-line echoing.
"""

import argparse
import json
import logging
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
from typing import Dict, List, Optional, Set, Tuple

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
LOG_SILENCE_TIMEOUT = 300  # Seconds without log output before considering a renderer stalled
# Abort if every worker sits at zero CPU while still stuck in AE splash/licensing
# for this many consecutive heartbeats. This protects against the "check manually"
# scenario reported by users.
ZERO_CPU_STUCK_HEARTBEATS = 4
# Enable extra logging only when explicitly requested via env var.
DEBUG_MODE = os.environ.get("STMPO_DEBUG", "0") == "1"

# Offload throttling/deprioritization
OFFLOAD_SCAN_INTERVAL = 2.5  # seconds between scans when there is work
OFFLOAD_BURST_LIMIT = 5  # move at most this many files per scan to keep I/O bursts short
OFFLOAD_IDLE_INTERVAL = 5.0  # seconds between scans when nothing is pending


@dataclass
class WorkerProcess:
    """Represents a single aerender worker and its metadata."""
    popen: subprocess.Popen
    psutil_proc: Optional[psutil.Process]
    start_frame: int
    end_frame: int
    start_time: float


# -----------------------------
# Utility helpers
# -----------------------------

def fmt_metric(value: Optional[float], precision: int = 2, suffix: str = "") -> str:
    """Safely formats a metric that might be None."""
    if value is None:
        return "n/a"
    return f"{value:.{precision}f}{suffix}"


def summarize_descendants(proc: Optional[psutil.Process]) -> str:
    """Return a short summary of a worker's child processes for diagnostics."""
    if not proc:
        return "psutil handle unavailable"

    try:
        descendants = proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as ex:
        return f"descendants unavailable: {ex}"
    except Exception as ex:  # pragma: no cover - defensive logging only
        return f"descendants unavailable: {ex}"

    if not descendants:
        return "no child processes"

    parts = []
    for child in descendants:
        try:
            parts.append(
                f"{child.pid}:{child.name()}({child.status()},cpu={fmt_metric(child.cpu_percent(None),2,'%%')},"
                f"rss={fmt_metric(child.memory_info().rss/(1024*1024),1,'MB')})"
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception as ex:  # pragma: no cover - defensive logging only
            parts.append(f"{child.pid}: {ex}")

    return "; ".join(parts)


def split_ranges(start: int, end: int, chunks: int) -> List[Tuple[int, int]]:
    """
    Split [start, end] inclusive into `chunks` contiguous ranges.

    Example:
      split_ranges(0, 9, 3) -> [(0, 3), (4, 7), (8, 9)]
    """
    if chunks <= 1:
        return [(start, end)]

    total = end - start + 1
    base = total // chunks
    rem = total % chunks
    ranges: List[Tuple[int, int]] = []

    cur = start
    for i in range(chunks):
        span = base + (1 if i < rem else 0)
        if span <= 0:
            span = 1
        s = cur
        e = cur + span - 1
        if e > end:
            e = end
        ranges.append((s, e))
        cur = e + 1
        if cur > end:
            break
    return ranges


def load_numa_map(path: str) -> Dict[str, List[int]]:
    """Load a JSON NUMA map of the form { "node0": [cpu indices], ... }."""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: Dict[str, List[int]] = {}
    for k, v in data.items():
        try:
            if isinstance(v, list):
                flat: List[int] = []
                for item in v:
                    if isinstance(item, list):
                        flat.extend(int(x) for x in item)
                    else:
                        flat.append(int(item))
                out[str(k)] = flat
        except Exception:
            continue
    return out


def numa_nodes_to_pools(numa_nodes: Dict[str, List[int]]) -> List[List[int]]:
    """Convert NUMA node mapping into a list of CPU pools."""
    pools: List[List[int]] = []
    for _, cpus in sorted(numa_nodes.items()):
        if not cpus:
            continue
        uniq = sorted(set(int(c) for c in cpus))
        pools.append(uniq)
    return pools


def build_affinity_blocks(concurrency: int, cpu_pools: List[List[int]]) -> List[List[int]]:
    """
    Given concurrency and a list of CPU pools (each a list of CPU indices), build
    a list of affinity "blocks" for each worker. Attempts to keep each worker
    local to a small subset of CPUs across pools.
    """
    if concurrency <= 0 or not cpu_pools:
        return []

    all_cpus: List[int] = []
    for pool in cpu_pools:
        all_cpus.extend(pool)
    all_cpus = sorted(set(all_cpus))

    total_cpus = len(all_cpus)
    if total_cpus == 0:
        return []

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


def load_env_overrides(env_file: Optional[str]) -> Dict[str, str]:
    if not env_file:
        return {}
    p = Path(env_file)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {str(k): str(v) for k, v in data.items()}


def log_affinity_diagnostics(logger: logging.Logger, error: Exception, pid: int, affinity: List[int]):
    """
    Analyzes affinity failures, specifically looking for Windows error 87
    (The parameter is incorrect), which occurs when the mask crosses processor
    group boundaries on hosts with >64 logical CPUs.
    """
    msg = f"Failed to set affinity for PID {pid} to {affinity}: {error}"
    logger.warning(msg)

    if hasattr(error, "winerror"):
        winerr = getattr(error, "winerror", None)
        if winerr == 87:
            logger.warning(
                "WinError 87 detected while setting affinity. This often indicates that "
                "the requested CPU mask crosses a Windows processor group boundary on a "
                "host with more than 64 logical CPUs. Consider disabling affinity or "
                "providing a NUMA map that aligns with processor groups."
            )


def apply_affinity(pid: int, affinity: List[int], logger: logging.Logger, allowed_cpus: Optional[List[int]] = None) -> Optional[List[int]]:
    """
    Attempt to apply affinity to PID, falling back if the requested mask is not
    compatible with the current process group (Windows >64 logical CPUs).
    """
    try:
        proc = psutil.Process(pid)
    except Exception as ex:
        logger.warning(f"Could not obtain psutil.Process for PID {pid}: {ex}")
        return None

    try:
        proc.cpu_affinity(affinity)
        return affinity
    except Exception as ex:
        # Attempt fallback within allowed_cpus if provided
        log_affinity_diagnostics(logger, ex, pid, affinity)
        if not allowed_cpus:
            return None

        # Intersect affinity with allowed CPUs
        fallback = [c for c in affinity if c in allowed_cpus]
        if not fallback:
            fallback = allowed_cpus[:]
        try:
            proc.cpu_affinity(fallback)
            logger.info(f"Fallback affinity for PID {pid} applied: {fallback}")
            return fallback
        except Exception as inner_ex:
            log_affinity_diagnostics(logger, inner_ex, pid, fallback)
            return None


def auto_concurrency(args: argparse.Namespace, logger: logging.Logger) -> int:
    """
    Heuristic for choosing a safe, high-throughput concurrency value.

    Goals:
    - Respect **RAM limits** so we don't spawn more aerender processes than memory allows.
    - Respect **core topology** so each process gets a sensible slice of the CPU.
    - Obey the explicit --max_concurrency cap when provided.

    This is intentionally conservative – it should get you into RenderGarden-style territory
    (a handful of very busy aerender processes) rather than "48 ghosts stuck at splash".
    """
    # CPU topology
    logical = psutil.cpu_count(logical=True) or 8
    physical = psutil.cpu_count(logical=False) or logical

    # Total RAM in GiB
    try:
        total_ram_gb = psutil.virtual_memory().total / float(1024 ** 3)
    except Exception:
        total_ram_gb = 0.0

    # User hint for RAM per process (GiB). Clamp to a sane range.
    per_gb = float(getattr(args, "ram_per_process_gb", 0.0) or 0.0)
    if per_gb <= 0:
        per_gb = 8.0
    MIN_PER = 4.0
    MAX_PER = 256.0
    per_gb = max(MIN_PER, min(per_gb, MAX_PER))

    # Safety margin so we never actually consume 100% of theoretical RAM.
    SAFETY_MARGIN = 1.25

    if total_ram_gb > 0:
        ram_capacity = max(1, int(total_ram_gb / (per_gb * SAFETY_MARGIN)))
    else:
        # Fallback when psutil can't read RAM: be very conservative.
        ram_capacity = 4

    # Core-based capacity:
    # - With MFR disabled, assume each aerender should get a few physical cores.
    # - With MFR enabled, args.mfr_threads approximates threads-per-process.
    if args.disable_mfr:
        CORES_PER_PROC = 4  # tuned for EPYC/Threadripper-style hosts
    else:
        mfr_threads = max(1, int(getattr(args, "mfr_threads", 16) or 16))
        CORES_PER_PROC = max(1, mfr_threads)

    core_capacity = max(1, physical // CORES_PER_PROC)

    base = min(ram_capacity, core_capacity)

    # Honor explicit max_concurrency if provided (>0).
    if getattr(args, "max_concurrency", 0) and args.max_concurrency > 0:
        base = min(base, args.max_concurrency)

    # Final clamp – never drop below 1.
    if base < 1:
        base = 1

    logger.info(
        "auto_concurrency: logical=%s, physical=%s, total_ram_gb=%.1f, "
        "per_proc_ram_gb=%.1f, ram_capacity=%s, core_capacity=%s, "
        "max_concurrency=%s, chosen=%s",
        logical,
        physical,
        total_ram_gb,
        per_gb,
        ram_capacity,
        core_capacity,
        getattr(args, "max_concurrency", 0),
        base,
    )
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
    logger.handlers.clear()
    logger.addHandler(handler)

    return logger


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="STMPO After Effects Orchestrator")
    p.add_argument("--project", required=True)
    p.add_argument("--comp", default=None)
    p.add_argument("--rqindex", type=int, default=None)
    p.add_argument("--output", required=True)
    p.add_argument("--start", type=int, required=True)
    p.add_argument("--end", type=int, required=True)
    
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--max_concurrency", type=int, default=24)
    
    p.add_argument("--spawn_delay", type=float, default=2.0)
    p.add_argument("--child_grace_sec", type=float, default=10.0)
    
    p.add_argument("--kill_on_fail", action="store_true")
    p.add_argument("--no_kill_on_fail", dest="kill_on_fail", 
                   action="store_false")
    
    p.add_argument("--env_file", default=None)
    p.add_argument("--log_file", default=None)
    
    p.add_argument("--ram_per_process_gb", type=float, default=32.0)
    p.add_argument("--mfr_threads", type=int, default=16, help="Target MFR threads per process")
    p.add_argument("--disable_mfr", action="store_true")
    
    p.add_argument("--aerender_path", default=DEFAULT_AERENDER)
    p.add_argument("--numa_map", default=DEFAULT_NUMA_MAP)
    p.add_argument(
        "--disable_affinity",
        action="store_true",
        default=True,
        help=(
            "Disable CPU affinity (default)."
            " Affinity can be explicitly re-enabled with --enable_affinity."
        ),
    )
    p.add_argument(
        "--enable_affinity",
        action="store_false",
        dest="disable_affinity",
        help=(
            "Force-enable CPU affinity even though the global default is disabled."
            " On Windows hosts with more than 64 logical CPUs, affinity may still be"
            " constrained by processor groups."
        ),
    )
    
    # Optional templates
    p.add_argument("--rs_template", default=None)
    p.add_argument("--om_template", default=None)
    p.add_argument("--output_is_pattern", action="store_true")
    
    return p.parse_args()


def offload_worker(
    local_dir: Path,
    final_dir: Path,
    stop_event: threading.Event,
    logger: logging.Logger,
):
    """
    Move frames from local NVMe scratch to the final NAS destination.
    Uses a small burst size to avoid overwhelming the NAS with large I/O spikes.
    """
    def process_files(burst_limit: int) -> int:
        moved = 0
        for local_file in sorted(local_dir.glob("*")):
            if moved >= burst_limit:
                break
            dest = final_dir / local_file.name
            try:
                if dest.exists():
                    logger.debug(f"[Offload] Destination already exists, skipping: {dest}")
                    continue
                # Use move (rename) when possible, fallback to copy if cross-device
                try:
                    shutil.move(str(local_file), str(dest))
                    logger.debug(f"[Offload] Moved {local_file} -> {dest}")
                except shutil.Error:
                    shutil.copy2(str(local_file), str(dest))
                    local_file.unlink(missing_ok=True)
                    logger.debug(f"[Offload] Copied {local_file} -> {dest}")
                moved += 1
            except PermissionError:
                # Transient lock on NAS – sleep briefly and retry in a subsequent scan
                for attempt in range(3):
                    logger.warning(
                        f"[Offload] File lock when moving {local_file.name} (attempt {attempt + 1}/3); retrying..."
                    )
                    time.sleep(0.5)
                    continue
            except Exception as ex:
                logger.warning(f"[Offload] Failed moving {local_file} -> {dest}: {ex}")
        return moved

    # Main offload loop
    while not stop_event.is_set():
        moved = process_files(OFFLOAD_BURST_LIMIT)
        if moved == 0:
            time.sleep(OFFLOAD_IDLE_INTERVAL)
        else:
            time.sleep(OFFLOAD_SCAN_INTERVAL)

    # Final Cleanup Pass
    logger.info("Offloader received stop signal. Performing final pass...")
    for _ in range(3):
        process_files(OFFLOAD_BURST_LIMIT)
        if not any(local_dir.glob("*")):
            break


def main():
    args = parse_args()
    logger = setup_logging(args.log_file)

    logical_cpus = psutil.cpu_count(logical=True) or 0

    # Capture the CPUs currently available to this process (used for Windows fallbacks)
    current_affinity: Optional[List[int]] = None
    try:
        current_affinity = psutil.Process().cpu_affinity()
        logger.info(f"Current process affinity mask: {current_affinity}")
    except Exception as aff_probe_ex:
        logger.debug(f"Could not read current process affinity: {aff_probe_ex}")

    # 1. Setup Local Scratch Architecture
    # -----------------------------------
    final_output_path = Path(args.output)
    final_output_dir = final_output_path.parent
    output_filename = final_output_path.name

    job_uuid = str(uuid.uuid4())[:8]
    local_scratch_dir = Path(LOCAL_SCRATCH_ROOT) / f"job_{job_uuid}"
    
    try:
        local_scratch_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created local scratch: {local_scratch_dir}")
    except Exception as e:
        logger.critical(f"Failed to create local scratch {local_scratch_dir}: {e}")
        sys.exit(1)

    local_output_path = local_scratch_dir / output_filename
    
    # 2. Concurrency & Affinity Logic
    # -------------------------------
    requested = args.concurrency if args.concurrency >= 1 else auto_concurrency(args, logger)
    total_frames = args.end - args.start + 1
    concurrency = min(requested, total_frames)

    logger.info(
        f"STMPO: frames={args.start}-{args.end}, requested_concurrency={requested}, "
        f"effective_concurrency={concurrency}, total_frames={total_frames}"
    )

    # Load NUMA map & affinity blocks if enabled
    affinities: Optional[List[List[int]]] = None
    if not args.disable_affinity:
        try:
            numa_nodes = load_numa_map(args.numa_map)
            if numa_nodes:
                pools = numa_nodes_to_pools(numa_nodes)
                if pools:
                    logger.info(f"Parsed NUMA CPU pools: {pools}")
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
        daemon=False 
    )
    offloader_thread.start()

    # 4. Spawn Children
    # -----------------
    ranges = split_ranges(args.start, args.end, concurrency)
    env_overrides = load_env_overrides(args.env_file)
    child_env = os.environ.copy()
    child_env.update(env_overrides)

    # HYBRID: Enhanced Environment Logging
    if env_overrides:
        preview_items = list(env_overrides.items())
        preview = ", ".join([f"{k}={v}" for k, v in preview_items[:5]])
        if len(env_overrides) > 5:
            preview += f", ... (+{len(env_overrides) - 5} more)"
        logger.info(f"Environment overrides: {preview}")

    children: List[WorkerProcess] = []
    stop_children_event = threading.Event()

    def make_cmd(start_f: int, end_f: int) -> List[str]:
        cmd = [
            args.aerender_path,
            "-project", args.project,
            "-rqindex", str(args.rqindex) if args.rqindex is not None else "1",
            "-output", str(local_output_path),
            "-s", str(start_f),
            "-e", str(end_f),
        ]
        if args.comp:
            cmd += ["-comp", args.comp]
        if args.rs_template:
            cmd += ["-RStemplate", args.rs_template]
        if args.om_template:
            cmd += ["-OMtemplate", args.om_template]
        if args.output_is_pattern:
            cmd += ["-output", str(local_output_path)]
        if args.disable_mfr:
            cmd += ["-mfr", "off"]
        return cmd

    # Streaming reader for child stdout
    out_q: queue.Queue = queue.Queue()

    def reader_thread(tag: str, pipe, pid: int):
        for line in iter(pipe.readline, ""):
            out_q.put((pid, tag, line.rstrip("\r\n")))
        pipe.close()

    # Launch workers
    for i, (s, e) in enumerate(ranges):
        if stop_children_event.is_set():
            break

        cmd = make_cmd(s, e)

        # HYBRID: Full Command Echo (Debug Mode)
        if DEBUG_MODE:
            logger.info(f"Launching #{i} for frames {s}-{e}")
            logger.info(f"Command #{i}: {' '.join(cmd)}")

        aff = affinities[i] if (affinities and i < len(affinities)) else None
        applied_affinity: Optional[List[int]] = None

        try:
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=child_env, text=True, encoding="utf-8", errors="replace", bufsize=1
            )

            # HYBRID: Robust Affinity Handling
            if aff:
                applied_affinity = apply_affinity(p.pid, aff, logger, allowed_cpus=current_affinity)
                if applied_affinity and DEBUG_MODE:
                    logger.info(f"Set CPU affinity for PID {p.pid}: {applied_affinity}")

            # Attach psutil handle
            proc_handle = None
            try:
                proc_handle = psutil.Process(p.pid)
                # Prime cpu_percent with a short sample so heartbeats are non-zero
                proc_handle.cpu_percent(interval=0.1)
            except Exception as ph_ex:
                logger.debug(f"Could not attach psutil handle to PID {p.pid}: {ph_ex}")
                proc_handle = None

            wp = WorkerProcess(
                popen=p,
                psutil_proc=proc_handle,
                start_frame=s,
                end_frame=e,
                start_time=time.time()
            )
            children.append(wp)

            t = threading.Thread(target=reader_thread, args=(f"worker-{i}", p.stdout, p.pid), daemon=True)
            t.start()

            if i < len(ranges) - 1:
                time.sleep(max(0.0, args.spawn_delay))

        except Exception as ex:
            logger.error(f"Failed to launch worker {i} ({s}-{e}): {ex}", exc_info=True)

    if not children:
        logger.error("No workers were launched; aborting.")
        stop_offload_event.set()
        offloader_thread.join()
        sys.exit(1)

    # 5. Monitor Children (Heartbeats)
    # --------------------------------
    last_heartbeat = time.time()
    last_log_time: Dict[int, float] = {}
    last_log_line: Dict[int, str] = {}
    zero_cpu_counts: Dict[int, int] = {}
    zero_cpu_global_streak = 0
    last_zero_cpu_diag = 0.0
    zero_cpu_hint_emitted = False

    job_failed = False
    job_failed_logged = False

    def handle_sigint(signum, frame):
        logger.warning("SIGINT received; signalling children to stop...")
        stop_children_event.set()

    signal.signal(signal.SIGINT, handle_sigint)

    idle_wait = HEARTBEAT_SECONDS / 3.0

    while True:
        # Drain log queue
        try:
            while True:
                try:
                    pid, tag, line = out_q.get_nowait()
                    logger.info(f"[PID {pid}] {line}")
                    last_log_time[pid] = time.time()
                    last_log_line[pid] = line
                except queue.Empty:
                    break
        except queue.Empty:
            pass

        # HYBRID: Heartbeat with Sampling & Diagnostics
        now = time.time()
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            running_children = [c for c in children if c.popen.poll() is None]
            logger.info(f"Heartbeat: {len(running_children)}/{len(children)} workers running.")
            
            summaries = []
            running_cpu_zero = []
            zombie_pids = []
            stalled_children: List[int] = []

            for ch in children:
                pid = ch.popen.pid
                rc = ch.popen.poll()
                state = "exited" if rc is not None else "running"
                runtime = now - ch.start_time
                cpu = None
                mem = None
                status = "unknown"

                if ch.psutil_proc is not None:
                    try:
                        cpu = ch.psutil_proc.cpu_percent(interval=0.0)
                    except Exception:
                        cpu = None
                    try:
                        mem = ch.psutil_proc.memory_info().rss / (1024.0 * 1024.0)
                    except Exception:
                        mem = None
                    try:
                        status = ch.psutil_proc.status()
                    except Exception:
                        status = "unknown"

                if rc is None:
                    # still running
                    if cpu is not None and cpu <= 0.01:
                        zero_cpu_counts[pid] = zero_cpu_counts.get(pid, 0) + 1
                    else:
                        zero_cpu_counts[pid] = 0
                else:
                    # child exited - keep counts but note as zombie if long-dead
                    if now - ch.start_time > LOG_SILENCE_TIMEOUT:
                        zombie_pids.append(pid)

                summaries.append(
                    f"PID {pid}: state={state}, runtime={runtime:.1f}s, "
                    f"cpu={fmt_metric(cpu,'','')}, mem={fmt_metric(mem,1,'MB')}, status={status}"
                )

                # Track "stalled" workers for potential global stuck detection
                if rc is None and zero_cpu_counts.get(pid, 0) >= 2:
                    running_cpu_zero.append(pid)

                # Detect per-worker stalls (no log output for a long time)
                last = last_log_time.get(pid, ch.start_time)
                if now - last > LOG_SILENCE_TIMEOUT and rc is None:
                    stalled_children.append(pid)

            if summaries:
                logger.info("Worker states:\n  " + "\n  ".join(summaries))

            if stalled_children:
                logger.warning(
                    "Stalled workers detected (no log output for a while): "
                    + ", ".join(str(pid) for pid in stalled_children)
                )

            # Global "all workers at zero CPU" diagnostic
            pending_children = [c for c in children if c.popen.poll() is None]
            if pending_children and len(running_cpu_zero) == len(pending_children):
                zero_cpu_global_streak += 1
                if now - last_zero_cpu_diag >= HEARTBEAT_SECONDS:
                    diag_lines = []
                    for ch in pending_children:
                        pid = ch.popen.pid
                        diag_lines.append(
                            f"PID {pid}: zero_cpu_streak={zero_cpu_counts.get(pid, 0)}, "
                            f"last_log='{last_log_line.get(pid, 'n/a')}', "
                            f"descendants={summarize_descendants(ch.psutil_proc)}"
                        )
                    logger.warning("Zero-CPU detailed diagnostics:\n  " + "\n  ".join(diag_lines))
                    last_zero_cpu_diag = now
                if not zero_cpu_hint_emitted:
                    logger.warning(
                        "Hint: If this warning repeats for several minutes with no new log output, try "
                        "reducing --concurrency and/or disabling affinity to rule out pinning/licensing issues.",
                    )
                    zero_cpu_hint_emitted = True

                # Hard-stop the job if every worker has been stuck at zero CPU for an
                # extended period while still only logging "Launching After Effects".
                launching_only = all(
                    "launching after effects" in last_log_line.get(ch.popen.pid, "").lower()
                    for ch in pending_children
                )
                if (
                    launching_only
                    and zero_cpu_global_streak >= ZERO_CPU_STUCK_HEARTBEATS
                    and now - min(ch.start_time for ch in pending_children)
                    >= ZERO_CPU_STUCK_HEARTBEATS * HEARTBEAT_SECONDS
                ):
                    logger.error(
                        "Workers appear stuck in the After Effects splash/licensing screen "
                        "with near-zero CPU usage. This usually indicates a licensing, "
                        "plugin, or environment issue. Aborting job so it can be retried "
                        "with adjusted settings (e.g., lower concurrency, different license)."
                    )
                    job_failed = True
                    stop_children_event.set()
            else:
                zero_cpu_global_streak = 0

            # Detect zombie PIDs
            if zombie_pids:
                logger.warning(f"Zombie workers detected (long-dead): {zombie_pids}")
            last_heartbeat = now

        # Check exit conditions
        all_done = all(c.popen.poll() is not None for c in children)
        any_failed = any(c.popen.poll() not in (None, 0) for c in children)
        fail_pid = None
        fail_rc = None
        if any_failed:
            for ch in children:
                rc = ch.popen.poll()
                if rc not in (None, 0):
                    fail_pid = ch.popen.pid
                    fail_rc = rc
                    break

        if any_failed:
            job_failed = True
            if not job_failed_logged:
                logger.error(f"Worker PID {fail_pid} failed with RC {fail_rc}")
                if args.kill_on_fail:
                    logger.error(
                        "kill_on_fail enabled; allowing running renderers to finish but job will exit non-zero once they complete."
                    )
                else:
                    logger.error("Continuing to monitor remaining workers; job will report failure when monitoring completes.")
                job_failed_logged = True

        if all_done:
            if job_failed:
                logger.error("One or more workers failed; exiting with failure status after allowing other workers to complete.")
                stop_offload_event.set()
                offloader_thread.join()
                sys.exit(1)
            else:
                logger.info("All workers completed successfully.")
                break

        time.sleep(idle_wait)

    # 6. Final Sync
    # -------------
    logger.info("Render complete. Waiting for final offload...")
    stop_offload_event.set()
    offloader_thread.join()
    
    try:
        shutil.rmtree(local_scratch_dir, ignore_errors=True)
        logger.info("Scratch cleared.")
    except Exception as e:
        logger.warning(f"Could not delete scratch dir: {e}")

    sys.exit(0)

if __name__ == "__main__":
    main()