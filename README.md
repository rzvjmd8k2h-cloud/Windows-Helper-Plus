# Windows Helper Plus Beta 0.2

[English](README_EN.md) | 简体中文

**AI 驱动的 Windows 全对象诊断、交互式问题定位与受控修复。**

![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D4)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB)
![UI](https://img.shields.io/badge/UI-PySide6%206.12-41CD52)
![License](https://img.shields.io/badge/license-MIT-green)

以《架构设计.md》为基础，实现 **Agent（原生 Qt 界面）– Planner（DeepSeek 最新推理模型）– Tools（Windows 原生取证）– Action Executor（用户确认后的修复动作）** 的完整闭环。所有分析结论由 AI 生成并绑定证据 `E#`；本地只负责采集证据、执行工具与受控命令，不再生成本地诊断结论。

> 本仓库只包含源码、脚本与测试。本地 Qwen 模型和 llama.cpp 二进制体积较大，不纳入 Git；按 `models/README.md`、`runtime/README.md` 自行放置即可，缺失时主诊断仍可使用。


## 1. v0.2 能力总览

| 能力 | 实现 |
| --- | --- |
| 全对象目标 | 文件、文件夹、驱动器、本机系统、PnP 设备、打印机 |
| 所有分析由 AI 完成 | 本地只做证据采集；没有“本地只读分析/本地报告”入口，缺少 API Key 时直接报错 |
| 计划先行 | 基线取证后的第一轮 Planner 必须输出 `action=plan`，在“执行计划”页展示后才继续 |
| 多轮取证 | 支持单工具 `call_tool`、批量工具 `call_tools`（1–8 个） |
| 交互问题定位 | `ask_user` 弹出原生对话框，支持选项与自由文本；回答进入后续上下文 |
| 受控修复 | `propose_action` 提出 1–5 条命令；UI 逐条展示、复选框确认，高风险需输入 `RUN`；`requires_admin` 动作通过 UAC 执行 |
| 命令执行 | 支持 `cmd` 与 `powershell` 任意命令/脚本，普通执行前用户确认；另提供沙箱最后兜底策略 |
| 修复后验证 | 动作结果作为 `action_execute` 证据回灌 Planner；AI 继续取证验证、回退或给出结论 |
| 无应用级 token/步数限制 | 不发送 `max_tokens`，不设最大轮次；服务端单次输出 `finish_reason=length` 时自动续写 JSON，不把单次输出上限变成一轮失败 |
| 长上下文压缩 | 每轮快照按预算压缩证据 data，但永远保留全部 `E#`、摘要、诊断日志、用户问答与动作历史 |
| 时间记录 | 会话开始/结束、每条证据/步骤时间、每个动作开始/结束/耗时、UI 实时用时 |
| API Key 保存修复 | 应用内保存优先于环境变量；DPAPI 加密；保存后立即验证读取；设置页保存按钮为默认按钮 |
| 界面语言 | 设置内切换中文/English，保存后当前窗口立即刷新；只翻译界面文案，不翻译 AI 原文、工具输出与日志 |
| 右键覆盖 | 文件、所有文件系统对象、文件夹、驱动器、文件夹空白处、桌面、“此电脑”、“打印机”文件夹；This PC/打印机入口打开对象选择器 |
| 工具完整性 | 24 个注册工具，覆盖系统、文件、目录、**项目清单/依赖架构**、日志、可靠性、WER、**崩溃转储**、更新、驱动、设备、打印机、**网络适配器/网络设备**、SFC/DISM、PnPUtil、SignTool、wevtutil、网页检索、**高级十法** |
| 广义文件/驱动 | 基线同时盘点设备、打印机、网络适配器；文件版本资源/签名主体/驱动提供者/设备厂商/项目名作为品牌线索 |
| 品牌/型号启发式 | 能推断品牌/型号时定向搜索官网驱动/已知问题；不确定或物理层信息缺失时 `ask_user` 弹窗补充 |
| UAC 修复动作 | `propose_action` 的 `requires_admin=true` 动作在用户确认后触发 UAC；SFC/DISM/驱动/服务/注册表修复示例已写入提示词 |
| 本地 Qwen 上下文官 | `Qwen3-1.7B-Q8_0.gguf` + llama.cpp CPU 运行时；整理上下文/完成度/历史病历/近期消息，JSON 交给主模型参考；主模型必须二次确认用户目标 |
| 病历与日志 MD | 每次运行由本地 Qwen 翻译为 Markdown 写入 `Log/runs/`，并归档到 `Log/病历/`；只追加不删除 |
| 执行权限 | 默认 trusted：普通只读工具与低风险动作放行；中/高风险动作由用户确认，管理员动作 UAC；`sandbox_control` 的 run 在 trusted 下仍强制确认；不再用路径隐私边界自我拦截 |
| 模型 | `deepseek-flash`，开启 thinking，`reasoning_effort=max` |

> **安全边界**：只读工具不会修改系统；真正的修改只能通过 `propose_action` → 用户逐条确认 → Action Executor。任何动作都不是“AI 自动执行”。**动作可声明 `backup_paths`，执行器会在命令前自动备份，备份失败则中止；可通过“设置 → 还原动作文件备份…”一键还原。不会自动创建系统还原点，也不会在失败后自动回滚。**
>
> **限制说明**：右键菜单在 Windows 11 精简菜单中仍需“显示更多选项”/`Shift+F10`。单个设备/打印机图标上的独立 Shell 菜单需要 COM Shell Extension；本项目改为“此电脑/打印机”文件夹菜单打开内置对象选择器，覆盖所有 PnP 设备与打印机，同时注册了实验性的设备/打印队列类菜单。

## 2. 快速开始

环境：Windows 11/10 x64、Python 3.11+（推荐 3.12+，开发环境 3.14）、PowerShell 7 可选。

```powershell
cd "<project-dir>"
.\setup.ps1
```

`setup.ps1` 只创建 `.venv` 并安装 Python 依赖。本地 Qwen 需要额外放置模型与运行时（可选）：

- 模型：`models/Qwen3-1.7B-Q8_0.gguf`，见 [models/README.md](models/README.md)
- 运行时：`runtime/llama-b9200/llama-server.exe`，见 [runtime/README.md](runtime/README.md)

放置完成后启动：

```powershell
.\run.bat
```

首次使用：

1. 打开 **设置**；
2. 填写 DeepSeek API Key，点击 **保存**（应用内保存使用 Windows DPAPI，仅当前用户可解密）；
3. 选择目标（文件/文件夹/设备/打印机，留空 = 本机系统），填写问题描述；
4. 点击 **开始分析**；
5. 在“执行计划”页查看 AI 计划；如 AI 提问，回答；如提出修复动作，逐条核对命令并确认；
6. 完成后查看“诊断结论”，可用 **文件 → 导出报告** 保存 HTML/JSON；
7. 如需撤销动作对文件的修改，使用 **设置 → 还原动作文件备份…** 选择动作前自动备份（仅覆盖声明了 `backup_paths` 的动作）。

API Key 优先级：**本会话 → 应用 DPAPI 保存 → 环境变量 `DEEPSEEK_API_KEY`**。即使环境中存在旧 Key，在设置里填写并保存的新 Key 也会立即生效。

## 2.1 界面语言（中文 / English）

- 入口：**设置 → 界面 → 界面语言**，可选“中文”或“English”；保存后当前窗口立即刷新，无需重启。
- 持久化：写入配置文件 `ui_language`（`zh`/`en`），下次启动自动生效；无效值回退 `zh`。
- 范围：菜单、按钮、标签、表头、设置项、确认对话框、状态/进度提示等界面文案。
- 不翻译：AI 生成的诊断文本、工具原始输出、日志与已生成报告内容，避免篡改证据原文。
- 切回中文时会反向还原已知界面文案；用户路径/设备名等动态数据保持原样。
- 实现见 `helper_plus/i18n.py` 与配置字段 `ui_language`。

## 2.2 本地模型与运行时（可选）

本地 Qwen 用于上下文整理、完成度分析、病历/日志翻译与归档，不参与最终诊断结论。仓库不分发以下大文件：

- `models/Qwen3-1.7B-Q8_0.gguf`（约 1.83 GB）：Qwen3-1.7B Q8_0 量化模型；
- `runtime/llama-b9200/llama-server.exe` 及同目录 DLL：llama.cpp Windows CPU x64 构建。

下载与校验方式见 [models/README.md](models/README.md) 和 [runtime/README.md](runtime/README.md)。两者缺失时，主诊断仍可正常使用；本地上下文功能会显示不可用，不会伪造结果。

可选：安装本地 Qwen 开机自启动：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_local_qwen_autostart.ps1
```

## 3. 典型工作流

```text
选择目标
  ↓
本地并行基线取证（系统、事件、可靠性、WER、更新、文件/目录、驱动映射、SYSTEM 特权校验）
  ↓
Planner 第一轮：action=plan → 在“执行计划”页展示
  ↓
循环（无步数上限）：
  call_tool / call_tools（只读取证）
  ask_user（证据不足/需要现场信息）
  web_search / web_fetch（定向联网查官方资料）
  propose_action → 用户确认 → cmd/powershell 执行 → action_execute 证据
  ↳ 证据不足回到取样；工具/动作失败则分析原因、回退或换路径
  ↓
final：输出带 E# 引用的事实、候选原因、下一步、动作记录与时间线
```

## 4. 目标类型与工具选择

| 目标 | 基线/典型工具 |
| --- | --- |
| 文件 | `inspect_file`、`map_related_files`、签名/版本/哈希、事件日志、可靠性、WER |
| 驱动文件 `.sys/.inf/.cat` | 额外 `get_driver_info`、`enum_drivers_pnputil`、`verify_signature_signtool`、`get_setupapi_log` |
| 文件夹 | `inspect_directory`（必要时 `recursive=true` 完整路径映射）、日志、更新、项目内文件映射 |
| 驱动器 | `inspect_directory`（根目录不默认递归，避免卡死）、系统/更新/驱动工具 |
| 系统 | 系统信息、System/Application 日志、可靠性、WER、更新、SFC/DISM 验证 |
| 蓝屏/崩溃/挂起 | `analyze_dump`（自动发现或指定 `.dmp`，离线 `!analyze -v`）+ 事件日志、驱动版本/签名、更新历史交叉验证 |
| PnP 设备 | `get_device_info` + 驱动/SetupAPI/事件日志 + `enum_drivers_pnputil` |
| 打印机 | `get_printer_info` + 驱动/事件日志 + 设备工具 |
| 网络设备/网卡 | `get_network_info` + `get_device_info` + 驱动/事件日志 |

AI 还可以按计划批量调用多个工具，或提出任意只读命令（通过受控动作）。

### 品牌/型号启发式与物理信息补全

基线会同时采集文件版本资源、签名主体、驱动提供者、PnP 设备厂商/型号、打印机驱动、网络适配器描述等线索。AI 会据此推断目标可能属于哪个品牌/产品线：

- 若品牌/型号较可靠，直接把品牌 + 型号 + 错误码/Event ID 作为 `web_search` 查询，优先检索官网/官方支持站，再 `web_fetch` 官方页面；
- 若品牌/型号不确定、有多个候选、官网信息缺失，或需要物理层信息（网线/接口/LED/设备标签/路由器/交换机/打印机面板/固件版本），AI 会通过 `ask_user` 弹窗让用户二次补充；
- 用户回答进入后续 `dialogue` 和诊断日志，AI 继续验证；同一机制可用于任何分析阶段，不只限于开始。

### 软件名称、项目内部架构与缺失项

- 软件名/产品名（例如 ProductName/FileDescription 为“钉钉”）：以软件名 + 版本 + 错误信息定向搜索官方帮助/下载/已知问题，而不是只搜品牌。
- 无明确名称/命名混乱的项目：`inspect_project_manifests` 读取 package.json、requirements、pyproject、csproj/sln、go.mod、Cargo.toml、pom.xml、CMakeLists、README 等，给出项目类型、目标框架、声明依赖、本地引用、已导入但未安装的 Python 模块，以及 Python/Node/.NET 中已声明但本机未安装的依赖；AI 据此判断缺失项。
- Windows 核心 DLL/组件缺失：先用 SFC/DISM 只读验证，修复动作优先 SFC/DISM 官方方式。
- 缺失第三方 DLL/运行库/扩展：先从版本资源/安装目录/官网确认官方产品与版本，再提出下载官方安装包并校验签名/哈希、备份、安装的 UAC 动作。**应用不会自动下载或替换系统文件**，所有下载/安装/替换都必须由用户确认。
- 设备标签/型号/序列号、硬件接口、网络拓扑等本机无法确认的物理信息：AI 用 `ask_user` 二次补充。

## 5. 24 个已注册工具

`get_system_info`、`inspect_file`、`inspect_directory`、`inspect_project_manifests`、`get_event_logs`、`get_reliability_records`、`get_driver_info`、`map_related_files`、`analyze_dump`、`get_wer_reports`、`get_setupapi_log`、`get_windows_updates`、`get_device_info`、`get_printer_info`、`get_network_info`、`extended_diagnostics`、`sandbox_control`、`run_sfc_verify`、`run_dism_scan`、`enum_drivers_pnputil`、`verify_signature_signtool`、`wevtutil_query`、`web_search`、`web_fetch`。

### 5.1 高级十法（`extended_diagnostics`）

`print_queue`（真实打印队列/PrintService 事件）、`pnp_tree`（PnP/USB/网络/打印设备树）、`pe_dependencies`（PE 导入/架构/版本/缺失 DLL）、`cbs_logs`（CBS/DISM 错误）、`crash_dumps`（WER dump/Driver Verifier）、`file_locks`（文件被哪些进程加载）、`restore_points`（还原点/VSS 只读）、`performance`（CPU/内存/磁盘/队列）、`network_test`（网卡/DNS/网关/连通性）、`sandbox_plan`（仅策略与时间预估，不复制目标、不启动）。

全部只读；修改/快照仍需 `propose_action` + 用户确认/UAC；沙箱启动走 `sandbox_control(mode=run)` 并强制确认；`sandbox_plan` 必须先 `ask_user` 确认时间与虚拟化环境。

### 5.2 诊断字段补充

- `pe_dependencies`：返回目标 `machine`/`pe32_plus`/`file_version`/`product_version`；每个已解析 DLL 带 `dll_checks`（架构、PE32+、版本、`compatible`）；异常分成 `missing_dlls`、`incompatible_dlls`（架构不匹配）和 `version_info_missing`。32 位目标优先解析 `SysWOW64`，`api-ms-win-*`/`ext-ms-*` 不会被误报为缺失。
- `inspect_project_manifests`：返回 `missing_python_modules`（已导入未安装）、`missing_local_references`、`missing_declared_dependencies`（Python/Node/.NET 已声明但本机未安装）以及 `declared_dependency_checked/scope`；Python 以当前解释器为准，打包环境会在 scope 中标注。

### 5.3 崩溃转储分析（`analyze_dump`）

- **依赖**：Windows SDK 的 **Debugging Tools for Windows**（`cdb.exe`）。未安装时工具不伪造结果，返回候选 dump 列表与安装指引；也可用环境变量 `HELPER_PLUS_CDB` 指定 `cdb.exe` 路径，或自行加入 `PATH`。
- **发现范围**：指定 `path` → 当前目标（本身是 `.dmp`）→ 自动扫描 `C:\Windows\MEMORY.DMP`、`C:\Windows\Minidump`、`C:\Windows\LiveKernelReports`、WER `ReportArchive/ReportQueue`、`%LOCALAPPDATA%\CrashDumps` 和目标所在目录；按修改时间倒序，最多返回 20 个候选，不做全盘遍历。
- **执行方式**：`cdb.exe -z <dump> -y <符号路径> -c "!analyze -v; q"`，只读离线分析；不会 attach 运行中的内核，也不修改系统。
- **符号**：默认使用 `Log/symbols` 本地缓存 + Microsoft 公共符号服务器（`https://msdl.microsoft.com/download/symbols`）；可传 `symbol_path` 自定义，或 `download_symbols=false` 只用本地缓存。符号下载慢时可调大 `timeout_seconds`（默认 120 秒）。
- **返回字段**：`dump_type`（kernel/user/unknown）、`bugcheck_code`/`bugcheck_name`/`bugcheck_arguments`、`exception_code`、`probably_caused_by`、`module_name`/`image_name`、`stack_text`（默认 40 帧，可调）、`failure_bucket_id`/`failure_id_hash`、`os_version`、`whea_related`，以及 `raw_excerpt` 原始输出。
- **隐私**：分析在本机完成；不会上传 dump。仅在启用符号下载时向 Microsoft 请求符号文件。
- **限制**：不提供实时内核调试/断点/驱动加载跟踪；dump 类型与 `cdb` 架构不匹配、符号缺失时可能无法识别 BugCheck，此时会返回失败原因与原始输出，供 AI 改用事件日志/可靠性记录继续排查。

所有工具在 `helper_plus/tools/__init__.py` 注册；测试会校验数量、只读风险、schema 与未知参数拒绝。

- `run_sfc_verify`、`run_dism_scan` 是只读验证/扫描命令，需要管理员权限，执行前 UI 会二次确认；若当前进程权限不足，会返回明确失败，AI 可改为提出 UAC 修复动作。
- `web_search`/`web_fetch` 使用 `https://api.deepseek.com/v1/tools/search|fetch`，同一 API Key；网页内容按不可信外部资料处理。
- 所有工具结果进入统一 `E#` 证据库，审计日志不记录 API Key。

## 6. 右键菜单覆盖

应用启动后会在后台自动检查并绑定右键菜单：11 个入口缺失、或命令仍指向旧目录时自动重新注册；已经正确时不做任何写入。移动项目目录后，下次启动会自动修复。注册只写 HKCU，不需要管理员。

也可以手动安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_context_menu.ps1
```

已验证出现菜单的对象：

- 任意文件（`*`）与 `AllFilesystemObjects`；
- 文件夹（`Directory` / `Folder`）；
- 驱动器（`Drive`）；
- 文件夹空白处（`Directory\Background`）；
- 桌面空白处（`DesktopBackground`）；
- 此电脑（`{20D04FE0-3AEA-1069-A2D8-08002B30309D}`）；
- 打印机文件夹（`{2227A280-3AEA-1069-A2DE-08002B30309D}`）；
- 打印队列与 PnP 设备对象（实验性）。

文件/文件夹/驱动器右键会传入路径并自动开始 AI 分析；此电脑/打印机菜单会打开设备/打印机选择器。卸载：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall_context_menu.ps1
```

注意：应用每次启动都会自动修复右键菜单，手动卸载后再次启动会自动恢复；如需永久移除，请同时移除启动时的自动注册调用。

说明：Windows 11 默认精简右键菜单只显示常用项，需要点击“显示更多选项”或按 `Shift+F10`。单个设备/打印机图标上的独立菜单受 Shell 命名空间扩展限制，改用对象选择器覆盖（见上文限制说明）。

## 7. SYSTEM 特权只读代理（可选）

普通用户已能完成大部分取证。若目标文件的 ACL/所有者等信息需要 SYSTEM 才能读取，可安装独立只读代理：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_system_broker.ps1
```

- UI/Planner 始终以普通用户运行；
- SYSTEM 计划任务 `WindowsHelperPlusSystemBroker` 只接受固定 `ping` / `inspect_target` 只读请求（SHA-256、签名、版本、ACL、下载标记）；
- 在线时自动追加一条 `system_broker_inspect` 证据，离线静默跳过；
- 不把 GUI/Planner 提升为 SYSTEM，也不把任意命令交给 SYSTEM 执行。

## 7.1 本地 Qwen 上下文压缩

- 模型：`models\Qwen3-1.7B-Q8_0.gguf`（**已复制到 WHP 项目内**，约 1.71 GB）；
- 运行时：`runtime\llama-b9200\llama-server.exe`（**已复制到项目内**），CPU 推理，默认端口 `8124`、上下文 `8192`；
- 应用首次需要上下文时自动启动 `llama-server`；日志在 `Log/local-model/llama-server.log`；
- 本地模型只做：上下文摘要、完成/未完成、相关历史病历、近期消息筛选、日志 Markdown 翻译；**不做最终诊断、不决定工具、不执行动作**；
- 主模型收到 `local_context` JSON 后必须二次确认用户目标（`plan.confirmed_user_goal` 必填），并独立引用证据生成 final；
- 每次刷新约 40–70 秒（CPU），不再设置“每会话最多 3 次”的应用级上限；GUI 使用**后台异步**，同一时间只跑一个 Qwen 刷新，不阻塞主 Planner；
- 日志翻译在运行结束后**后台异步**执行，约 1–3 分钟，不阻塞结论展示；
- 开机自启动（当前用户 Startup，无需管理员）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_local_qwen_autostart.ps1
```

登录时会启动/预热带本地 Qwen、检查健康并列出缺少 Markdown 的日志，写入 `Log/local-model/startup-status.json`；不会自动执行诊断/修复。可移除：`scripts\uninstall_local_qwen_autostart.ps1`。

- 可在设置中关闭本地模型，或切换 strict 权限模式（strict 仅让所有动作都需确认；路径只读工具仍按最高权限模式放行）。

## 7.2 沙箱最后兜底

- `helper_plus/sandbox.py` 检测 Windows Sandbox、Hyper-V、VirtualBox、VMware、WSL；
- `sandbox_control(mode=plan)` 只返回策略，不复制目标、不写 `Log`；`extended_diagnostics(mode=sandbox_plan)` 同样无副作用；
- `sandbox_control(mode=run)` 才会：复制目标到 `Log/sandbox/<时间>/staging`（跳过 `.git`/`node_modules`/`__pycache__`/`.venv` 等，超过 2 GB 或 2 万文件直接拒绝）→ 生成 `.wsb`/PowerShell 启动器 → 启动 Windows Sandbox → 沙箱内实际运行可执行目标（`exe/com/bat/cmd/ps1/msi`，默认 60 秒超时，可用 `execute_target=false` 关闭）→ 等待 `sandbox-result.json` → 校验宿主文件哈希 → 关闭本次沙箱 → 清理 staging；
- 工作目录 `C:\WHP\Work` 是可写映射，宿主目标原目录**不映射**；沙箱只能修改副本，不修改宿主原文件；
- 启动器使用 UTF-8 PowerShell 脚本，支持中文/日文等非 ASCII 路径；`sandbox_control` 即使处于 trusted 模式也会强制弹窗确认；
- 运行结束只关闭本次新出现的 `WindowsSandboxClient.exe`，不会误杀用户已打开的其他沙箱窗口；
- 采集脚本返回目标哈希/签名、系统基线、进程/服务数量以及目标运行结果（退出码、耗时、超时、stdout/stderr 尾部）；
- Hyper-V/VMware/VirtualBox 可用时只提供快照/VM/复制/采集方案，要求现成 VM 与干净快照，不会自动执行；
- 启用 Windows Sandbox（需 UAC + 重启，脚本不自动重启）：`scripts\enable_windows_sandbox.ps1`；回滚：`scripts\disable_windows_sandbox.ps1`；
- 沙箱内实际运行目标仍受权限、GUI 交互、安装包重启等限制；驱动/内核问题、真实打印机/USB/PCI 物理设备无法由 Windows Sandbox 完整覆盖，需要 Hyper-V/VM 或人工验证。

## 7.3 动作前备份与一键还原

- AI 提出修改/删除/替换文件或目录的动作时，应在 `propose_action.actions[*]` 中提供 `backup_paths`（最多 8 项，绝对路径）；执行器在运行命令前复制到 `Log/actions/<时间>-<随机>/backup/`（打包后位于 `%LOCALAPPDATA%\WindowsHelperPlus\Log\actions`），并写 `backup-manifest.json`；
- 备份失败（权限、磁盘满、目录超过 1 GB/2 万文件等）会直接中止动作，命令不会执行；
- 动作前不存在的路径会记录为 `missing`；还原时会删除该动作新建的路径；
- 还原入口：**设置 → 还原动作文件备份…**，选择备份后二次确认；目录还原会先删除现有目录再复制回备份；
- `rollback_command`/`rollback_shell` 仍作为可执行回退命令保存在审计中，供 AI/用户参考；应用不会在动作失败后自动执行回滚；
- 不自动创建系统还原点；涉及驱动、注册表、服务等高危修改时，仍建议用户先手动创建还原点/完整备份。

## 8. 安全设计

1. **工具白名单**：模型只能调用注册工具，参数按 JSON Schema 校验并拒绝未知字段。
2. **证据闭环**：只有 final/原因推理必须引用真实 `E#`；工具调用、ask_user、propose_action、plan 不强制 basis/narration，模型可自由调用只读验证工具。final 的 summary 与每个 finding/cause/next_step 必须有 E#；不存在的 ID 会被拒绝并反馈修正。
3. **修复动作必须确认**：`propose_action` 只生成提案；UI 展示完整命令、风险、管理员需求、理由、回滚建议；用户确认后才执行。声明 `backup_paths` 的动作会先自动备份，备份失败则中止。
4. **高风险二次确认**：`risk=high` 的动作需要输入 `RUN`。
5. **UAC**：`requires_admin` 动作通过一次性提权 PowerShell runner 执行，输出/退出码回写证据；UAC 取消会记录失败（取消或超时后，提升进程可能仍在运行，应用不会自动终止它）。
6. **沙箱策略**：沙箱是最后兜底层（Windows Sandbox/Hyper-V/VM），用于静态工具无法证明或需要隔离复现的情况；`sandbox_control` 的 run 即使 trusted 模式也强制确认，plan 无副作用，不自动执行。
7. **系统自检**：检测 Windows 10/11（含 build 号映射，避免 Win11 注册表仍报 Win10 的误判）、64 位、Python/Qt、本地 Qwen、API Key、Log 目录、沙箱能力。入口：帮助 → 系统自检… 或设置窗口“系统自检…”。
8. **不可信输入**：文件内容、事件文本、设备名、网页内容均作为数据，不得改变系统提示。
9. **数据发送透明**：首次分析时确认证据会发送给 DeepSeek；不会上传完整文件内容/转储。
10. **本地优先**：报告、记录默认只保存在本机 `%LOCALAPPDATA%\WindowsHelperPlus`，无遥测。
11. **上传文件不一定是故障文件**：`inspect_file` 会返回内容类型/文本预览/角色提示；模型必须先判断它是问题说明、日志、截图、配置，还是待诊断的可执行/驱动文件，不能默认它已损坏。
12. **追问永远有“其他”**：`ask_user` 弹窗自动追加“其他（请自行补充）”并保留自由文本，用户可补充未列出的情况。
13. **信任模式工具放行**：注册工具风险均为只读，trusted 模式下普通只读工具无需逐次确认；但 `sandbox_control` 的 run 会复制目标、启动沙箱并运行目标，标记为 trusted 模式也必须确认。
14. **服务端输出截断续写**：单次回复 `finish_reason=length` 时自动续写 JSON；`narration` 为空不再作为失败条件。

## 9. 项目结构

```text
.github/workflows/      # Windows CI：安装依赖并运行 pytest
docs/                   # 实现说明
models/README.md        # 本地模型放置说明（.gguf 不纳入 Git）
runtime/README.md       # llama.cpp 运行时放置说明（二进制不纳入 Git）
helper_plus/
  targets.py            # DiagnosisTarget：file/directory/drive/system/device/printer
  safety.py             # 路径边界、目标校验
  core/
    orchestrator.py     # Agent–Planner–Tools 主循环（无步数上限、计划、交互、动作）
    baseline.py         # 并行基线取证计划（按目标类型扩展）
    validation.py       # plan/call_tool(s)/ask_user/propose_action/final 协议
    context.py          # 上下文压缩、诊断日志、时间线
    actions.py          # 用户确认后的 cmd/powershell 执行器（UAC、动作前文件备份/还原）
  llm/                  # DeepSeek SSE 客户端、提示词、narration
  i18n.py               # 中/英文界面文案与控件树翻译（不翻译 AI 原文/日志）
  tools/                # 24 个只读工具（含 analyze_dump）
  ui/                   # 主窗口、执行计划页、交互/动作对话框、设置、worker
  system_broker_client.py
  reporting.py
scripts/                # 右键菜单、SYSTEM 代理、工具/UI 冒烟脚本
tests/                  # 105 个 pytest 测试
```

## 10. 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\smoke_tools.py
.\.venv\Scripts\python.exe scripts\ui_ai_mock_smoke.py   # 本地假 SSE 全链路 UI（不调用真实 API）
```

开发期自动化测试没有使用真实 Key，因此：

- Planner/SSE/工具循环/计划/动作/ask_user 由本地 mock 服务器和假 Planner 测试；
- `web_search` 的 HTTP 请求由本地假 Search API 测试；
- `Search/Fetch` 尚未在该记录中命中官方页面，联网检索仍需真实使用验收。

## 10.1 日志导出

应用日志默认在 `%LOCALAPPDATA%\WindowsHelperPlus\logs`（JSONL 审计）与 `%LOCALAPPDATA%\WindowsHelperPlus\reports`（完整 JSON 记录）。可一键导出到项目根目录 `Log/`：

```powershell
.\.venv\Scripts\python.exe scripts\collect_logs.py
```

导出内容：

- `Log/audit/`：原始审计日志；
- `Log/reports/`：完整诊断记录；
- `Log/latest/`：最近一次运行的审计与记录；
- `Log/latest-summary.md`：最近一次运行的人类可读摘要；
- `Log/README.md`：索引与隐私提示；
- `Log/runs/`：本地 Qwen 翻译的每次运行 Markdown；
- `Log/病历/`：完整病历（Markdown + JSON + index.jsonl），只追加；
- `Log/local-model/`：本地 llama-server 运行日志；
- `Log/actions/`：动作前文件备份、`backup-manifest.json` 与还原记录（打包后位于 `%LOCALAPPDATA%\WindowsHelperPlus\Log\actions`；可能包含目标文件副本，分享前请检查）；
- `Log/symbols/`：`analyze_dump` 使用的调试符号本地缓存（体积可能较大，可按需删除；不会自动导出）。

脚本只复制/生成文件，不修改或删除原始日志，也不导出 `credentials.bin`/API Key。



## 11. 许可证与免责声明

代码以 **MIT License** 发布，见 [LICENSE](LICENSE)。本地 Qwen 模型遵循 Qwen 上游许可证（Apache-2.0），llama.cpp 及其二进制遵循各自上游许可证；本仓库不分发模型和运行时二进制。

本项目用于学习与排障辅助，按“现状”提供。AI 生成的候选原因与命令可能不准确，不能替代微软官方支持、专业维修或硬件检测。任何修改系统的操作都应由用户确认风险、备份数据并创建还原点；使用本工具造成的后果由使用者承担。

## 12. 贡献

欢迎提交 Issue 和 Pull Request。提交前请运行：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

请勿提交 `.venv/`、`models/*.gguf`、`runtime/` 二进制、`Log/`、API Key、证书私钥或本机诊断记录。