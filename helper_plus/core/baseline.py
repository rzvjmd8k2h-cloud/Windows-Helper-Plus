"""本地只读基线取证。

架构文档第 3 节要求“自低成本证据向高成本操作收敛”。在向 Planner 提问前，
系统先并行采集一轮低成本的 Windows 原生证据，让模型的每一步都有可引用的
E# 证据，而不是凭空猜测。基线本身不含任何写入操作。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable
import time

from ..config import AppConfig
from ..models import Evidence, EvidenceStore, ToolResult, ToolStep
from ..safety import SafetyError
from ..tools import ToolRegistry
from ..tools.base import ToolContext


@dataclass(frozen=True)
class BaselineTask:
    tool: str
    arguments: dict
    narration: str
    why: str = "系统规则生成的只读基线取证（非 AI 决定）"


def build_baseline_plan(target_path, config: AppConfig) -> list[BaselineTask]:
    tasks: list[BaselineTask] = [
        BaselineTask(
            "get_system_info",
            {},
            "读取 Windows 版本、硬件概况与待重启状态",
        ),
        BaselineTask(
            "get_event_logs",
            {"log": "System", "hours": config.event_hours, "max_events": config.event_max},
            "读取 System 事件日志中的错误/警告时间线",
        ),
        BaselineTask(
            "get_event_logs",
            {"log": "Application", "hours": config.event_hours, "max_events": config.event_max},
            "读取 Application 事件日志中的错误/警告时间线",
        ),
        BaselineTask(
            "get_reliability_records",
            {"limit": 20},
            "读取可靠性监视器中的崩溃/错误历史",
        ),
        BaselineTask(
            "get_wer_reports",
            {"limit": 8},
            "读取最近的 Windows 错误报告（WER）元数据",
        ),
        BaselineTask(
            "get_windows_updates",
            {"limit": 20},
            "读取已安装的 Windows 更新清单",
        ),
        BaselineTask(
            "get_network_info",
            {"limit": 30},
            "盘点网络适配器/网络设备及其驱动、链路和 IP 配置",
        ),
        BaselineTask(
            "get_device_info",
            {"limit": 40},
            "盘点 PnP 设备/外部设备及其驱动，作为品牌、型号与设备关联线索",
        ),
        BaselineTask(
            "get_printer_info",
            {"limit": 20},
            "盘点打印机/打印队列及其驱动，覆盖打印机驱动与外部设备线索",
        ),
    ]
    if target_path is not None:
        if target_path.is_dir():
            tasks.insert(
                1,
                BaselineTask(
                    "inspect_directory",
                    {
                        "max_entries": 200,
                        "recursive": bool(target_path.anchor != str(target_path)),
                    },
                    "盘点目标目录/驱动器的文件结构、类型、近期修改与项目入口",
                ),
            )
            tasks.insert(
                2,
                BaselineTask(
                    "inspect_project_manifests",
                    {"max_files": 20},
                    "读取项目清单/README/依赖与本地引用，推断软件名称、内部架构和缺失依赖",
                ),
            )
        else:
            tasks.insert(
                1,
                BaselineTask(
                    "inspect_file",
                    {"path": str(target_path)},
                    "计算目标文件 SHA-256，并读取版本、签名、下载标记等静态信息",
                ),
            )
            suffix = str(getattr(target_path, "suffix", "")).lower()
            name = getattr(target_path, "name", "")
            if suffix in {".sys", ".inf", ".cat", ".exe", ".dll"}:
                tasks.append(
                    BaselineTask(
                        "map_related_files",
                        {"path": str(target_path)},
                        "映射同目录 INF/CAT/SYS/DLL 以及关联的系统服务/驱动路径",
                    )
                )
            if suffix in {".sys", ".inf", ".cat"}:
                tasks.extend(
                    [
                        BaselineTask(
                            "get_driver_info",
                            {"query": name, "limit": 40},
                            "关联 PnP 驱动与系统驱动信息，定位目标驱动所属设备",
                        ),
                        BaselineTask(
                            "enum_drivers_pnputil",
                            {},
                            "枚举驱动仓库，匹配目标驱动的发布名称与 INF",
                        ),
                        BaselineTask(
                            "verify_signature_signtool",
                            {"path": str(target_path)},
                            "使用 SignTool 对目标驱动文件做完整 Authenticode 验证",
                        ),
                        BaselineTask(
                            "get_setupapi_log",
                            {"max_lines": 80},
                            "读取 SetupAPI 设备安装日志中的错误行",
                        ),
                    ]
                )
    return tasks


def _invoke_with_failure_capture(
    registry: ToolRegistry,
    name: str,
    arguments: dict,
    ctx: ToolContext,
) -> ToolResult:
    try:
        normalized = registry.validate_call(name, arguments)
        return registry.invoke(name, normalized, ctx)
    except SafetyError as exc:
        return ToolResult.failure("基线取证参数非法", str(exc))
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure("基线取证异常", f"{type(exc).__name__}: {exc}")


def collect_baseline(
    registry: ToolRegistry,
    store: EvidenceStore,
    ctx: ToolContext,
    *,
    start_sequence: int = 1,
    on_step: Callable[[ToolStep, Evidence], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[ToolStep]:
    tasks = build_baseline_plan(ctx.target, ctx.config)
    if on_phase:
        on_phase(
            f"正在并行执行 {len(tasks)} 项 Windows 原生只读取证（SHA-256、签名、事件日志、可靠性、WER、更新）…"
        )
    results: dict[int, tuple[BaselineTask, ToolResult]] = {}
    total = len(tasks)
    started = time.monotonic()
    max_workers = min(4, total)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="hp-baseline") as executor:
        future_map = {
            executor.submit(_invoke_with_failure_capture, registry, task.tool, task.arguments, ctx): (index, task)
            for index, task in enumerate(tasks)
        }
        for done_count, future in enumerate(as_completed(future_map), 1):
            index, task = future_map[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = ToolResult.failure("基线取证任务崩溃", f"{type(exc).__name__}: {exc}")
            results[index] = (task, result)
            if on_progress:
                on_progress(done_count, total)
            if ctx.cancel_event.is_set():
                # 工具层会尽快响应取消；继续收尾已有结果，不再发起新任务。
                pass
    steps: list[ToolStep] = []
    sequence = start_sequence
    for index in range(total):
        task, result = results[index]
        evidence = store.add(task.tool, result, target=ctx.target_display)
        step = ToolStep(
            sequence=sequence,
            tool=task.tool,
            arguments=task.arguments,
            basis=["Q1"],
            narration=task.narration,
            why=task.why,
            evidence_id=evidence.id,
            ok=result.ok,
            origin="baseline",
            duration_ms=result.duration_ms,
            error=result.error,
        )
        sequence += 1
        steps.append(step)
        if on_step:
            on_step(step, evidence)
    elapsed = time.monotonic() - started
    if on_phase:
        ok_count = sum(1 for _, result in results.values() if result.ok)
        on_phase(f"基线取证完成：{ok_count}/{total} 项成功，用时 {elapsed:.1f} 秒。")
    return steps
