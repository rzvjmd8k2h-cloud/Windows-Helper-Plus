# 实现说明（v2）

本文记录 Windows Helper Plus v2 如何落地《架构设计.md》的阶段 1–3，以及关键工程取舍。使用手册见 `README.md`。

## 1. 模块落地

| 架构概念 | 代码 | 说明 |
|---|---|---|
| Agent 用户交互层 | `helper_plus/ui/main_window.py`、`ui/dialogs.py` | 原生 Qt；目标选择、计划页、提问/动作确认、证据/审计/报告 |
| Agent 后台桥 | `helper_plus/ui/workers.py` | QThread + 信号；交互请求阻塞 worker，UI 弹窗后回填回答/确认 |
| 目标抽象 | `helper_plus/targets.py` | `file/directory/drive/system/device/printer`；UI/编排器/工具共用 |
| Planner | `helper_plus/llm/client.py`、`llm/prompts.py` | DeepSeek SSE；JSON Output；thinking/max |
| 协议守护 | `helper_plus/core/validation.py` | plan、call_tool(s)、ask_user、propose_action、final；E# 引用闭环 |
| 编排器 | `helper_plus/core/orchestrator.py` | 基线→计划→无限轮决策→工具/交互/动作→验证→final |
| 基线取证 | `helper_plus/core/baseline.py` | 按目标类型扩展；文件/驱动/目录映射；并行执行 |
| 上下文压缩 | `helper_plus/core/context.py` | 全 E# 摘要 + 最近/焦点证据完整 data + 诊断日志/时间线 |
| 受控动作执行 | `helper_plus/core/actions.py` | cmd/powershell；需用户确认；`requires_admin` 走 UAC；无沙箱 |
| 工具层 | `helper_plus/tools/` | 24 个只读工具（含 `analyze_dump` 离线转储分析）；JSON Schema 校验；统一 `ToolResult` |
| 设备/打印机选择 | `helper_plus/objects.py` | CIM 枚举，供“此电脑/打印机”右键入口与 UI 选择器 |
| 报告/审计 | `helper_plus/reporting.py`、`audit.py`、`models.py` | HTML/JSON；动作、用户问答、计划、时间线入报告/记录 |
| SYSTEM 只读代理 | `system_broker_client.py`、`scripts/system_broker.ps1` | 独立计划任务；固定 ping/inspect_target 只读请求 |

## 2. 编排循环（v2）

1. `run()` 记录开始时间；按目标类型构造 `ToolContext`（含 target_spec、api_key）。
2. 并行基线取证 + SYSTEM 代理校验（仅文件），生成 `E#`。
3. 要求 DeepSeek **第一轮必须 `action=plan`**；计划通过 UI `on_plan` 显示到“执行计划”页。
4. 之后循环（应用层无步数上限）：
   - `call_tool` / `call_tools`：工具白名单校验 →（必要时用户确认）→ 执行 → 新建 `E#`；
   - `ask_user`：UI 弹窗，回答写入 `interactions` 与下一轮 `dialogue`；
   - `web_search` / `web_fetch`：DeepSeek Search/Fetch，结果作为证据，网页按不可信资料处理；
   - `propose_action`：UI 逐条展示命令/风险/管理员/回滚；确认后由 `actions.execute_action` 执行，结果成为 `action_execute` 证据；未确认只记录拒绝；
   - `final`：保存计划、证据、工具步骤、交互、动作、usage、时间；完成。
5. 工具失败/动作失败：结果回灌；AI 可换工具、改参数、回退假设、询问用户或提出回滚。
6. 取消/错误：保留已采集证据与审计；**不再生成任何本地分析报告**（状态 error/cancelled）。

### 2.1 广义驱动/设备覆盖与品牌启发式

- 基线除系统/日志/WER/更新外，统一采集 PnP 设备清单、打印机清单、网络适配器与 IP 配置；文件目标额外采集版本资源、签名主体、驱动映射、INF/PnPUtil、SetupAPI。
- Planner 的第一层推理允许从 CompanyName/ProductName/FileDescription、驱动提供者、设备厂商、打印机驱动、网卡描述、目录/项目名推断品牌与型号；软件产品名（如“钉钉”）同样作为官方检索关键词。
- `inspect_project_manifests` 对项目目录读取 package.json/requirements/pyproject/csproj/sln/go.mod/Cargo.toml/pom.xml/CMakeLists/README 等，返回软件名/描述/版本、项目类型、目标框架、声明依赖、本地引用缺失和 Python 未安装 import，作为“内部架构/缺什么”的客观证据。
- 若品牌/型号可靠，`web_search` 查询必须包含品牌+型号+错误码/系统版本，并优先官网/官方支持页；命中后 `web_fetch`。
- 若品牌/型号不确定、缺型号/序列号、需要物理层信息（接口/LED/面板/网络拓扑/固件），调用 `ask_user`。用户回答进入 `dialogue`，并作为后续推理依据；该机制适用于任意阶段。
- UAC 修复动作：需要管理员权限的修复只能通过 `propose_action` + `requires_admin=true` 提出；UI 确认后触发 UAC；执行结果回灌为证据。

## 3. Planner 协议

根对象：

```json
{"narration": "...", "action": "plan|call_tool|call_tools|ask_user|propose_action|final", "basis": ["Q1", "E1"]}
```

- `plan`：目标 + 步骤 + 计划工具 + 备注；首轮强制。
- `call_tool`：tool + arguments + why。
- `call_tools`：1–8 个 `{tool, arguments}`。
- `ask_user`：question `{prompt, options, allow_free_text}`。
- `propose_action`：1–5 个 `{title, command, shell, risk, requires_admin, rationale, rollback}`。
- `final`：headline/summary/findings/candidate_causes/next_steps/limitations；每条结论带 E#。

校验器拒绝：未知动作/工具、参数 schema 失败、重复相同工具调用、无效 basis、空证据结论、伪造 E#。连续两次协议失败则会话 `error`。

### 3.1 本地 Qwen 上下文官（v2.1）

- `helper_plus/local_model.py`：发现/启动项目内 `runtime\llama-b9200\llama-server.exe`，加载 `models\Qwen3-1.7B-Q8_0.gguf`（CPU，端口 8124，上下文 8192）。模型与运行时已复制到 WHP 项目内，便于整体打包。
- `helper_plus/local_context.py`：
  - 每轮按需（首轮、用户回答、动作后；不再设每会话最多 3 次的应用级上限）生成 JSON：用户目标候选、状态、completed/unfinished/blocked、open_questions、相关历史病历、近期消息保留/丢弃、上下文摘要、风险、建议关注点；
  - 主模型只有在 `plan.confirmed_user_goal` 中二次确认目标后才能继续；本地 JSON 仅参考；
  - 每次运行后把记录翻译成 Markdown 写入 `Log/runs/`，并归档到 `Log/病历/`（MD + JSON + index.jsonl），只追加不删除。
- 本地模型在 CPU 上较慢：每次上下文约 40–70 秒，日志翻译约 1–3 分钟；均为可降级路径，失败不会伪造结论。
- 主模型提示词新增：不得照抄本地候选目标、必须审查 prior_cases、以当前 E# 为准、用户目标/纠正/未完成不可丢。
- **异步**：GUI 使用 `local_async=True`，本地上下文在后台线程生成，绝不阻塞主 Planner；日志翻译在运行结束后后台执行。启动服务与登录预热由 `scripts/local_qwen_service.py` + Startup 快捷方式负责，可提前加载模型并检查待翻译日志。
- 启动自启：`scripts/install_local_qwen_autostart.ps1` 创建当前用户 Startup 快捷方式；不执行诊断/修复，只启动/预热并写 `Log/local-model/startup-status.json`。

### 3.2 高级十法（`extended_diagnostics`）

- `print_queue`：Win32_PrintJob + PrintService/Operational；
- `pnp_tree`：PnP/USB/网络/打印设备状态与错误码；
- `pe_dependencies`：PE 导入表、API Set 契约、缺失 DLL；
- `cbs_logs`：CBS.log/dism.log 错误/损坏/修复行；
- `crash_dumps`：WER dump/Report.wer + Driver Verifier；
- `file_locks`：哪些进程已加载文件；
- `restore_points`：还原点/VSS 只读；
- `performance`：CPU/内存/磁盘/队列快照；
- `network_test`：接口/DNS/网关/Ping；
- `sandbox_plan`：20–60 分钟 VM 方案，只返回计划，必须先 ask_user；不执行。

## 4. 上下文与长对话

- 每轮是无状态快照，但由 `context.py` 压缩：
  - 所有证据保留 ID、工具、摘要、错误、时间；
  - 最近 12 条、最近步骤引用和 focus 优先保留完整 data；
  - 超出字符预算的旧证据 `data=null, compressed=true`；
  - 附带诊断日志（工具/问答/动作）、执行步骤、动作历史、计划、模型使用量。
- 应用层不发送 `max_tokens`、不限制轮次；服务端单次输出 `finish_reason=length` 时自动续写 JSON；上下文预算可在设置中调整（默认 700k 字符）。
- 时间：会话开始/结束、`elapsed_seconds`、每条证据/步骤/动作的 ISO 时间与耗时，均写入记录和报告。

## 5. 安全设计

1. 工具只读白名单 + 未知字段拒绝；
2. 文件路径边界校验（目标/Windows 目录/目录子树）；
3. `ask_user` 只是交互，不是执行；
4. `propose_action` 必须用户确认；高风险输入 `RUN`；UI 展示完整命令；
5. Action Executor 支持当前用户或 UAC 一次性提升；输出/退出码/时间回写；
6. 未确认动作绝不执行；拒绝动作写入 action_history，让模型看到；
7. 外部数据（网页/文件/事件/设备名）不改变系统提示；
8. API Key 使用 DPAPI；应用内保存优先于环境变量；
9. SYSTEM 代理只读，不接收任意命令；动作执行器不把命令发送给 SYSTEM 代理。

**已知未实现**：自动回滚、Shell Extension 级单设备/单打印机菜单、内存转储/内核调试。

## 6. 测试与开发

- `tests/`：79 passed（开发环境），覆盖配置、工具注册（22）、路径边界、协议、上下文压缩、动作/UAC 分支、项目清单/依赖分析、系统兼容/自检/沙箱策略、计划/问答/动作闭环、SSE、报告、SYSTEM 代理、API Key、UI 入口导入等。
- `scripts/ui_ai_mock_smoke.py`：本地假 SSE 跑通计划/最终 UI，输出截图。
- `scripts/smoke_tools.py`：真实 Windows 工具冒烟。
- 本机已有真实运行记录（DeepSeek 规划、30 条证据、2 个 UAC 动作成功）；`Search/Fetch` 仍待真实使用验收。
- 日志导出脚本 `scripts/collect_logs.py` 会把审计/记录复制到项目根目录 `Log/`，并生成 `latest-summary.md`。

## 7. 已知取舍

1. 用进程内 JSON 快照而非 JSON-RPC；保留清晰接口，后续可替换。
2. 基线取证由本地规则并行执行，模型读取后再决定深入；这不产生本地“分析结论”。
3. 计划展示后自动继续执行，不额外等待用户批准计划；计划只是“先呈现”。修复动作另需审批。
4. 右键菜单以 HKCU 注册，覆盖文件系统对象和 This PC/打印机文件夹；单设备/单打印机图标菜单依赖 Shell Extension，未实现，改用对象选择器。
5. 日常动作命令能力完全开放（不加系统沙箱），UI 确认与审计是主要安全阀；沙箱是最后兜底（Windows Sandbox/Hyper-V/VM），启动同样需要用户确认，且本机目前仅有 WSL，无法覆盖 Windows 驱动/内核。
6. 应用层无 token/步数限制，但 DeepSeek 服务端和模型仍有硬限制。
