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
  - Supports explicit on/off for MFR.
  - Auto-mode can choose a concurrency based on logical cores and MFR settings.

- ⚙️ **NUMA / CPU affinity (optional)**
  - Reads a JSON **NUMA map** (`numa_map.json`) to understand physical CPU layout.
  - Slices CPU pools into affinity blocks and pins each `aerender` child to its own block.
  - Graceful fallback: if affinity or topology fails, STMPO logs a warning and continues without pinning.

- 🗂 **Job template integration**
  - OpenJD job templates for both **video** and **image sequence** renders.
  - STMPO controls exposed in the AWS Deadline Cloud submission UI.
  - Job environment that **creates output directories** on the worker before rendering.

- 🔤 **Font deployment**
  - Detects fonts used on the submitter machine, attaches them to the job, and installs them on the worker at job start.
  - Cleans up fonts at job end, so workers stay tidy.

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
