# stmpo_wrapper.py
"""
STMPO After Effects Orchestrator (Production Hardened)

- Splits a single Deadline Cloud task frame range into N sub-ranges.
- Spawns N aerender.exe processes in parallel.
- Assigns NUMA + processor-group safe CPU affinity per process.
- Aggregates child logs into parent log with PID-prefixing.
- Fail-fast: if any child fails, terminates siblings.

V2 UPDATES:
- Fixed Argument Logic: kill_on_fail can now be properly disabled.
- Safety Guard: Blocks parallel rendering of single video files (.mov/.mp4).
- Race Condition Fix: Pre-creates output directories.
- Staggered Launch: Prevents I/O storms.
- Recursive Kill: Cleans up zombie sub-processes.

Designed for: Dual EPYC / Threadripper Windows hosts under Deadline Cloud.
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
import platform
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil


# -----------------------------
# Defaults / Constants
# -----------------------------

DEFAULT_AERENDER = r"E:\DCC\Adobe\Adobe After Effects 2024\Support Files\aerender.exe"
DEFAULT_NUMA_MAP = r"C:\Deadline\Scripts\numa_map.json"
PROC_GROUP_SIZE = 64



# -----------------------------
# Windows processor-group affinity helpers
# -----------------------------
# On systems with >64 logical CPUs, Windows uses processor groups (max 64 logical CPUs per group).
# psutil / SetProcessAffinityMask cannot set affinity across groups directly.
# We therefore:
#   1) Compute a group-local 64-bit mask for each affinity block.
#   2) Move the child process' main thread into that group via SetThreadGroupAffinity.
#   3) Apply the group-local mask to the process via SetProcessAffinityMask.
#
# On Windows 11 / Server 2022, processes span all groups by default; calling
# SetThreadGroupAffinity intentionally restricts to a single group and sets that as primary.
# See Microsoft Processor Groups documentation.
_IS_WINDOWS = (os.name == "nt")

if _IS_WINDOWS:
    TH32CS_SNAPTHREAD = 0x00000004
    THREAD_QUERY_INFORMATION = 0x0040
    THREAD_SET_INFORMATION = 0x0020
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_SET_INFORMATION = 0x0200

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    class GROUP_AFFINITY(ctypes.Structure):
        _fields_ = [
            ("Mask", ctypes.c_ulonglong),
            ("Group", wintypes.WORD),
            ("Reserved", wintypes.WORD * 3),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    CreateToolhelp32Snapshot = kernel32.CreateToolhelp32Snapshot
    CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    Thread32First = kernel32.Thread32First
    Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    Thread32First.restype = wintypes.BOOL

    Thread32Next = kernel32.Thread32Next
    Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    Thread32Next.restype = wintypes.BOOL

    OpenThread = kernel32.OpenThread
    OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenThread.restype = wintypes.HANDLE

    OpenProcess = kernel32.OpenProcess
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenProcess.restype = wintypes.HANDLE

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    SetThreadGroupAffinity = getattr(kernel32, "SetThreadGroupAffinity", None)
    if SetThreadGroupAffinity:
        SetThreadGroupAffinity.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(GROUP_AFFINITY),
            ctypes.POINTER(GROUP_AFFINITY),
        ]
        SetThreadGroupAffinity.restype = wintypes.BOOL

    SetProcessAffinityMask = kernel32.SetProcessAffinityMask
    SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_ulonglong]
    SetProcessAffinityMask.restype = wintypes.BOOL


    def _get_main_thread_id(pid):
        """Best-effort: return the smallest thread id owned by pid."""
        snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if snap == wintypes.HANDLE(-1).value:
            return None
        try:
            te = THREADENTRY32()
            te.dwSize = ctypes.sizeof(THREADENTRY32)
            ok = Thread32First(snap, ctypes.byref(te))
            tids = []
            while ok:
                if te.th32OwnerProcessID == pid:
                    tids.append(int(te.th32ThreadID))
                ok = Thread32Next(snap, ctypes.byref(te))
            return min(tids) if tids else None
        finally:
            CloseHandle(snap)


    def _compute_group_and_mask(cpus):
        g = int(cpus[0] // PROC_GROUP_SIZE)
        mask = 0
        for c in cpus:
            cg = int(c // PROC_GROUP_SIZE)
            if cg != g:
                raise ValueError("Affinity block spans multiple processor groups.")
            mask |= (1 << int(c % PROC_GROUP_SIZE))
        return g, mask


    def apply_group_affinity(pid, cpus, logger):
        """Apply processor-group local affinity to pid."""
        try:
            group, mask = _compute_group_and_mask(cpus)
        except Exception as ex:
            logger.warning("Affinity plan invalid for PID %s: %s" % (pid, ex))
            return False

        tid = None
        # Retry because very new processes may not show threads immediately.
        for _ in range(10):
            tid = _get_main_thread_id(pid)
            if tid is not None:
                break
            time.sleep(0.05)

        if tid is None:
            logger.warning("Could not locate main thread for PID %s; skipping group affinity." % pid)
            return False

        hThread = OpenThread(THREAD_QUERY_INFORMATION | THREAD_SET_INFORMATION, False, tid)
        if not hThread:
            logger.warning("OpenThread failed for TID %s (PID %s); skipping group affinity." % (tid, pid))
            return False
        try:
            if SetThreadGroupAffinity:
                ga = GROUP_AFFINITY(ctypes.c_ulonglong(mask), wintypes.WORD(group), (wintypes.WORD * 3)(0, 0, 0))
                prev = GROUP_AFFINITY()
                ok = SetThreadGroupAffinity(hThread, ctypes.byref(ga), ctypes.byref(prev))
                if not ok:
                    err = ctypes.get_last_error()
                    logger.warning("SetThreadGroupAffinity failed for PID %s group %s err=%s." % (pid, group, err))
                    return False
            else:
                logger.warning("SetThreadGroupAffinity not available on this host; skipping group affinity.")
                return False
        finally:
            CloseHandle(hThread)

        hProc = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_SET_INFORMATION, False, pid)
        if not hProc:
            logger.warning("OpenProcess failed for PID %s; skipping process mask." % pid)
            return False
        try:
            ok = SetProcessAffinityMask(hProc, ctypes.c_ulonglong(mask))
            if not ok:
                err = ctypes.get_last_error()
                logger.warning("SetProcessAffinityMask failed for PID %s mask=%s err=%s." % (pid, hex(mask), err))
                return False
        finally:
            CloseHandle(hProc)

        logger.info("Applied group affinity to PID %s: group=%s, mask=%s" % (pid, group, hex(mask)))
        return True
else:
    def apply_group_affinity(pid, cpus, logger):
        return False

# -----------------------------
# Data structures
# -----------------------------

@dataclass
class CpuPool:
    node_index: int
    group_index: int
    cpus: List[int]


@dataclass
class ChildProc:
    popen: subprocess.Popen
    frame_range: Tuple[int, int]
    affinity: Optional[List[int]]


# -----------------------------
# Argument parsing
# -----------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="STMPO wrapper for AE aerender parallelization on a single host."
    )

    # Required render inputs
    p.add_argument("--project", required=True, help="Path to .aep project file.")
    p.add_argument("--output", required=True,
                   help="Output directory OR full aerender output pattern.")
    p.add_argument("--start", type=int, required=True, help="Start frame (inclusive).")
    p.add_argument("--end", type=int, required=True, help="End frame (inclusive).")

    # Optional AE targeting
    p.add_argument("--comp", default=None, help="Optional comp name.")
    p.add_argument("--rqindex", type=int, default=None, help="Optional render queue index.")
    p.add_argument("--rs_template", default=None, help="Optional Render Settings template.")
    p.add_argument("--om_template", default=None, help="Optional Output Module template.")

    # Output control
    p.add_argument("--output_pattern", default="[#####].png",
                   help="Filename/pattern appended if --output is a directory.")
    p.add_argument("--output_is_pattern", action="store_true",
                   help="Treat --output as a full output pattern even if it looks like a directory.")

    # Concurrency / resources
    p.add_argument("--concurrency", type=int, default=-1,
                   help=">=1 to force, -1 for auto.")
    p.add_argument("--max_concurrency", type=int, default=48,
                   help="Upper cap even in auto mode.")
    p.add_argument("--ram_per_process_gb", type=float, default=10.0,
                   help="Auto-mode RAM budget per aerender instance.")

    # MFR options
    p.add_argument("--mfr_threads", type=int, default=2,
                   help="Threads per aerender instance when MFR on.")
    p.add_argument("--disable_mfr", action="store_true",
                   help="Disable MFR for each aerender instance.")

    # Affinity / topology
    p.add_argument("--aerender_path", default=DEFAULT_AERENDER,
                   help="Override path to aerender.exe.")
    p.add_argument("--numa_map", default=DEFAULT_NUMA_MAP,
                   help="Path to numa_map.json produced from Coreinfo -n.")
    p.add_argument("--disable_affinity", action="store_true",
                   help="Do not set CPU affinity.")

    # Behavior / safety
    # FIX: Logic corrected to allow disabling kill_on_fail
    p.add_argument("--no_kill_on_fail", dest="kill_on_fail", action="store_false",
                   help="Disable terminating siblings if a child fails (Default: kill enabled).")
    p.set_defaults(kill_on_fail=True)

    p.add_argument("--child_grace_sec", type=int, default=10,
                   help="Seconds to wait after terminate before kill().")
    p.add_argument("--spawn_delay", type=float, default=2.0,
                   help="Seconds to wait between launching children to prevent I/O storms.")
    p.add_argument("--dry_run", action="store_true",
                   help="Print commands/affinity plan but do not execute.")

    # Env + logging
    p.add_argument("--env_file", default=None, help="Optional JSON file of env vars.")
    p.add_argument("--log_file", default=None, help="Optional local log file path.")

    return p.parse_args()


# -----------------------------
# Logging
# -----------------------------

def setup_logging(log_file: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger("STMPO")
    
    # FIX: Avoid duplicate handlers if re-initialized
    if logger.handlers:
        return logger
        
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.propagate = False
    return logger


# -----------------------------
# Topology / affinity planning
# -----------------------------

def load_numa_nodes(path: str) -> List[List[int]]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"NUMA map not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    nodes = data.get("nodes", [])
    return [sorted(set(map(int, n))) for n in nodes]


def numa_nodes_to_pools(numa_nodes: List[List[int]]) -> List[CpuPool]:
    pools: List[CpuPool] = []
    for node_i, cpus in enumerate(numa_nodes):
        groups: Dict[int, List[int]] = {}
        for c in cpus:
            g = c // PROC_GROUP_SIZE
            groups.setdefault(g, []).append(c)
        for g, gcpus in groups.items():
            pools.append(CpuPool(node_index=node_i, group_index=g, cpus=sorted(gcpus)))
    return pools


def allocate_processes_to_pools(num_procs: int, pools: List[CpuPool]) -> Dict[int, int]:
    if num_procs <= 0: return {}
    sizes = [len(p.cpus) for p in pools]
    total = sum(sizes)
    if total == 0: return {i: 0 for i in range(len(pools))}

    raw = [num_procs * (s / total) for s in sizes]
    base = [int(math.floor(r)) for r in raw]
    remainder = num_procs - sum(base)

    frac = sorted([(i, raw[i] - base[i]) for i in range(len(pools))], key=lambda x: x[1], reverse=True)
    for i, _ in frac[:remainder]:
        base[i] += 1
    return {i: base[i] for i in range(len(pools))}


def split_cpus_evenly(cpus: List[int], k: int) -> List[List[int]]:
    if k <= 1: return [cpus]
    n = len(cpus)
    blocks: List[List[int]] = []
    start = 0
    for i in range(k):
        end = start + math.ceil((n - start) / (k - i))
        blocks.append(cpus[start:end])
        start = end
    return blocks


def build_affinity_blocks(num_procs: int, pools: List[CpuPool]) -> List[List[int]]:
    if num_procs <= 0: return []
    alloc = allocate_processes_to_pools(num_procs, pools)
    pool_blocks: List[List[List[int]]] = []
    
    for pool_i, pool in enumerate(pools):
        k = alloc.get(pool_i, 0)
        # FIX: Safety Clamp - never ask for more blocks than available CPUs
        k = min(k, len(pool.cpus))
        
        if k <= 0:
            pool_blocks.append([])
            continue
        pool_blocks.append(split_cpus_evenly(pool.cpus, k))

    affinities: List[List[int]] = []
    exhausted = False
    idx = 0
    while not exhausted and len(affinities) < num_procs:
        exhausted = True
        for blocks in pool_blocks:
            if idx < len(blocks):
                affinities.append(blocks[idx])
                exhausted = False
                if len(affinities) >= num_procs: break
        idx += 1
    return affinities[:num_procs]


# -----------------------------
# Concurrency & range splitting
# -----------------------------

def auto_concurrency(args: argparse.Namespace, logger: logging.Logger) -> int:
    logical = psutil.cpu_count(logical=True) or 1
    threads_per_proc = 1 if args.disable_mfr else max(1, args.mfr_threads)
    cpu_cap = int((logical / threads_per_proc) * 0.90)
    avail_gb = psutil.virtual_memory().available / (1024 ** 3)
    mem_cap = int(avail_gb / max(0.1, args.ram_per_process_gb))
    chosen = max(1, min(cpu_cap, mem_cap, args.max_concurrency))
    logger.info(f"Auto concurrency: {chosen} (CPU cap:{cpu_cap}, RAM cap:{mem_cap})")
    return chosen


def split_ranges(start: int, end: int, n: int) -> List[Tuple[int, int]]:
    total = end - start + 1
    n_eff = min(n, total)
    chunk = math.ceil(total / n_eff)
    ranges: List[Tuple[int, int]] = []
    cur = start
    while cur <= end:
        r_start = cur
        r_end = min(end, cur + chunk - 1)
        ranges.append((r_start, r_end))
        cur = r_end + 1
    return ranges


# -----------------------------
# Subprocess streaming
# -----------------------------

def stream_reader(pid: int, stream, out_q: queue.Queue, label: str):
    try:
        for line in iter(stream.readline, ""):
            if not line: break
            out_q.put(f"[PID {pid} {label}] {line.rstrip()}")
    finally:
        try: stream.close()
        except: pass


# -----------------------------
# Main orchestration
# -----------------------------

def resolve_output(args: argparse.Namespace, concurrency: int, logger: logging.Logger) -> str:
    out = args.output
    if args.output_is_pattern:
        return out

    looks_like_pattern = ("[" in out) or ("#" in out)
    looks_like_file = any(out.lower().endswith(ext) for ext in
                          [".png", ".exr", ".jpg", ".jpeg", ".tif", ".tiff", ".mov", ".mp4", ".avi", ".mxf"])
    
    # FIX: Safety check for single movie files
    movie_exts = (".mov", ".mp4", ".mxf", ".avi", ".webm")
    is_movie = out.lower().endswith(movie_exts)
    
    if is_movie and concurrency > 1 and not looks_like_pattern:
        logger.error("CRITICAL: Output looks like a single movie file but concurrency > 1.")
        logger.error("Parallel rendering will corrupt the file. Use image sequences or set --concurrency 1.")
        sys.exit(2)

    if looks_like_pattern or looks_like_file:
        return out
    
    # treat as directory
    return str(Path(out) / args.output_pattern)


def build_aerender_cmd(args: argparse.Namespace, s: int, e: int, output_pattern: str) -> List[str]:
    cmd = [
        args.aerender_path,
        "-project", args.project,
        "-output", output_pattern,
        "-sound", "OFF",
        "-s", str(s),
        "-e", str(e),
    ]
    if args.comp: cmd += ["-comp", args.comp]
    if args.rqindex: cmd += ["-rqindex", str(args.rqindex)]
    if args.rs_template: cmd += ["-RStemplate", args.rs_template]
    if args.om_template: cmd += ["-OMtemplate", args.om_template]
    if args.disable_mfr: cmd += ["-mfr", "off"]
    else: cmd += ["-mfr", "on", "-mfr_num_threads", str(args.mfr_threads)]
    return cmd


def load_env_overrides(env_file: Optional[str]) -> Dict[str, str]:
    if not env_file: return {}
    p = Path(env_file)
    if not p.exists(): raise FileNotFoundError(f"env_file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items()}


def terminate_children(children: List[ChildProc], logger: logging.Logger, grace_sec: int):
    # 1. Gentle Terminate
    for ch in children:
        if ch.popen.poll() is None:
            logger.warning(f"Terminating child PID {ch.popen.pid} ...")
            try: ch.popen.terminate()
            except: pass

    # 2. Wait
    deadline = time.time() + grace_sec
    while time.time() < deadline:
        if all(ch.popen.poll() is not None for ch in children): return
        time.sleep(0.25)

    # 3. Recursive Kill (Nuclear)
    for ch in children:
        if ch.popen.poll() is None:
            logger.error(f"Force killing child PID {ch.popen.pid}...")
            try:
                parent = psutil.Process(ch.popen.pid)
                for child in parent.children(recursive=True):
                    try: child.kill()
                    except: pass
                parent.kill()
            except: pass


def main():
    args = parse_args()
    logger = setup_logging(args.log_file)

    def log_affinity_diagnostics(ex: Exception):
        msg = str(ex).lower()
        if "winerror 87" in msg or "parameter is incorrect" in msg:
            logger.warning("Affinity diagnostic: Windows rejected the CPU list (WinError 87).")
            logger.warning("Hint: This typically happens when CPU indices span multiple processor"  \
                           " groups. Validate numa_map.json against the host and try --disable_affinity if"  \
                           " the map is outdated.")
            logger.warning("Hint: psutil on Windows cannot cross processor groups. STMPO attempts group-aware pinning first; if you still see WinError 87, keep each affinity set"  \
                           " within a single 64-core group or disable affinity.")
        else:
            logger.warning("Affinity diagnostic: failed to set CPU affinity; affinity attempts will be"  \
                           " skipped for remaining children.")

    if args.start > args.end:
        logger.error(f"Invalid frame range: start={args.start} > end={args.end}")
        sys.exit(2)

    proj = Path(args.project)
    if not proj.exists():
        logger.error(f"Project not found: {proj}")
        sys.exit(2)

    aer = Path(args.aerender_path)
    if not aer.exists():
        logger.error(f"aerender.exe not found: {aer}")
        sys.exit(2)

    requested = args.concurrency if args.concurrency >= 1 else auto_concurrency(args, logger)
    total_frames = args.end - args.start + 1
    concurrency = min(requested, total_frames)
    
    output_pattern = resolve_output(args, concurrency, logger)
    
    # FIX: Pre-create output directory to prevent race conditions
    try:
        out_dir = Path(output_pattern).parent
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning(f"Could not pre-create output dir: {e}")

    logger.info(f"Resolved output pattern: {output_pattern}")
    logger.info(f"Using concurrency={concurrency}")

    affinities: List[List[int]] = []
    if not args.disable_affinity:
        try:
            numa_nodes = load_numa_nodes(args.numa_map)
            pools = numa_nodes_to_pools(numa_nodes)
            if pools:
                affinities = build_affinity_blocks(concurrency, pools)
                logger.info(f"Affinity active: {len(affinities)} blocks built across {len(pools)} pools.")
            else:
                logger.warning("No CPU pools found; affinity disabled.")
        except Exception as e:
            logger.error(f"Topology error: {e}. Affinity disabled.")
    else:
        logger.info("Affinity disabled by flag.")

    ranges = split_ranges(args.start, args.end, concurrency)
    env_overrides = load_env_overrides(args.env_file)
    child_env = os.environ.copy()
    child_env.update(env_overrides)

    if args.dry_run:
        logger.info("DRY RUN: Commands/affinities only.")
        for i, (s, e) in enumerate(ranges):
            cmd = build_aerender_cmd(args, s, e, output_pattern)
            aff = affinities[i] if affinities else None
            logger.info(f"[DRY] #{i} frames {s}-{e} aff={aff} cmd={' '.join(cmd)}")
        sys.exit(0)

    stop_event = threading.Event()
    def on_signal(signum, frame):
        logger.warning(f"Received signal {signum}; stopping children.")
        stop_event.set()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    children: List[ChildProc] = []
    out_q: queue.Queue = queue.Queue()

    logger.info(f"Starting spawn sequence with {args.spawn_delay}s stagger delay...")

    affinity_applicable = bool(affinities) and not args.disable_affinity

    for i, (s, e) in enumerate(ranges):
        if stop_event.is_set(): break
        if i > 0 and args.spawn_delay > 0: time.sleep(args.spawn_delay)

        cmd = build_aerender_cmd(args, s, e, output_pattern)
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
                bufsize=1
            )

            aff = None
            if affinity_applicable and affinities and i < len(affinities):
                aff = affinities[i]
                try:
                    applied = False
                    if _IS_WINDOWS and (psutil.cpu_count(logical=True) or 0) > PROC_GROUP_SIZE:
                        applied = apply_group_affinity(p.pid, aff, logger)
                    if not applied:
                        # Fallback to psutil affinity within primary group
                        psutil.Process(p.pid).cpu_affinity(aff)
                        applied = True
                    if not applied:
                        raise RuntimeError("Affinity not applied")
                except Exception as ex:
                    logger.warning(f"Affinity warning PID {p.pid}: {ex}")
                    log_affinity_diagnostics(ex)
                    affinity_applicable = False

            threading.Thread(target=stream_reader, args=(p.pid, p.stdout, out_q, "LOG"), daemon=True).start()
            children.append(ChildProc(popen=p, frame_range=(s, e), affinity=aff))
        except Exception as spawn_ex:
            logger.error(f"Failed to spawn child #{i}: {spawn_ex}")
            stop_event.set()
            break
            
    # FIX: Explicit check if no children were spawned
    if not children:
        logger.error("No children were spawned. Aborting.")
        sys.exit(1)

    failed: Optional[Tuple[int, int]] = None
    while True:
        try:
            msg = out_q.get(timeout=0.25)
            print(msg, flush=True)
        except queue.Empty: pass

        if stop_event.is_set():
            terminate_children(children, logger, args.child_grace_sec)
            sys.exit(3)

        alive = False
        for ch in children:
            code = ch.popen.poll()
            if code is None:
                alive = True
                continue
            if code != 0 and failed is None:
                failed = (ch.popen.pid, code)

        if failed and args.kill_on_fail:
            pid, code = failed
            logger.error(f"Child PID {pid} failed with code {code}; terminating siblings.")
            terminate_children(children, logger, args.child_grace_sec)
            break

        if not alive: break

    codes = [(ch.popen.pid, ch.popen.returncode) for ch in children]
    bad = [(pid, c) for pid, c in codes if c != 0]

    if bad:
        logger.error(f"STMPO failure; bad children: {bad}")
        sys.exit(1)

    logger.info("STMPO complete; all children succeeded.")
    sys.exit(0)


if __name__ == "__main__":
    main()