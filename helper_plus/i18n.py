"""轻量 UI 国际化。

英文模式只翻译应用界面文案（菜单、按钮、标签、设置、确认对话框），
AI 生成内容、工具原始输出、日志与已生成报告保持原文，不做二次翻译。
"""

from __future__ import annotations

from typing import Any

LANGUAGES: dict[str, str] = {"zh": "中文", "en": "English"}

_language = "zh"

_EN: dict[str, str] = {
    # 菜单/动作
    "文件(&F)": "&File",
    "设置(&S)": "&Settings",
    "诊断(&D)": "&Diagnosis",
    "帮助(&H)": "&Help",
    "打开目标文件…": "Open Target File…",
    "打开目标文件夹…": "Open Target Folder…",
    "导出报告…": "Export Report…",
    "退出": "Exit",
    "设置…": "Settings…",
    "更新 SYSTEM 代理…": "Update SYSTEM Broker…",
    "还原动作文件备份…": "Restore Action Backup…",
    "开始分析": "Start Analysis",
    "停止": "Stop",
    "刷新状态": "Refresh Status",
    "系统自检…": "System Self-check…",
    "查看使用说明": "View User Guide",
    "查看架构设计文档": "View Architecture Document",
    "关于 Windows Helper Plus": "About Windows Helper Plus",
    # 主界面
    "诊断状态：": "Diagnosis status: ",
    "分析对象": "Analysis target",
    "未选择（系统级诊断）": "Not selected (system-level diagnosis)",
    "未选择（系统级分析）": "Not selected (system-level analysis)",
    "选择文件": "Choose File",
    "选择文件夹": "Choose Folder",
    "设备/打印机…": "Devices/Printers…",
    "诊断设备：": "Diagnosis device: ",
    "选择设备…": "Select device…",
    "系统：": "System: ",
    "本机系统": "Local system",
    "打印机：": "Printer: ",
    "选择打印机…": "Select printer…",
    "未选择": "Not selected",
    "就绪": "Ready",
    "分析进行中": "Analyzing",
    "用时 00:00": "Elapsed 00:00",
    "用时 ": "Elapsed ",
    "证据 0": "Evidence 0",
    "证据 ": "Evidence ",
    "证据（0）": "Evidence (0)",
    "诊断流（实时）": "Diagnosis stream (live)",
    "思考过程（实时）": "Reasoning (live)",
    "诊断概览": "Diagnosis overview",
    "诊断结论": "Diagnosis conclusion",
    "工具与修复动作执行": "Tools & remediation actions",
    "实时诊断": "Live diagnosis",
    "分析": "Analyze",
    "完成": "Done",
    "失败": "Failed",
    "成功": "Success",
    "部分完成": "Partially completed",
    "进行中": "In progress",
    "已取消": "Cancelled",
    "等待开始…": "Waiting to start…",
    "模型的深度思考会在这里实时显示。": "The model's deep reasoning is streamed here in real time.",
    "Planner 原始 JSON 响应会保留在这里，便于审计。": "Raw Planner JSON responses are kept here for auditing.",
    "AI 会先给出执行计划，再开始工具调用。": "The AI will present a plan before calling tools.",
    "分析结论": "Analysis Conclusion",
    "设置": "Settings",
    "打开设置": "Open settings",
    "正在取证…": "Collecting evidence…",
    "AI 正在分析…": "AI is analyzing…",
    "尚未生成分析结论。": "No conclusion yet.",
    "报告已导出：": "Report exported: ",
    "导出完成": "Export complete",
    "导出失败": "Export failed",
    "无可导出内容": "Nothing to export",
    "分析失败": "Analysis failed",
    "分析失败：": "Analysis failed: ",
    "正在停止，请稍候…": "Stopping, please wait…",
    "仍在停止中": "Still stopping",
    "清空实时输出": "Clear Live Output",
    "已清空实时输出。": "Live output cleared.",
    "复制摘要": "Copy Summary",
    "复制证据 ID": "Copy Evidence ID",
    "查看完整证据…": "View Full Evidence…",
    "打开配置与报告文件夹": "Open Config & Reports Folder",
    "未启动管理员进程": "Broker not running",
    "SYSTEM 代理": "SYSTEM broker",
    "身份：": "Identity: ",
    "签名：": "Signature: ",
    "状态：": "Status: ",
    "来源：": "Source: ",
    "目标：": "Target: ",
    "问题：": "Issue: ",
    "备注：": "Notes: ",
    "摘要": "Summary",
    "步骤": "Steps",
    "结果": "Result",
    "证据": "Evidence",
    "来源": "Source",
    "工具": "Tool",
    "依据": "Basis",
    "是": "Yes",
    "否": "No",
    "未知错误": "Unknown error",
    "未知设备": "Unknown device",
    "未知打印机": "Unknown printer",
    "没有设备": "No devices",
    "没有打印机": "No printers",
    "未找到 ": "Not found: ",
    "未找到：": "Not found: ",
    "目标无效": "Invalid target",
    "无效目标：": "Invalid target: ",
    "检查失败：": "Check failed: ",
    "文档不存在": "Document not found",
    "脚本不存在": "Script not found",
    "选择设备": "Select Device",
    "选择打印机": "Select Printer",
    "清空": "Clear",
    "清除": "Clear",
    "卸载": "Uninstall",
    "关闭": "Close",
    "安装/修复": "Install/Repair",
    "以管理员身份": "Run as administrator",
    "以管理员身份卸载…": "Uninstall as administrator…",
    "未安装或未启动": "Not installed or not running",
    "SYSTEM 代理：状态未知": "SYSTEM broker: unknown",
    "SYSTEM 代理：已连接": "SYSTEM broker: connected",
    "SYSTEM 代理：未就绪": "SYSTEM broker: not ready",
    "本地 Qwen：已配置": "Local Qwen: configured",
    "本地 Qwen：关闭": "Local Qwen: disabled",
    "未命名动作": "Unnamed action",
    "未命名": "Unnamed",
    "已选择文件：": "Selected file: ",
    "已选择文件夹：": "Selected folder: ",
    "已拖入目标：": "Dropped target: ",
    "无法写入报告：": "Cannot write report: ",
    "警告：": "Warning: ",
    "错误：": "Error: ",
    "还原动作文件备份": "Restore Action File Backup",
    "没有可还原的备份": "No restorable backups",
    "选择要还原的备份：": "Select a backup to restore: ",
    "确认还原": "Confirm Restore",
    "还原完成": "Restore Complete",
    "还原未完全成功": "Restore Incomplete",
    "动作文件备份已还原。": "Action file backup restored.",
    "设置已保存。": "Settings saved.",
    "检查失败": "Check failed",
    "（用户跳过了问题）": "(User skipped the question)",
    "👤 用户回答：": "👤 User answer: ",
    # 设置
    "设置 — Windows Helper Plus": "Settings — Windows Helper Plus",
    "模型与 API 设置": "Model & API Settings",
    "推理模型": "Reasoning Model",
    "模型：": "Model: ",
    "思考强度：": "Reasoning effort: ",
    "显示": "Show",
    "清除已保存": "Clear Saved",
    "API Key：": "API Key: ",
    "当前来源：": "Current source: ",
    "记住 API Key（使用 Windows DPAPI 加密保存，仅当前 Windows 用户可解密）": (
        "Remember API Key (encrypted with Windows DPAPI; only this Windows user can decrypt)"
    ),
    "上下文与执行": "Context & Execution",
    "API 超时：": "API timeout: ",
    "上下文压缩预算：": "Context compression budget: ",
    "动作超时：": "Action timeout: ",
    "限制：": "Limits: ",
    "本地 Qwen 上下文官": "Local Qwen Context Officer",
    "启用：": "Enable: ",
    "开机自启动：": "Start at login: ",
    "执行权限：": "Execution permission: ",
    "保存": "Save",
    "取消": "Cancel",
    "恢复推荐值": "Restore Recommended",
    "设置无效": "Invalid Settings",
    "保存失败": "Save Failed",
    "API Key 保存失败": "Failed to Save API Key",
    "开机自启动设置失败": "Failed to Configure Autostart",
    "未配置 API Key": "API Key not configured",
    "（自定义）": " (custom)",
    " 秒": " s",
    " 字符": " chars",
    "应用内 DPAPI Key 已保存（优先于环境变量）": (
        "In-app DPAPI key saved (takes precedence over environment variables)"
    ),
    "已保存（DPAPI 加密；留空保持不变）": "Saved (DPAPI encrypted; leave blank to keep)",
    "环境变量已设置；输入新 Key 保存后将优先使用应用内 Key": (
        "Environment variable is set; a new in-app key will take precedence after saving"
    ),
    "已清除本地保存的 API Key（环境变量不受影响）": (
        "Local DPAPI key cleared (environment variables unaffected)"
    ),
    "在 DeepSeek 开放平台获取 API Key": "Get an API Key on the DeepSeek platform",
    "界面": "Interface",
    "界面语言：": "Interface language: ",
    "切换后立即刷新当前窗口；AI 输出与已生成报告保持原语言。": (
        "The current window refreshes immediately; AI output and generated reports keep their original language."
    ),
    "Planner 默认使用 DeepSeek deepseek-flash（V4.1 Flash-1017），开启思考模式并固定 Max 推理强度。分析时会把证据发送给 DeepSeek API；需要联网检索时，会通过 DeepSeek Search/Fetch 接口获取网页。": (
        "The planner uses DeepSeek deepseek-flash (V4.1 Flash-1017) with thinking enabled and Max reasoning. "
        "Evidence is sent to the DeepSeek API for analysis; web retrieval uses DeepSeek Search/Fetch."
    ),
    "模型名使用 DeepSeek 官方别名，指向当前最新 V4.1 Flash 模型。": (
        "The model name is DeepSeek's official alias for the latest V4.1 Flash model."
    ),
    '<a href="https://platform.deepseek.com/api_keys">在 DeepSeek 开放平台获取 API Key</a>': (
        '<a href="https://platform.deepseek.com/api_keys">Get an API Key on the DeepSeek platform</a>'
    ),
    "sk-…（留空表示保留当前配置）": "sk-… (leave blank to keep current)",
    "应用层不限制步数和 token：不发送 max_tokens，不设最大轮次。上下文由压缩模块控制；可随时点击“停止”。": (
        "No app-level step/token limit: max_tokens is not sent and no round limit is set. "
        "Context is controlled by compression; you can stop at any time."
    ),
    "所有分析由 AI 完成。写入/删除/驱动/服务/注册表等修复只能通过动作提案执行，并且每一条都必须在 UI 中由用户明确确认；需要管理员时触发 UAC。": (
        "All analysis is AI-driven. Write/delete/driver/service/registry fixes are only executed via "
        "action proposals, and every action must be explicitly confirmed in the UI; admin actions trigger UAC."
    ),
    "最高权限/信任模式（调用默认放行；中高风险动作仍确认/UAC）": (
        "Highest privilege / trusted mode (tool calls allowed; medium/high-risk actions still confirm/UAC)"
    ),
    "关闭后切换 strict：所有动作都需要用户确认；路径只读工具仍按最高权限模式放行。": (
        "Turn off to use strict: every action requires confirmation; read-only tools still run under the "
        "highest permission mode."
    ),
    "启用本地 Qwen 上下文整理/病历/日志翻译": (
        "Enable local Qwen for context organization / case records / log translation"
    ),
    "CPU 推理，首次加载约 15–20 秒；不产生最终诊断，只做主模型的参考上下文。": (
        "CPU inference; first load takes about 15–20 seconds. It does not produce the final diagnosis and "
        "only provides reference context to the main model."
    ),
    "Windows 登录时自启动本地 Qwen 预检/预热": "Start local Qwen preflight/warm-up at Windows login",
    "在当前用户 Startup 文件夹创建/删除快捷方式；不需要管理员。": (
        "Creates/removes a shortcut in the current user's Startup folder; admin is not required."
    ),
    "Low — 更快": "Low — Faster",
    "High — 均衡": "High — Balanced",
    "Max — 最强推理（推荐）": "Max — Strongest reasoning (Recommended)",
    "其他（请自行补充）": "Other (please specify)",
    "已从右键菜单启动：窗口出现后将自动开始 AI 分析…": (
        "Started from the context menu: AI analysis will begin automatically when the window appears…"
    ),
    "右键启动：等待 API Key 确认后自动开始分析…": (
        "Context-menu start: analysis begins automatically after the API Key is confirmed…"
    ),
    "0 条": "0 items",
    "条": "items",
    "诊断设备：": "Diagnosis device: ",
    "诊断打印机：": "Diagnosis printer: ",
    "SYSTEM 特权只读代理正在运行。": "The SYSTEM privileged read-only broker is running.",
    "SYSTEM 特权只读代理未就绪。": "The SYSTEM privileged read-only broker is not ready.",
    "右键启动：自动开始分析…": "Context-menu start: starting analysis automatically…",
    "正在采集基线证据；随后 AI 会先生成执行计划并逐步执行。": (
        "Collecting baseline evidence; the AI will then create an execution plan and proceed step by step."
    ),
    "取证：": "Evidence: ",
    "文件…": "File…",
    "文件夹…": "Folder…",
    "执行计划": "Execution Plan",
    "原始响应（审计）": "Raw response (audit)",
    "等待取证": "Waiting for evidence",
    "未配置": "Not configured",
    "本次会话（未保存）": "This session (not saved)",
    "Windows DPAPI 已保存": "Saved in Windows DPAPI",
    "环境变量 DEEPSEEK_API_KEY（应用内未保存 Key）": (
        "Environment variable DEEPSEEK_API_KEY (no in-app key saved)"
    ),
    "模型与 API 设置…": "Model & API Settings…",
    "SYSTEM 代理：检测中…": "SYSTEM broker: checking…",
    "SYSTEM 特权只读代理…": "SYSTEM privileged read-only broker…",
    "选择一个文件进行分析": "Choose a file to analyze",
    "选择一个文件夹/项目目录进行分析": "Choose a folder/project directory to analyze",
    "从打印机列表中选择诊断对象": "Choose a diagnosis target from the printer list",
    "从 PnP 设备列表中选择诊断对象": "Choose a diagnosis target from the PnP device list",
    "导出 HTML 报告或 JSON 诊断记录": "Export an HTML report or JSON diagnosis record",
    "从动作执行前的自动备份恢复被修改/删除的文件": (
        "Restore modified/deleted files from automatic pre-action backups"
    ),
    "请求停止当前分析/动作（正在执行的命令会尽快结束）": (
        "Request stopping the current analysis/action (running commands will end soon)"
    ),
    "查看/安装 SYSTEM 只读代理（安装与卸载需要管理员权限）": (
        "View/install the SYSTEM read-only broker (admin required to install/uninstall)"
    ),
    "配置 DeepSeek 模型、Max 推理强度与 API Key": (
        "Configure the DeepSeek model, Max reasoning effort and API Key"
    ),
    "由 DeepSeek Planner 规划取证、修复与验证，输出有证据引用的结论": (
        "DeepSeek Planner plans evidence collection, remediation and verification, and returns evidence-cited conclusions"
    ),
    "检测 Windows 10/11 兼容性、本地 Qwen、API Key 与沙箱能力": (
        "Check Windows 10/11 compatibility, local Qwen, API Key and sandbox capabilities"
    ),
    "文件 / 文件夹 / 驱动器路径（支持相对路径）；留空 = 本机系统分析": (
        "File / folder / drive path (relative paths supported); blank = local system analysis"
    ),
    "可输入故障描述、命令、相对/绝对路径、品牌/型号、物理现象；AI 会解析并在信息不足时弹窗追问": (
        "Enter a fault description, command, relative/absolute path, brand/model or physical symptom; "
        "the AI will parse it and ask when information is missing"
    ),
    "Windows Helper Plus — AI 驱动的 Windows 诊断与修复": (
        "Windows Helper Plus — AI-powered Windows diagnosis and repair"
    ),
    "AI 驱动的 Windows 全对象诊断 · 交互问题定位 · 受控修复 · 联网检索": (
        "AI-powered diagnosis for all Windows objects · interactive troubleshooting · controlled repair · web retrieval"
    ),
    "提示：所有分析由 DeepSeek 完成；可选择文件、文件夹、驱动器、设备或打印机。修复动作必须经你确认。": (
        "Tip: all analysis is performed by DeepSeek; choose a file, folder, drive, device or printer. "
        "Remediation actions require your confirmation."
    ),
    "中文": "中文",
    "English": "English",
    "思考": "Thinking",
    "DeepSeek V4.1 Flash（推荐）": "DeepSeek V4.1 Flash (Recommended)",
    "DeepSeek V4 Pro（更强推理）": "DeepSeek V4 Pro (Stronger Reasoning)",
    "证据（": "Evidence (",
    # 确认/提问对话框
    "确认执行诊断工具": "Confirm Tool Execution",
    "确认修复动作 — 需要审批": "Confirm Remediation — Approval Required",
    "模型提出了以下修复动作。": "The model proposed the following remediation actions.",
    "以下命令将被真实执行。请逐条核对；": (
        "The following commands will actually run. Review each one;"
    ),
    "执行结果会写入诊断记录与审计日志。": (
        "Results will be written to the diagnosis record and audit log."
    ),
    "即将执行：": "Executing: ",
    "命令：": "Command: ",
    "原因：": "Reason: ",
    "自动备份：": "Auto backup: ",
    "回滚说明：": "Rollback: ",
    "回滚命令（": "Rollback command (",
    "风险：": "Risk: ",
    "管理员": "Administrator",
    "是（执行时触发 UAC）": "Yes (triggers UAC)",
    "其他": "Other",
    "其他：": "Other: ",
    "可选回答：": "Suggested answer: ",
    "提交回答": "Submit",
    "跳过": "Skip",
    "执行": "Run",
    "拒绝": "Reject",
    "确认码：": "Confirmation code: ",
    "高风险动作：输入 RUN 以确认": "High-risk action: type RUN to confirm",
    "动作": "Action",
    "风险": "Risk",
    "原因": "Reason",
    "我已了解该操作及其耗时/权限影响，确认执行": (
        "I understand the operation, its duration and privilege impact, and confirm execution"
    ),
    "我已阅读并理解所有命令、自动备份路径、风险与回滚说明，并同意执行上述动作": (
        "I have read and understood all commands, backup paths, risks and rollback notes, "
        "and agree to execute these actions"
    ),
    "Helper Plus — 需要补充信息": "Helper Plus — More Information Needed",
    "（用户未提供信息）": "(No information provided)",
    "系统自检": "System Self-check",
    "Windows Helper Plus — 系统自检": "Windows Helper Plus — System Self-check",
    "SYSTEM 特权只读代理": "SYSTEM Privileged Read-only Broker",
}


def set_language(language: str | None) -> None:
    global _language
    _language = "en" if str(language or "").lower().startswith("en") else "zh"


def get_language() -> str:
    return _language


def is_english() -> bool:
    return _language == "en"


def tr(text: str) -> str:
    return _EN.get(text, text) if is_english() else text


def tr_text(text: str) -> str:
    """翻译完整文案；对“前缀：动态值”类文本按已知前缀替换。"""
    if not text:
        return text
    if is_english():
        if text in _EN:
            return _EN[text]
        for source in _PREFIXES:
            if text.startswith(source):
                return _EN[source] + text[len(source) :]
        return text
    # 切回中文时反转已翻译的界面文案（只反转精确匹配/已知前缀）。
    if text in _ZH:
        return _ZH[text]
    for english, chinese in _ZH_PREFIXES:
        if english and text.startswith(english):
            return chinese + text[len(english) :]
    return text


_PREFIXES = sorted(
    [
        key
        for key in _EN
        if len(key) >= 2 and (key.endswith(("：", " ", "（")))
    ],
    key=len,
    reverse=True,
)

_ZH: dict[str, str] = {}
for _source, _translated in _EN.items():
    if _translated and _translated not in _ZH:
        _ZH[_translated] = _source
_ZH_PREFIXES = sorted(
    [(_EN[source], source) for source in _PREFIXES if _EN[source]],
    key=lambda pair: len(pair[0]),
    reverse=True,
)


def translate_widget_tree(root: Any) -> None:
    """翻译已构建控件树中的界面文案；跳过文本编辑/浏览器等原始内容。"""
    if root is None:
        return
    try:
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import (
            QAbstractButton,
            QComboBox,
            QDoubleSpinBox,
            QGroupBox,
            QLabel,
            QLineEdit,
            QListWidget,
            QPlainTextEdit,
            QSpinBox,
            QTextEdit,
            QTabWidget,
            QTableWidget,
            QTreeWidget,
            QWidget,
        )
    except Exception:  # pragma: no cover - 非 GUI 环境
        return

    for action in root.findChildren(QAction):
        if action.text():
            action.setText(tr_text(action.text()))
        if action.toolTip():
            action.setToolTip(tr_text(action.toolTip()))
        if action.statusTip():
            action.setStatusTip(tr_text(action.statusTip()))

    for widget in [root, *root.findChildren(QWidget)]:
        try:
            if isinstance(widget, (QLabel, QAbstractButton, QGroupBox)):
                current = widget.title() if isinstance(widget, QGroupBox) else widget.text()
                if current:
                    translated = tr_text(current)
                    if isinstance(widget, QGroupBox):
                        widget.setTitle(translated)
                    else:
                        widget.setText(translated)
            if isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit)) and widget.placeholderText():
                widget.setPlaceholderText(tr_text(widget.placeholderText()))
            if isinstance(widget, QComboBox):
                for index in range(widget.count()):
                    widget.setItemText(index, tr_text(widget.itemText(index)))
            if isinstance(widget, QTabWidget):
                for index in range(widget.count()):
                    widget.setTabText(index, tr_text(widget.tabText(index)))
            if isinstance(widget, (QSpinBox, QDoubleSpinBox)) and widget.suffix():
                widget.setSuffix(tr_text(widget.suffix()))
            if isinstance(widget, QTreeWidget) and widget.headerItem():
                for column in range(widget.columnCount()):
                    text = widget.headerItem().text(column)
                    if text:
                        widget.headerItem().setText(column, tr_text(text))
            if isinstance(widget, QTableWidget):
                for column in range(widget.columnCount()):
                    item = widget.horizontalHeaderItem(column)
                    if item and item.text():
                        item.setText(tr_text(item.text()))
            if isinstance(widget, QListWidget):
                for index in range(widget.count()):
                    item = widget.item(index)
                    if item and item.text():
                        item.setText(tr_text(item.text()))
            if widget.windowTitle():
                widget.setWindowTitle(tr_text(widget.windowTitle()))
            if widget.toolTip():
                widget.setToolTip(tr_text(widget.toolTip()))
            if widget.statusTip():
                widget.setStatusTip(tr_text(widget.statusTip()))
        except Exception:  # noqa: BLE001 - 单个控件失败不应中断整体翻译
            continue
