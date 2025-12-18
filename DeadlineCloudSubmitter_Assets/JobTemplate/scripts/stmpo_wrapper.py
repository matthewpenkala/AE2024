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
        return "no After Effects child processes detected yet"

    parts: List[str] = []
    for child in descendants:
        try:
            name = child.name()
            status = child.status()
            cpu_pct = child.cpu_percent(interval=0.0)
            mem_info = child.memory_info()
            parts.append(
                f"{name}(pid={child.pid}, status={status}, cpu%={cpu_pct:.2f}, rss_mb={mem_info.rss / (1024 ** 2):.1f})"
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception as ex:  # pragma: no cover - defensive logging only
            parts.append(f"{child.pid}: {ex}")

    return "; ".join(parts) if parts else "descendants present but not readable"

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


def _flatten_numa_values(value) -> List:
    if isinstance(value, (list, tuple)):
        flattened: List = []
        for item in value:
            flattened.extend(_flatten_numa_values(item))
        return flattened
    return [value]


def _assert_cpu_ids_ints(cpus: List[int], logger: logging.Logger, node_name: str) -> bool:
    if all(isinstance(cpu, int) for cpu in cpus):
        return True

    logger.warning(f"NUMA entry '{node_name}' contained non-integer CPU ids: {cpus}")
    return False


def load_numa_nodes(json_path: str, logger: Optional[logging.Logger] = None) -> Dict[str, List[int]]:
    logger = logger or logging.getLogger("stmpo")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    out: Dict[str, List[int]] = {}
    for k, v in data.items():
        node_name = str(k)
        flattened = _flatten_numa_values(v)
        cpus: List[int] = []

        try:
            for entry in flattened:
                cpus.append(int(entry))
        except (TypeError, ValueError):
            logger.warning(f"NUMA entry '{node_name}' is malformed; affinity will be disabled for this entry.")
            continue

        if not _assert_cpu_ids_ints(cpus, logger, node_name):
            continue

        out[node_name] = cpus

    if not out:
        logger.warning("NUMA map parsed but no valid entries were found; affinity will be disabled.")

    return out


def resolve_aerender_path(raw_path: Optional[str], logger: logging.Logger) -> str:
    """Resolve the aerender.exe path with sensible fallbacks.

    A blank string coming from the template is treated as "not provided". We then
    try, in order:
    - User-provided CLI flag
    - Environment variable ``AERENDER_PATH``
    - ``shutil.which("aerender")`` (lets PATH supply the location)
    - The built-in ``DEFAULT_AERENDER`` constant

    If none of the candidates exists on disk, the process exits with an explicit
    error telling the user how to fix the configuration.
    """

    candidates: List[str] = []

    if raw_path and str(raw_path).strip():
        candidates.append(str(raw_path).strip())

    env_path = os.environ.get("AERENDER_PATH")
    if env_path:
        candidates.append(env_path)

    which_path = shutil.which("aerender")
    if which_path:
        candidates.append(which_path)

    candidates.append(DEFAULT_AERENDER)

    seen: Set[str] = set()
    for cand in candidates:
        # Avoid duplicate filesystem checks
        if cand in seen:
            continue
        seen.add(cand)

        expanded = os.path.expandvars(cand)
        if Path(expanded).exists():
            logger.info(f"Using aerender executable: {expanded}")
            return str(expanded)

    logger.critical(
        "Could not locate aerender.exe. Provide --aerender_path, set AERENDER_PATH, or add aerender to PATH."
    )
    sys.exit(1)


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
    Heuristic for Concurrency tuned for high-core, high-RAM hosts (e.g. dual EPYC + 1 TB).
    Balances CPU threads, RAM per process, and MaxConcurrency.
    """
    logical = psutil.cpu_count(logical=True) or 8

    # Try to read total RAM; fall back if psutil is not available.
    try:
        vm = psutil.virtual_memory()
        total_ram_gb = vm.total / float(1024 ** 3)
    except Exception:
        total_ram_gb = 0.0

    # Ensure we have a sane per-process RAM budget
    try:
        ram_per_proc_gb = float(getattr(args, "ram_per_process_gb", 0.0) or 0.0)
    except Exception:
        ram_per_proc_gb = 0.0
    if ram_per_proc_gb <= 0.0:
        ram_per_proc_gb = 32.0  # sensible default for large hosts

    # Use only a fraction of total RAM for children so we leave room for AE / OS / other tasks.
    if total_ram_gb > 0.0:
        usable_ram_gb = max(1.0, total_ram_gb * 0.8)
        max_by_ram = max(1, int(usable_ram_gb // ram_per_proc_gb))
    else:
        usable_ram_gb = 0.0
        max_by_ram = logical

    # Thread-based limit
    mfr_threads = getattr(args, "mfr_threads", 0) or 0
    try:
        mfr_threads = int(mfr_threads)
    except Exception:
        mfr_threads = 0

    if args.disable_mfr:
        # Classic AE style: more, lighter-weight processes.
        target_threads_per_proc = max(8, mfr_threads or 8)
    else:
        # MFR enabled: fewer, heavier processes.
        target_threads_per_proc = max(16, mfr_threads or 16)

    base_by_threads = max(1, logical // target_threads_per_proc)

    # Combine limits
    base = min(max_by_ram, base_by_threads)

    # Respect MaxConcurrency if provided
    max_conc = getattr(args, "max_concurrency", 0) or 0
    try:
        max_conc = int(max_conc)
    except Exception:
        max_conc = 0
    if max_conc > 0:
        base = min(base, max_conc)

    if base < 1:
        base = 1

    logger.info(
        "Auto concurrency chose %s (logical=%s, total_ram_gb=%.1f, usable_ram_gb=%.1f, "
        "ram_per_proc_gb=%.1f, max_by_ram=%s, base_by_threads=%s, disable_mfr=%s, mfr_threads=%s)",
        base,
        logical,
        total_ram_gb,
        usable_ram_gb,
        ram_per_proc_gb,
        max_by_ram,
        base_by_threads,
        args.disable_mfr,
        target_threads_per_proc,
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


def log_affinity_diagnostics(logger: logging.Logger, error: Exception, pid: int, affinity: List[int]):
    """
    Analyzes affinity failures, specifically looking for Windows Error 87.
    """
    message = f"Failed to set CPU affinity for PID {pid} to {affinity}: {error}"
    if isinstance(error, OSError) and getattr(error, "winerror", None) == 87:
        message += " (WinError 87: invalid parameter; verify CPU IDs, Group assignments, and permissions)"
    logger.warning(message)


def apply_affinity(pid: int, affinity: List[int], logger: logging.Logger, allowed_cpus: Optional[List[int]] = None) -> Optional[List[int]]:
    """
    Attempts to apply CPU affinity with graceful Windows fallback.

    On some Windows builds with more than 64 logical CPUs, psutil delegates to
    the legacy `SetProcessAffinityMask` API, which rejects CPU ids that span
    processor groups and raises ``WinError 87``. When that happens, we retry
    using only the CPUs that are currently allowed for this process (typically
    one processor group) so the render still benefits from pinning instead of
    outright failing.
    """

    if not affinity:
        return None

    # Deduplicate and sanitize
    cleaned: List[int] = []
    for cpu in affinity:
        try:
            cpu_id = int(cpu)
        except (TypeError, ValueError):
            continue
        if cpu_id < 0:
            continue
        if cpu_id in cleaned:
            continue
        cleaned.append(cpu_id)

    if not cleaned:
        return None

    try:
        psutil.Process(pid).cpu_affinity(cleaned)
        return cleaned
    except OSError as ex:
        log_affinity_diagnostics(logger, ex, pid, cleaned)

        # Windows group-aware fallback: restrict to currently allowed CPUs
        if os.name == "nt" and getattr(ex, "winerror", None) == 87 and allowed_cpus:
            # First try a strict intersection with the allowed set; if that is empty
            # (common when the parent process is pinned to a single processor group
            # but the NUMA map lists CPUs across multiple groups), fall back to the
            # full allowed set so the child still benefits from pinning instead of
            # failing outright.
            fallback = [c for c in cleaned if c in allowed_cpus]
            if not fallback:
                fallback = list(dict.fromkeys(allowed_cpus))  # preserve order, dedupe

            try:
                psutil.Process(pid).cpu_affinity(fallback)
                logger.info(f"Fallback affinity for PID {pid} applied: {fallback}")
                return fallback
            except Exception as inner_ex:
                log_affinity_diagnostics(logger, inner_ex, pid, fallback)
                return None
        return None
    except Exception as ex:
        log_affinity_diagnostics(logger, ex, pid, cleaned)
        return None


# -----------------------------
# File Offloader (The "Sidecar")
# -----------------------------

def is_file_stable(filepath: Path) -> bool:
    """
    Checks if a file is ready to be moved.
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

    The worker is intentionally throttled so it never competes with active renders:
    - Processes a small burst of files, then yields for OFFLOAD_SCAN_INTERVAL seconds.
    - When idle, sleeps longer (OFFLOAD_IDLE_INTERVAL) to keep the NAS traffic intermittent.
    - Retries transient file locks but never blocks render scheduling/heartbeat threads.
    """
    logger.info(
        f"Offloader started (burst_limit={OFFLOAD_BURST_LIMIT}, scan_interval={OFFLOAD_SCAN_INTERVAL}s, "
        f"idle_interval={OFFLOAD_IDLE_INTERVAL}s): {local_dir} -> {remote_dir}"
    )

    try:
        remote_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Offloader could not create remote dir {remote_dir}: {e}")

    def process_files(max_files: int) -> int:
        moved = 0
        for local_file in local_dir.glob("*"):
            if moved >= max_files:
                break

            if not local_file.is_file():
                continue

            if not is_file_stable(local_file):
                continue

            dest_path = remote_dir / local_file.name

            for attempt in range(3):
                try:
                    shutil.copy2(local_file, dest_path)
                    os.remove(local_file)
                    logger.info(f"[Offload] Moved {local_file.name}")
                    moved += 1
                    break
                except PermissionError as e:
                    if attempt < 2:
                        logger.warning(
                            f"[Offload] File lock when moving {local_file.name} (attempt {attempt + 1}/3); retrying..."
                        )
                        time.sleep(0.5)
                        continue
                    logger.error(f"[Offload] Failed to move {local_file.name} after retries: {e}")
                except Exception as e:
                    logger.error(f"[Offload] Failed to move {local_file.name}: {e}")
                    break
        return moved

    while not stop_event.is_set():
        moved = process_files(OFFLOAD_BURST_LIMIT)
        if moved:
            # Short pause after a burst so render I/O stays prioritized.
            stop_event.wait(OFFLOAD_SCAN_INTERVAL)
        else:
            # Nothing ready; sleep longer to keep traffic intermittent.
            stop_event.wait(OFFLOAD_IDLE_INTERVAL)

    # Final Cleanup Pass
    logger.info("Offloader received stop signal. Performing final pass...")
    for _ in range(3):
        process_files(OFFLOAD_BURST_LIMIT)
        if not any(local_dir.glob("*")):
            break
        time.sleep(1)

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
    mfr_flag = "OFF" if args.disable_mfr else "ON"
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
    p.add_argument("--max_concurrency", type=int, default=24)
    
    p.add_argument("--spawn_delay", type=float, default=2.0)
    p.add_argument("--child_grace_sec", type=float, default=10.0)
    
    p.add_argument("--kill_on_fail", action="store_true")
    p.add_argument("--no_kill_on_fail", dest="kill_on_fail", action="store_false")
    p.set_defaults(kill_on_fail=True)
    
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
        default=False,
        help=(
            "Disable CPU affinity / NUMA pinning. Default is False so affinity is enabled."
            " On Windows hosts with more than 64 logical CPUs, affinity may still be"
            " constrained by processor groups."
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


def main():
    args = parse_args()
    logger = setup_logging(args.log_file)

    # Normalize aerender path so blank inputs gracefully fall back to env/PATH/defaults.
    args.aerender_path = resolve_aerender_path(args.aerender_path, logger)

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

    logger.info(f"Orchestration: Concurrency={concurrency}, MFR={'OFF' if args.disable_mfr else 'ON'}")
    logger.info(f"Pipeline: NVMe [{local_output_path}] -> NAS [{final_output_path}]")

    if args.disable_affinity and logical_cpus > 64:
        logger.info(
            "Affinity disabled by default on high-core host (logical_cpus=%s).",
            logical_cpus,
        )
    elif args.disable_affinity:
        logger.info(
            "Affinity disabled by global default. Use --enable_affinity to explicitly opt back in."
        )
    else:
        logger.info("Affinity explicitly enabled via --enable_affinity.")

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
                numa_nodes = load_numa_nodes(numa_path, logger)
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
    if args.env_file:
        logger.info(f"Environment overrides loaded from {args.env_file}: {len(env_overrides)} entries")
        if env_overrides:
            # Truncated preview from Version A
            preview_items = list(env_overrides.items())
            preview = ", ".join([f"{k}={v}" for k, v in preview_items[:5]])
            if len(env_overrides) > 5:
                preview += f", ... (+{len(env_overrides) - 5} more)"
            logger.info(f"Environment override preview: {preview}")
        else:
            logger.info("Env file provided but contained no overrides.")
    else:
        logger.info("No env_file provided; using process environment only.")

    # HYBRID: Spawn Plan
    logger.info(f"Spawn plan: {len(ranges)} children across frames {args.start}-{args.end} (per-child ~{math.ceil((args.end - args.start + 1)/len(ranges))} frames)")

    children: List[ChildProc] = []
    out_q: queue.Queue = queue.Queue()
    stop_children_event = threading.Event()

    # Cleanup Helper
    def cleanup_resources():
        logger.info("Shutting down...")
        stop_children_event.set()
        for ch in children:
            if ch.popen.poll() is None:
                try:
                    ch.popen.terminate()
                except:
                    pass
        stop_offload_event.set()
        if offloader_thread.is_alive():
            offloader_thread.join()
        try:
            if local_scratch_dir.exists():
                shutil.rmtree(local_scratch_dir, ignore_errors=True)
                logger.info("Scratch cleared.")
        except Exception as e:
            logger.warning(f"Error clearing scratch: {e}")

    signal.signal(signal.SIGINT, lambda s, f: cleanup_resources())
    signal.signal(signal.SIGTERM, lambda s, f: cleanup_resources())

    last_log_time: Dict[int, float] = {}
    last_log_line: Dict[int, str] = {}

    for i, (s, e) in enumerate(ranges):
        if stop_children_event.is_set(): break
        if i > 0 and args.spawn_delay > 0:
            time.sleep(args.spawn_delay)

        cmd = build_aerender_cmd(args, s, e, str(local_output_path))
        
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

            threading.Thread(target=stream_reader, args=(p.pid, p.stdout, out_q, "LOG"), daemon=True).start()

            children.append(ChildProc(p, (s, e), applied_affinity, proc_handle, time.time()))
            last_log_time[p.pid] = time.time()
            logger.info(f"Launched Worker #{i} (PID {p.pid}) Frames {s}-{e} affinity={applied_affinity}")

        except Exception as ex:
            logger.error(f"Spawn failed: {ex}")
            cleanup_resources()
            sys.exit(1)

    # 5. Monitor Loop
    # ---------------
    last_heartbeat = time.time()
    completion_logged: Set[int] = set()
    stalled_pids: Set[int] = set()
    zero_cpu_hint_emitted = False
    zero_cpu_counts: Dict[int, int] = {}
    zero_cpu_terminated: Set[int] = set()
    render_progress: Set[int] = set()
    child_failures: Dict[int, str] = {}
    last_zero_cpu_diag: float = 0.0
    zero_cpu_global_streak = 0
    job_failed = False
    job_failed_logged = False
    
    while True:
        # Drain Logs (block briefly to avoid busy-wait CPU burn)
        time_to_heartbeat = max(0.0, HEARTBEAT_SECONDS - (time.time() - last_heartbeat))
        try:
            pid, tag, line = out_q.get(timeout=min(0.5, time_to_heartbeat))
            logger.info(f"[PID {pid}] {line}")
            last_log_time[pid] = time.time()
            last_log_line[pid] = line

            lower_line = line.lower()
            if pid not in render_progress:
                if "progress:" in lower_line or "starting composition" in lower_line or "finished composition" in lower_line:
                    render_progress.add(pid)
            if pid not in child_failures:
                if "error code: 14" in lower_line or "unexpected error occurred while exporting" in lower_line:
                    child_failures[pid] = "After Effects Error Code 14 detected"
                    logger.error(
                        f"Detected After Effects Error Code 14 in PID {pid} output; terminating worker to force retry."
                    )
                    try:
                        psutil.Process(pid).terminate()
                    except Exception:
                        pass

                if "could not be found" in lower_line and ".tif" in lower_line:
                    child_failures[pid] = "Rendered frame missing on disk"
                    logger.error(
                        f"PID {pid} reported a missing rendered frame; terminating worker so frames can be retried."
                    )
                    try:
                        psutil.Process(pid).terminate()
                    except Exception:
                        pass

            # Drain any remaining lines quickly without another blocking wait
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

                current_affinity = ch.affinity or []

                if ch.psutil_proc:
                    try:
                        status = ch.psutil_proc.status()
                        # HYBRID: Use blocking 0.05s sample for accuracy (from Version B)
                        cpu = ch.psutil_proc.cpu_percent(interval=0.05)
                        mem_info = ch.psutil_proc.memory_info()
                        mem = mem_info.rss / (1024 ** 2)
                        try:
                            current_affinity = ch.psutil_proc.cpu_affinity()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        status = "access_denied"
                    except Exception as hb_ex:
                        logger.debug(f"Heartbeat psutil read failed for PID {pid}: {hb_ex}")

                # Detect Low CPU (Version B logic)
                if state == "running" and cpu is not None:
                    if pid not in render_progress:
                        running_cpu_zero.append(cpu <= 0.01)
                        if cpu <= 0.01:
                            zero_cpu_counts[pid] = zero_cpu_counts.get(pid, 0) + 1
                        else:
                            zero_cpu_counts[pid] = 0
                    else:
                        zero_cpu_counts[pid] = 0

                # Detect no log output
                last_log = last_log_time.get(pid, ch.start_time)
                if state == "running" and now - last_log >= LOG_SILENCE_TIMEOUT:
                    stalled_children.append(pid)

                # Detect Zombies (Version A logic)
                if status == psutil.STATUS_ZOMBIE or status == "zombie":
                    zombie_pids.append(pid)

                # Format Summary (Version A structure)
                summaries.append(
                    f"PID {pid} {state} frames {ch.frame_range[0]}-{ch.frame_range[1]} "
                    f"elapsed={runtime:.1f}s status={status} cpu%={fmt_metric(cpu)} rss_mb={fmt_metric(mem)} "
                    f"affinity={current_affinity if current_affinity else 'none'} rc={rc}"
                )

            # Print multiline status
            logger.info("Heartbeat Details:\n  " + "\n  ".join(summaries))

            # Diagnostic Warnings
            if zombie_pids:
                logger.warning(f"Heartbeat diagnostic: Zombie renderer processes detected: {zombie_pids}")

            pending_children = [ch for ch in running_children if ch.popen.pid not in render_progress]

            if pending_children and running_cpu_zero and all(running_cpu_zero):
                zero_cpu_global_streak += 1
                zero_cpu_streak = min([zero_cpu_counts.get(ch.popen.pid, 0) for ch in pending_children])
                logger.warning(
                    "Heartbeat diagnostic: All running aerender children are currently reporting "
                    "near-zero CPU (<= 0.01%). They may be in splash/licensing or between render phases. "
                    "Check After Effects UI or licensing state if this persists."
                )
                if zero_cpu_streak >= 2 and now - last_zero_cpu_diag >= HEARTBEAT_SECONDS:
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
                        "reducing --concurrency and/or disabling affinity to rule out pinning/licensing issues."
                    )
                    zero_cpu_hint_emitted = True

                # Hard-stop the job if every worker has been stuck at zero CPU for an
                # extended period without any render progress. This now triggers even when
                # the last log line is not the explicit "Launching After Effects" marker, so
                # we also catch licensing/splash stalls that log only the aerender version
                # banner before hanging.
                launching_only = all(
                    "launching after effects" in last_log_line.get(ch.popen.pid, "").lower()
                    for ch in pending_children
                )
                prolonged_zero_cpu = zero_cpu_global_streak >= ZERO_CPU_STUCK_HEARTBEATS
                long_runtime = (
                    now - min(ch.start_time for ch in pending_children)
                    >= ZERO_CPU_STUCK_HEARTBEATS * HEARTBEAT_SECONDS
                )
                no_progress = pending_children and all(
                    zero_cpu_counts.get(ch.popen.pid, 0) >= ZERO_CPU_STUCK_HEARTBEATS
                    and ch.popen.pid not in render_progress
                    for ch in pending_children
                )

                if prolonged_zero_cpu and long_runtime and (launching_only or no_progress):
                    reason = (
                        "Stuck at launch (zero CPU heartbeats)"
                        if launching_only
                        else "Zero CPU with no render progress"
                    )
                    logger.error(
                        "Workers appear stalled with persistent zero CPU and no render progress. "
                        "Terminating stalled workers so the task can retry those frames."
                    )
                    for ch in pending_children:
                        pid = ch.popen.pid
                        try:
                            psutil.Process(pid).terminate()
                            child_failures[pid] = reason
                            logger.warning(
                                f"Heartbeat diagnostic: PID {pid} reported <=0.01% CPU for "
                                f"{ZERO_CPU_STUCK_HEARTBEATS} consecutive heartbeats and no render progress; "
                                "terminating to break stall."
                            )
                        except Exception:
                            pass
            else:
                zero_cpu_global_streak = 0

            # IMPORTANT: Do NOT kill workers purely on low CPU. AE can legitimately sit at low CPU between
            # heavy phases while still making progress. Stalls are handled via log-silence detection below.
            for stalled_pid in stalled_children:
                if stalled_pid in stalled_pids:
                    continue
                stalled_pids.add(stalled_pid)
                logger.warning(
                    f"Heartbeat diagnostic: PID {stalled_pid} produced no log output for {LOG_SILENCE_TIMEOUT}s; terminating to avoid long hangs. "
                    "Consider relaunching with reduced concurrency if this persists."
                )
                try:
                    psutil.Process(stalled_pid).terminate()
                except Exception as stall_ex:
                    logger.debug(f"Failed to terminate stalled PID {stalled_pid}: {stall_ex}")

            last_heartbeat = now

        # Check Status
        all_done = True
        any_failed = False
        fail_pid = None
        fail_rc = None
        
        for ch in children:
            rc = ch.popen.poll()
            failure_reason = child_failures.get(ch.popen.pid)
            if rc is None:
                all_done = False
                continue

            # Log completion once
            if ch.popen.pid not in completion_logged:
                duration = time.time() - ch.start_time
                logger.info(f"Worker PID {ch.popen.pid} completed frames {ch.frame_range[0]}-{ch.frame_range[1]} with code {rc} after {duration:.1f}s")
                completion_logged.add(ch.popen.pid)

            if failure_reason:
                any_failed = True
                fail_pid = ch.popen.pid
                fail_rc = failure_reason
            elif rc != 0:
                any_failed = True
                fail_pid = ch.popen.pid
                fail_rc = rc

        if any_failed:
            job_failed = True
            if not job_failed_logged:
                logger.error(f"Worker PID {fail_pid} failed with RC {fail_rc}")
                if args.kill_on_fail:
                    logger.error(
                        "kill_on_fail enabled; allowing running renders to finish but job will exit non-zero once they complete."
                    )
                else:
                    logger.error("Continuing to monitor remaining workers; job will report failure when monitoring completes.")
                job_failed_logged = True

        if all_done:
            if job_failed:
                logger.error("One or more workers failed; exiting with failure status after allowing other renders to finish.")
                cleanup_resources()
                sys.exit(1)
            logger.info("All render processes completed successfully.")
            break
        
        if stop_children_event.is_set():
            cleanup_resources()
            sys.exit(1)
        
        # HYBRID: Faster tick (from Version B) without busy-waiting when quiet
        # Block on the stop event instead of spinning so idle loops keep CPU near zero.
        # When the log queue is busy, wake quickly; when quiet, wait longer but still
        # bounded by the heartbeat interval so diagnostics fire on time.
        idle_wait = 0.05 if not out_q.empty() else min(2.0, max(0.2, time_to_heartbeat))
        stop_children_event.wait(idle_wait)

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