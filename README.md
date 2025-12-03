# STMPO for After Effects (AWS Deadline Cloud)

Single-Task Multi-Process Orchestrator (STMPO) for Adobe After Effects running on **AWS Deadline Cloud**.

This repo extends the official AWS Deadline Cloud for After Effects integration with:

- A custom **ScriptUI submitter** (`DeadlineCloudSubmitter_STMPO.jsx`)
- A **STMPO-enabled** job template bundle
- A small collection of **worker-side helper scripts**

Together, they let a single Deadline task fan out into **multiple parallel `aerender.exe` processes** on a large multi-core worker, with optional **NUMA-aware CPU affinity** and automatic **font deployment**.

> One Deadline task → many local `aerender` workers, with optional core pinning and AE Multi-Frame Rendering awareness.

---

## Features

- 🧠 **STMPO orchestration**
  - Splits a task’s frame range into N sub-ranges.
  - Spawns N child `aerender.exe` processes in parallel on the worker.
  - Tracks children, aggregates logs, and enforces a configurable **fail-fast** policy.

- 🧵 **Multi-Frame Rendering–aware**
  - Uses AE’s `-mfr` flag per child.
  - Supports explicit on/off for MFR via a template parameter.
  - Auto-mode chooses concurrency based on:
    - Logical CPU count,
    - MFR on/off state,
    - Target threads per process.

- 🚀 **High-core auto-concurrency (dual-EPYC / 1 TB–friendly)**
  - Uses `psutil` to inspect **logical cores** and **total RAM** at runtime.
  - Balances:
    - CPU threads,
    - RAM-per-process,
    - `MaxConcurrency` from the job template.
  - Default high-performance profile (as shipped in this repo):
    - `MaxConcurrency` = **24** (upper bound per STMPO task).
    - `RamPerProcessGB` = **32.0** (approx RAM budget per `aerender` child).
    - With 1 TB RAM and an 80% safety factor, this allows ~24 workers before RAM becomes the limit.
  - On a large host (e.g., dual AMD EPYC 7742, 128 cores / 256 threads, 1 TB RAM):
    - With **MFR enabled**, auto-concurrency typically chooses ~16 workers.
    - With **MFR disabled**, it can scale closer to the `MaxConcurrency` cap.

- ⚙️ **NUMA / CPU affinity (optional but ON by default)**
  - Reads a JSON **NUMA map** (`numa_map.json`) to understand physical CPU layout.
  - Slices CPU pools into affinity blocks and pins each `aerender` child to its own block.
  - Graceful fallback: if affinity or topology setup fails, STMPO logs a warning and continues without pinning.
  - Affinity / NUMA pinning is **enabled by default**:
    - Can be turned **off** via the `DisableAffinity` parameter in the job template / submitter UI.
    - Equivalent CLI flags are `--disable_affinity` / `--enable_affinity`.
  - On Windows hosts with more than 64 logical CPUs, STMPO uses processor-group–aware fallbacks rather than silently disabling affinity.

- 🗂 **Job template integration**
  - OpenJD job templates for both **video** and **image sequence** renders.
  - STMPO controls exposed in the AWS Deadline Cloud submission UI:
    - Concurrency (`Concurrency`, `MaxConcurrency`),
    - RAM per process (`RamPerProcessGB`),
    - MFR toggle (`DisableMFR`),
    - Affinity toggle (`DisableAffinity`),
    - Spawn timing and fail-fast behaviour.
  - Job environment that **creates output directories** on the worker before rendering.

- 🔤 **Font deployment**
  - Detects fonts used on the submitter machine, attaches them to the job, and installs them on the worker at job start.
  - Cleans up fonts at job end, so workers stay tidy.

---

## Performance profiles (recommended usage)

Although all controls are individually tunable, the template is designed around two practical profiles:

### 1. Hero (default) – MFR ON, moderate concurrency

Best for heavy, modern comps and plugin stacks that benefit from AE’s Multi-Frame Rendering.

Typical settings (and what this repo’s defaults approximate on a high-core host):

- `DisableMFR` = **false** (MFR **enabled**)
- `RamPerProcessGB` = **32–64**
- `MaxConcurrency` = **12–24**
- `Concurrency` = **0** (auto)

STMPO will:

- Spawn a **smaller number of “fat” `aerender` workers**,
- Let each worker use MFR internally (many threads),
- Keep each process well-fed with RAM,
- Pin workers across NUMA nodes for stable scaling.

### 2. Farm mode – MFR OFF, higher process count

Useful for well-behaved comps where you want more, lighter-weight `aerender` instances instead of a few heavyweight MFR processes.

Suggested starting point:

- `DisableMFR` = **true**
- `RamPerProcessGB` = **24–32**
- `MaxConcurrency` = **24–32**
- `Concurrency` = **0** (auto) or explicitly set to **16–24**

STMPO will:

- Use a **higher count** of concurrent `aerender` workers,
- Each with lower per-process RAM and fewer threads,
- Still constrained by:
  - Total RAM (~80% cap),
  - Logical CPU count,
  - `MaxConcurrency`.

In both profiles, you can always explicitly override `Concurrency` if you want a fixed worker count instead of auto-detection.

---

## Repository layout

From the repo root:

```text
.
├─ DeadlineCloudSubmitter_STMPO.jsx
└─ DeadlineCloudSubmitter_Assets/
   └─ JobTemplate/
      ├─ template.json
      ├─ image_template.json
      ├─ video_template.json
      ├─ job_environments_fragment.json
      ├─ parameter_definitions_image_fragment.json
      ├─ parameter_definitions_video_fragment.json
      ├─ step_image_fragment.json
      ├─ step_video_fragment.json
      └─ scripts/
         ├─ call_aerender.py
         ├─ create_output_directory.py
         ├─ font_manager.py
         ├─ get_user_fonts.py
         ├─ numa_map.json
         └─ stmpo_wrapper.py