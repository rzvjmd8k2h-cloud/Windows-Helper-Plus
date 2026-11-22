# Windows Helper Plus Beta 0.2

English | [简体中文](README.md)

**AI-powered full-object diagnosis, interactive problem localization, and controlled repair for Windows.**

![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D4)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB)
![UI](https://img.shields.io/badge/UI-PySide6%206.12-41CD52)
![License](https://img.shields.io/badge/license-MIT-green)

Built on the *Architecture Design* document, this project implements the complete loop of **Agent (native Qt UI) – Planner (latest DeepSeek reasoning model) – Tools (native Windows forensics) – Action Executor (repair actions after user confirmation)**. Every conclusion is produced by the AI and bound to evidence IDs `E#`; locally the app only collects evidence, runs tools and executes controlled commands. It no longer generates local diagnostic conclusions.

> This repository contains source code, scripts and tests only. The local Qwen model and the llama.cpp binaries are large and are not tracked by Git; place them yourself following `models/README.md` and `runtime/README.md`. The main diagnosis still works when they are absent.


## 1. v0.2 Capability Overview

| Capability | Implementation |
| --- | --- |
| Full-object targets | Files, folders, drives, the local system, PnP devices, printers |
| All analysis done by AI | Local code only collects evidence; there is no "local read-only analysis / local report" entry point, and a missing API key fails loudly |
| Plan first | After baseline collection, the first Planner round must emit `action=plan`, which is shown on the "Execution Plan" page before anything continues |
| Multi-round evidence gathering | Single-tool `call_tool` and batched `call_tools` (1–8 tools) |
| Interactive problem localization | `ask_user` opens a native dialog supporting options and free text; answers feed into subsequent context |
| Controlled repair | `propose_action` proposes 1–5 commands; the UI lists each one with a checkbox, and high-risk actions require typing `RUN`; `requires_admin` actions run through UAC |
| Command execution | Arbitrary `cmd` and `powershell` commands/scripts, with user confirmation before ordinary execution; a sandbox fallback strategy is also provided |
| Post-repair verification | Action results are fed back to the Planner as `action_execute` evidence; the AI keeps gathering evidence, rolls back, or concludes |
| No app-level token/step limits | No `max_tokens` is sent and no maximum round count is set; when the server returns `finish_reason=length`, the JSON is continued automatically, so a single-response limit never becomes a failed round |
| Long-context compression | Evidence `data` is compressed per round under a budget, but all `E#` IDs, summaries, diagnostic logs, user Q&A and action history are always retained |
| Timing records | Session start/end, per-evidence/per-step timestamps, per-action start/end/duration, and live elapsed time in the UI |
| API key persistence fix | In-app storage takes precedence over environment variables; DPAPI encryption; read-back verified immediately after saving; the settings save button is the default button |
| UI language | Switch Chinese/English in settings; the current window refreshes immediately after saving; only UI strings are translated, never AI output, tool output or logs |
| Context menu coverage | Files, all filesystem objects, folders, drives, folder background, desktop, "This PC", the "Printers" folder; the This PC / Printers entries open the object picker |
| Tool completeness | 24 registered tools covering system, files, directories, **project manifests/dependency architecture**, logs, reliability, WER, **crash dumps**, updates, drivers, devices, printers, **network adapters/network devices**, SFC/DISM, PnPUtil, SignTool, wevtutil, web retrieval, and the **Advanced Ten** |
| Broad file/driver coverage | The baseline also inventories devices, printers and network adapters; file version resources, signing subject, driver provider, device vendor and product name are used as brand clues |
| Brand/model heuristics | When a brand/model can be inferred, the AI searches the vendor site for drivers/known issues; when it is uncertain or physical-layer information is missing, `ask_user` prompts for it |
| UAC repair actions | `propose_action` actions with `requires_admin=true` trigger UAC after user confirmation; SFC/DISM/driver/service/registry repair examples are built into the prompts |
| Local Qwen context officer | `Qwen3-1.7B-Q8_0.gguf` + llama.cpp CPU runtime; organizes context, completeness, historical case records and recent messages into JSON for the main model; the main model must re-confirm the user's goal |
| Case records and log Markdown | Each run is translated to Markdown by the local Qwen into `Log/runs/` and archived to `Log/病历/`; append-only |
| Execution permissions | `trusted` by default: ordinary read-only tools and low-risk actions pass; medium/high-risk actions require user confirmation and admin actions use UAC; `sandbox_control` run still forces confirmation under `trusted`; path privacy boundaries are no longer used to block itself |
| Model | `deepseek-flash` with thinking enabled and `reasoning_effort=max` |

> **Security boundary**: Read-only tools never modify the system; real changes can only happen through `propose_action` → per-item user confirmation → Action Executor. No action is "executed automatically by the AI". **Actions may declare `backup_paths`; the executor backs them up before running the command and aborts if the backup fails; restore everything with "Settings → Restore action file backups…". System restore points are not created automatically, and no automatic rollback happens after a failure.**
>
> **Limitations**: In the Windows 11 compact context menu you still need "Show more options" / `Shift+F10`. A per-device/per-printer Shell menu requires a COM Shell Extension; instead this project opens a built-in object picker from the "This PC / Printers" folder menu, covering all PnP devices and printers, and also registers experimental device/print-queue class menus.

## 2. Quick Start

Environment: Windows 11/10 x64, Python 3.11+ (3.12+ recommended; the development environment uses 3.14), PowerShell 7 optional.

```powershell
cd "<project-dir>"
.\setup.ps1
```

`setup.ps1` only creates `.venv` and installs the Python dependencies. The local Qwen needs an extra model and runtime (optional):

- Model: `models/Qwen3-1.7B-Q8_0.gguf`, see [models/README.md](models/README.md)
- Runtime: `runtime/llama-b9200/llama-server.exe`, see [runtime/README.md](runtime/README.md)

Once placed, launch:

```powershell
.\run.bat
```

First use:

1. Open **Settings**;
2. Enter the DeepSeek API key and click **Save** (in-app storage uses Windows DPAPI and is decryptable only by the current user);
3. Choose a target (file/folder/device/printer; leave empty for the local system) and describe the problem;
4. Click **Start analysis**;
5. Review the AI plan on the "Execution Plan" page; answer if the AI asks a question; if repair actions are proposed, check each command and confirm;
6. When finished, read the "Diagnosis Conclusion"; use **File → Export report** to save HTML/JSON;
7. To undo file modifications made by actions, use **Settings → Restore action file backups…** and pick the pre-action backup (only covers actions that declared `backup_paths`).

API key precedence: **current session → app DPAPI storage → environment variable `DEEPSEEK_API_KEY`**. Even if an old key exists in the environment, a new key entered and saved in Settings takes effect immediately.

## 2.1 UI Language (Chinese / English)

- Entry point: **Settings → Interface → UI language**, choose "中文" or "English"; the current window refreshes immediately after saving, no restart needed.
- Persistence: written to the config file as `ui_language` (`zh`/`en`) and applied on next launch; invalid values fall back to `zh`.
- Scope: menus, buttons, labels, table headers, settings items, confirmation dialogs, status/progress hints and other UI strings.
- Not translated: AI-generated diagnostic text, raw tool output, logs and already-generated reports, so evidence text is never altered.
- Switching back to Chinese restores known UI strings; dynamic data such as user paths and device names is left untouched.
- Implementation: `helper_plus/i18n.py` and the `ui_language` config field.

## 2.2 Local Model and Runtime (Optional)

The local Qwen is used for context organization, completeness analysis, case-record/log translation and archiving. It does not take part in the final diagnostic conclusion. The repository does not distribute these large files:

- `models/Qwen3-1.7B-Q8_0.gguf` (~1.83 GB): Qwen3-1.7B Q8_0 quantized model;
- `runtime/llama-b9200/llama-server.exe` plus the DLLs in the same directory: llama.cpp Windows CPU x64 build.

Download and verification instructions are in [models/README.md](models/README.md) and [runtime/README.md](runtime/README.md). When both are missing, the main diagnosis still works; local context features report themselves as unavailable instead of fabricating results.

Optional: install the local Qwen autostart:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_local_qwen_autostart.ps1
```

## 3. Typical Workflow

```text
Select target
  ↓
Local parallel baseline collection (system, events, reliability, WER, updates, files/directories, driver mapping, SYSTEM privilege check)
  ↓
Planner round 1: action=plan → displayed on the "Execution Plan" page
  ↓
Loop (no step limit):
  call_tool / call_tools (read-only evidence)
  ask_user (insufficient evidence / need on-site information)
  web_search / web_fetch (targeted lookups of official material)
  propose_action → user confirmation → cmd/powershell execution → action_execute evidence
  ↳ insufficient evidence returns to sampling; tool/action failure leads to root-cause analysis, rollback or another path
  ↓
final: facts with E# references, candidate causes, next steps, action records and timeline
```

## 4. Target Types and Tool Selection

| Target | Baseline / typical tools |
| --- | --- |
| File | `inspect_file`, `map_related_files`, signature/version/hash, event logs, reliability, WER |
| Driver files `.sys/.inf/.cat` | Additionally `get_driver_info`, `enum_drivers_pnputil`, `verify_signature_signtool`, `get_setupapi_log` |
| Folder | `inspect_directory` (full path mapping with `recursive=true` when needed), logs, updates, in-project file mapping |
| Drive | `inspect_directory` (no recursion at the root by default, to avoid hangs), system/update/driver tools |
| System | System information, System/Application logs, reliability, WER, updates, SFC/DISM verification |
| Blue screen/crash/hang | `analyze_dump` (auto-discovery or a specified `.dmp`, offline `!analyze -v`) + event logs, driver version/signature, update history cross-checks |
| PnP device | `get_device_info` + driver/SetupAPI/event logs + `enum_drivers_pnputil` |
| Printer | `get_printer_info` + driver/event logs + device tools |
| Network device/NIC | `get_network_info` + `get_device_info` + driver/event logs |

The AI can also call several tools in one batch according to its plan, or propose arbitrary read-only commands (through controlled actions).

### Brand/Model Heuristics and Physical Information Completion

The baseline collects file version resources, signing subject, driver provider, PnP device vendor/model, printer driver and network adapter descriptions as clues. The AI uses them to infer which brand/product line the target may belong to:

- If the brand/model is reliable, the brand + model + error code/Event ID are used directly as a `web_search` query, preferring vendor sites and official support pages, followed by `web_fetch` on official pages;
- If the brand/model is uncertain, has multiple candidates, official information is missing, or physical-layer information is needed (cable/port/LED/device label/router/switch/printer panel/firmware version), the AI uses `ask_user` to ask the user;
- User answers enter the subsequent `dialogue` and diagnostic log, and the AI keeps verifying; the same mechanism works at any analysis stage, not just at the beginning.

### Software Names, Internal Project Architecture and Missing Items

- Software/product names (for example ProductName/FileDescription "DingTalk"): search official help/downloads/known issues by software name + version + error message, instead of searching the brand only.
- Projects without clear naming: `inspect_project_manifests` reads package.json, requirements, pyproject, csproj/sln, go.mod, Cargo.toml, pom.xml, CMakeLists, README, etc., and reports project type, target framework, declared dependencies, local references, imported-but-not-installed Python modules, and declared-but-not-installed dependencies for Python/Node/.NET, so the AI can identify what is missing.
- Missing Windows core DLLs/components: verify read-only with SFC/DISM first; repair actions prefer the official SFC/DISM methods.
- Missing third-party DLLs/runtimes/extensions: confirm the official product and version from version resources/install directory/vendor site first, then propose a UAC action that downloads the official installer, verifies signature/hash, backs up and installs. **The app never downloads or replaces system files automatically**; every download/install/replacement must be confirmed by the user.
- Physical information that cannot be confirmed locally (device label/model/serial number, hardware ports, network topology): the AI asks via `ask_user`.

## 5. The 24 Registered Tools

`get_system_info`, `inspect_file`, `inspect_directory`, `inspect_project_manifests`, `get_event_logs`, `get_reliability_records`, `get_driver_info`, `map_related_files`, `analyze_dump`, `get_wer_reports`, `get_setupapi_log`, `get_windows_updates`, `get_device_info`, `get_printer_info`, `get_network_info`, `extended_diagnostics`, `sandbox_control`, `run_sfc_verify`, `run_dism_scan`, `enum_drivers_pnputil`, `verify_signature_signtool`, `wevtutil_query`, `web_search`, `web_fetch`.

### 5.1 Advanced Ten (`extended_diagnostics`)

`print_queue` (real print queues/PrintService events), `pnp_tree` (PnP/USB/network/print device tree), `pe_dependencies` (PE imports/architecture/version/missing DLLs), `cbs_logs` (CBS/DISM errors), `crash_dumps` (WER dumps/Driver Verifier), `file_locks` (which processes have a file loaded), `restore_points` (restore points/VSS read-only), `performance` (CPU/memory/disk/queues), `network_test` (NIC/DNS/gateway/connectivity), `sandbox_plan` (policy and time estimate only; does not copy the target or launch anything).

All are read-only; modifications/snapshots still require `propose_action` + user confirmation/UAC; sandbox launch goes through `sandbox_control(mode=run)` and forces confirmation; `sandbox_plan` must first confirm timing and virtualization environment via `ask_user`.

### 5.2 Additional Diagnostic Fields

- `pe_dependencies`: returns the target's `machine`/`pe32_plus`/`file_version`/`product_version`; each resolved DLL carries `dll_checks` (architecture, PE32+, version, `compatible`); anomalies are split into `missing_dlls`, `incompatible_dlls` (architecture mismatch) and `version_info_missing`. 32-bit targets resolve from `SysWOW64` first, and `api-ms-win-*`/`ext-ms-*` are never reported as missing.
- `inspect_project_manifests`: returns `missing_python_modules` (imported but not installed), `missing_local_references`, `missing_declared_dependencies` (declared for Python/Node/.NET but not installed locally) and `declared_dependency_checked/scope`; Python is judged against the current interpreter, and packaged environments are noted in scope.

### 5.3 Crash Dump Analysis (`analyze_dump`)

- **Dependency**: **Debugging Tools for Windows** from the Windows SDK (`cdb.exe`). When it is not installed the tool does not fabricate results; it returns the candidate dump list and installation guidance. You can also point `HELPER_PLUS_CDB` at a `cdb.exe` path or add it to `PATH`.
- **Discovery scope**: a specified `path` → the current target (if it is itself a `.dmp`) → automatic scan of `C:\Windows\MEMORY.DMP`, `C:\Windows\Minidump`, `C:\Windows\LiveKernelReports`, WER `ReportArchive/ReportQueue`, `%LOCALAPPDATA%\CrashDumps` and the target's directory; sorted by modification time descending, at most 20 candidates, never a whole-disk walk.
- **Execution**: `cdb.exe -z <dump> -y <symbol path> -c "!analyze -v; q"`, an offline read-only analysis; it never attaches to a running kernel and never modifies the system.
- **Symbols**: uses the `Log/symbols` local cache plus the Microsoft public symbol server (`https://msdl.microsoft.com/download/symbols`) by default; pass `symbol_path` to override, or `download_symbols=false` to use the local cache only. If symbol download is slow, raise `timeout_seconds` (default 120 s).
- **Returned fields**: `dump_type` (kernel/user/unknown), `bugcheck_code`/`bugcheck_name`/`bugcheck_arguments`, `exception_code`, `probably_caused_by`, `module_name`/`image_name`, `stack_text` (40 frames by default, adjustable), `failure_bucket_id`/`failure_id_hash`, `os_version`, `whea_related`, and `raw_excerpt` with the raw output.
- **Privacy**: analysis happens locally; dumps are never uploaded. Symbol files are requested from Microsoft only when symbol download is enabled.
- **Limitations**: no live kernel debugging/breakpoints/driver-load tracing; when the dump type and `cdb` architecture mismatch, or symbols are missing, the BugCheck may not be identified — the failure reason and raw output are returned so the AI can switch to event logs/reliability records.

All tools are registered in `helper_plus/tools/__init__.py`; tests verify the count, read-only risk, schemas and rejection of unknown parameters.

- `run_sfc_verify` and `run_dism_scan` are read-only verification/scan commands that require administrator rights; the UI asks for confirmation before running them. If the current process lacks privileges they fail explicitly, and the AI can propose a UAC repair action instead.
- `web_search`/`web_fetch` use `https://api.deepseek.com/v1/tools/search|fetch` with the same API key; web content is treated as untrusted external material.
- All tool results enter the unified `E#` evidence store; the audit log never records API keys.

## 6. Context Menu Coverage

After launch the app checks and binds its context menu entries in the background: if any of the 11 entries is missing, or its command still points at an old directory, it re-registers them; when everything is correct nothing is written. After moving the project directory, the next launch repairs it automatically. Registration writes to HKCU only and needs no administrator rights.

Manual installation:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_context_menu.ps1
```

Objects where the menu has been verified to appear:

- Any file (`*`) and `AllFilesystemObjects`;
- Folders (`Directory` / `Folder`);
- Drives (`Drive`);
- Folder background (`Directory\Background`);
- Desktop background (`DesktopBackground`);
- This PC (`{20D04FE0-3AEA-1069-A2D8-08002B30309D}`);
- Printers folder (`{2227A280-3AEA-1069-A2DE-08002B30309D}`);
- Print queues and PnP device objects (experimental).

Right-clicking a file/folder/drive passes the path and starts AI analysis automatically; the This PC / Printers menus open the device/printer picker. Uninstall:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall_context_menu.ps1
```

Note: the app repairs the context menu on every launch, so a manual uninstall is undone by the next launch; for permanent removal also remove the auto-registration call at startup.

Note: the Windows 11 compact context menu shows only common items by default — click "Show more options" or press `Shift+F10`. A standalone menu on individual device/printer icons is limited by Shell namespace extensions, so the object picker is used instead (see the limitation note above).

## 7. SYSTEM Privilege Read-Only Broker (Optional)

A normal user can already complete most forensics. If ACL/owner information on a target file requires SYSTEM to read, install the standalone read-only broker:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_system_broker.ps1
```

- The UI/Planner always runs as a normal user;
- The SYSTEM scheduled task `WindowsHelperPlusSystemBroker` accepts only the fixed read-only requests `ping` / `inspect_target` (SHA-256, signature, version, ACL, download mark);
- When online, one `system_broker_inspect` evidence item is appended automatically; when offline it is skipped silently;
- The GUI/Planner is never elevated to SYSTEM, and arbitrary commands are never handed to SYSTEM.

## 7.1 Local Qwen Context Compression

- Model: `models\Qwen3-1.7B-Q8_0.gguf` (**already copied into the WHP project**, ~1.71 GB);
- Runtime: `runtime\llama-b9200\llama-server.exe` (**already copied into the project**), CPU inference, default port `8124`, context `8192`;
- The app starts `llama-server` automatically the first time context is needed; logs are in `Log/local-model/llama-server.log`;
- The local model only does: context summarization, completed/not-completed items, related historical case records, recent-message selection and log Markdown translation; it **never makes the final diagnosis, never chooses tools, never executes actions**;
- After receiving the `local_context` JSON, the main model must re-confirm the user's goal (`plan.confirmed_user_goal` is required) and independently cite evidence to produce `final`;
- Each refresh takes about 40–70 s (CPU); the old app-level "at most 3 per session" cap is gone; the GUI runs it **asynchronously in the background** with only one Qwen refresh at a time, never blocking the main Planner;
- Log translation runs **asynchronously in the background** after the run ends, taking about 1–3 minutes, without blocking the conclusion display;
- Autostart (current-user Startup, no admin needed):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_local_qwen_autostart.ps1
```

At sign-in it starts/prewarms the local Qwen, checks health, lists logs still missing Markdown, and writes `Log/local-model/startup-status.json`; it never runs diagnosis/repair automatically. Remove with `scripts\uninstall_local_qwen_autostart.ps1`.

- You can disable the local model in Settings, or switch to strict permission mode (strict requires confirmation for every action; read-only path tools still pass under the highest permission mode).

## 7.2 Sandbox as the Last Resort

- `helper_plus/sandbox.py` detects Windows Sandbox, Hyper-V, VirtualBox, VMware and WSL;
- `sandbox_control(mode=plan)` returns policy only: it does not copy the target and does not write `Log`; `extended_diagnostics(mode=sandbox_plan)` is equally side-effect free;
- Only `sandbox_control(mode=run)` performs: copy the target to `Log/sandbox/<time>/staging` (skipping `.git`/`node_modules`/`__pycache__`/`.venv` etc., refusing anything over 2 GB or 20,000 files) → generate the `.wsb`/PowerShell launcher → start Windows Sandbox → actually run the executable target inside the sandbox (`exe/com/bat/cmd/ps1/msi`, 60 s timeout by default, disable with `execute_target=false`) → wait for `sandbox-result.json` → verify host file hashes → close this sandbox → clean up staging;
- The working directory `C:\WHP\Work` is a writable mapping; the host target's original directory is **not** mapped, so the sandbox can only modify the copy, never the host originals;
- The launcher uses a UTF-8 PowerShell script and supports non-ASCII paths such as Chinese/Japanese; `sandbox_control` forces a confirmation dialog even in trusted mode;
- At the end it closes only the `WindowsSandboxClient.exe` instances that appeared during this run, never other sandbox windows the user already had open;
- The collection script returns the target hash/signature, system baseline, process/service counts and the target's run result (exit code, duration, timeout, stdout/stderr tail);
- When Hyper-V/VMware/VirtualBox is available it only offers snapshot/VM/copy/collection plans, requiring an existing VM and a clean snapshot; it never executes them automatically;
- Enable Windows Sandbox (needs UAC + reboot; the script does not reboot automatically): `scripts\enable_windows_sandbox.ps1`; rollback: `scripts\disable_windows_sandbox.ps1`;
- Running the target inside the sandbox is still limited by permissions, GUI interaction and installer reboots; driver/kernel problems and real printer/USB/PCI physical devices cannot be fully covered by Windows Sandbox and need Hyper-V/VM or manual verification.

## 7.3 Pre-Action Backup and One-Click Restore

- When the AI proposes an action that modifies/deletes/replaces files or directories, it should provide `backup_paths` in `propose_action.actions[*]` (at most 8 items, absolute paths); the executor copies them to `Log/actions/<time>-<random>/backup/` before running the command (packaged builds use `%LOCALAPPDATA%\WindowsHelperPlus\Log\actions`) and writes `backup-manifest.json`;
- A failed backup (permissions, full disk, directory over 1 GB/20,000 files, etc.) aborts the action outright, and the command is not executed;
- Paths that did not exist before the action are recorded as `missing`; restoring deletes the paths that action created;
- Restore entry point: **Settings → Restore action file backups…**, pick a backup and confirm twice; directory restore deletes the existing directory before copying the backup back;
- `rollback_command`/`rollback_shell` are still stored in the audit as executable rollback commands for reference by the AI/user; the app never runs rollbacks automatically after a failed action;
- System restore points are never created automatically; for high-risk changes involving drivers, the registry or services, users should still create a restore point/full backup first.

## 8. Security Design

1. **Tool allowlist**: the model can only call registered tools, arguments are validated against JSON Schema, and unknown fields are rejected.
2. **Evidence closure**: only `final`/cause reasoning must cite real `E#` IDs; tool calls, ask_user, propose_action and plan do not force basis/narration, so the model can freely call read-only verification tools. `final`'s summary and every finding/cause/next_step must carry an `E#`; non-existent IDs are rejected with corrective feedback.
3. **Repair actions must be confirmed**: `propose_action` only produces a proposal; the UI shows the full command, risk, admin requirement, rationale and rollback suggestion, and nothing runs until the user confirms. Actions declaring `backup_paths` are backed up first, and the action aborts if the backup fails.
4. **High-risk double confirmation**: actions with `risk=high` require typing `RUN`.
5. **UAC**: `requires_admin` actions run through a one-shot elevated PowerShell runner, with output/exit code written back as evidence; a cancelled UAC is recorded as a failure (after cancel or timeout the elevated process may still be running, and the app does not terminate it automatically).
6. **Sandbox policy**: the sandbox is the last-resort layer (Windows Sandbox/Hyper-V/VM) for cases static tools cannot prove or that need isolated reproduction; `sandbox_control` run forces confirmation even in trusted mode, plan is side-effect free and never auto-executes.
7. **Self-check**: detects Windows 10/11 (including build-number mapping, to avoid the false reading where the Windows 11 registry still reports Windows 10), 64-bit, Python/Qt, local Qwen, API key, Log directory and sandbox capability. Entry: Help → System self-check…, or "System self-check…" in the settings window.
8. **Untrusted input**: file content, event text, device names and web content are all treated as data and must never alter the system prompt.
9. **Transparent data sending**: the first analysis confirms that evidence will be sent to DeepSeek; full file content/dumps are never uploaded.
10. **Local first**: reports and records are stored only on the local machine in `%LOCALAPPDATA%\WindowsHelperPlus` by default, with no telemetry.
11. **An uploaded file is not necessarily the faulty file**: `inspect_file` returns content type/text preview/role hints; the model must first decide whether it is a problem description, log, screenshot, configuration, or the executable/driver to be diagnosed, and must not assume it is already broken.
12. **Follow-up questions always include "Other"**: the `ask_user` dialog automatically appends "Other (please specify)" with free text, so users can add cases that were not listed.
13. **Tool clearance in trust mode**: all registered tools are read-only in risk, so ordinary read-only tools need no per-call confirmation in trusted mode; but `sandbox_control` run copies the target, starts a sandbox and runs the target, so it must be confirmed even when marked trusted.
14. **Server truncation continuation**: when a single reply returns `finish_reason=length`, the JSON is continued automatically; an empty `narration` is no longer a failure condition.

## 9. Project Structure

```text
.github/workflows/      # Windows CI: install dependencies and run pytest
docs/                   # implementation notes
models/README.md        # where to place the local model (.gguf is not tracked by Git)
runtime/README.md       # where to place the llama.cpp runtime (binaries are not tracked by Git)
helper_plus/
  targets.py            # DiagnosisTarget: file/directory/drive/system/device/printer
  safety.py             # path boundaries, target validation
  core/
    orchestrator.py     # Agent–Planner–Tools main loop (no step limit, plans, interaction, actions)
    baseline.py         # parallel baseline collection plan (extended per target type)
    validation.py       # plan/call_tool(s)/ask_user/propose_action/final protocol
    context.py          # context compression, diagnostic log, timeline
    actions.py          # cmd/powershell executor after user confirmation (UAC, pre-action backup/restore)
  llm/                  # DeepSeek SSE client, prompts, narration
  i18n.py               # Chinese/English UI strings and widget-tree translation (never AI text/logs)
  tools/                # 24 read-only tools (including analyze_dump)
  ui/                   # main window, execution plan page, interaction/action dialogs, settings, worker
  system_broker_client.py
  reporting.py
scripts/                # context menu, SYSTEM broker, tool/UI smoke scripts
tests/                  # 105 pytest tests
```

## 10. Development and Testing

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\smoke_tools.py
.\.venv\Scripts\python.exe scripts\ui_ai_mock_smoke.py   # full UI chain against a local fake SSE (no real API)
```

Development automated tests do not use a real key, therefore:

- Planner/SSE/tool loop/plan/action/ask_user are tested with a local mock server and a fake Planner;
- `web_search` HTTP requests are tested against a local fake Search API;
- `Search/Fetch` has not yet hit an official page in that record, so live retrieval still needs acceptance testing with real usage.

## 10.1 Log Export

App logs live in `%LOCALAPPDATA%\WindowsHelperPlus\logs` (JSONL audit) and `%LOCALAPPDATA%\WindowsHelperPlus\reports` (full JSON records) by default. Export them to the project's `Log/` with one command:

```powershell
.\.venv\Scripts\python.exe scripts\collect_logs.py
```

Exported content:

- `Log/audit/`: raw audit logs;
- `Log/reports/`: full diagnostic records;
- `Log/latest/`: audit and records of the most recent run;
- `Log/latest-summary.md`: human-readable summary of the most recent run;
- `Log/README.md`: index and privacy notice;
- `Log/runs/`: Markdown for each run translated by the local Qwen;
- `Log/病历/`: complete case records (Markdown + JSON + index.jsonl), append-only;
- `Log/local-model/`: local llama-server runtime logs;
- `Log/actions/`: pre-action file backups, `backup-manifest.json` and restore records (packaged builds use `%LOCALAPPDATA%\WindowsHelperPlus\Log\actions`; may contain copies of target files — check before sharing);
- `Log/symbols/`: local cache of debugging symbols used by `analyze_dump` (can be large, delete as needed; never exported automatically).

The script only copies/generates files; it never modifies or deletes the original logs, and never exports `credentials.bin`/API keys.



## 11. License and Disclaimer

The code is released under the **MIT License**, see [LICENSE](LICENSE). The local Qwen model follows the upstream Qwen license (Apache-2.0), and llama.cpp and its binaries follow their respective upstream licenses; this repository does not distribute the model or runtime binaries.

This project is for learning and troubleshooting assistance and is provided "as is". AI-generated candidate causes and commands may be inaccurate and cannot replace Microsoft official support, professional repair or hardware diagnostics. Any system modification should be preceded by the user confirming the risk, backing up data and creating a restore point; the consequences of using this tool are borne by the user.

## 12. Contributing

Issues and pull requests are welcome. Before submitting, run:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Please do not commit `.venv/`, `models/*.gguf`, `runtime/` binaries, `Log/`, API keys, certificate private keys or local diagnostic records.
