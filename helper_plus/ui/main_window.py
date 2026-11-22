"""主窗口：原生 Windows 11 风格的 AI 全对象诊断与受控修复界面。

- 顶部：目标（文件/文件夹/驱动器/设备/打印机）、问题描述、开始分析；
- 左栏：概览、证据（E#）；
- 右栏：实时流、执行计划、工具/动作审计、最终分析报告；
- 底部：SYSTEM 代理、用时、模型与状态。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import html
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types
from typing import Any

from PySide6.QtCore import QSize, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QKeySequence,
    QTextCursor,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QStyle,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import AppConfig, app_data_dir, default_export_dir, reports_dir
from ..i18n import is_english, set_language, tr_text, translate_widget_tree
from ..reporting import (
    default_report_name,
    render_export_html,
    render_report_html,
)
from ..objects import list_devices, list_printers
from ..safety import SafetyError, validate_user_target
from ..secrets import get_api_key
from ..system_broker_client import SystemBrokerClient, current_user_sid
from ..targets import DiagnosisTarget
from .dialogs import ActionConfirmDialog, AskUserDialog, SelfCheckDialog, ToolConfirmDialog
from .settings_dialog import SettingsDialog
from .workers import DiagnosisWorker


_STATUS_LABELS = {
    "completed": "完成",
    "partial": "部分完成",
    "cancelled": "已取消",
    "error": "失败",
    "running": "进行中",
}


class EvidenceDetailDialog(QDialog):
    def __init__(self, evidence: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"证据 {evidence.get('id', '')} — {evidence.get('tool', '')}")
        self.resize(780, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)
        summary = QLabel(str(evidence.get("summary") or ""))
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(summary)
        meta = QLabel(
            f"来源：{evidence.get('source') or '—'}    "
            f"采集时间：{evidence.get('created_at') or '—'}    "
            f"耗时：{evidence.get('duration_ms') or 0} ms    "
            f"状态：{'成功' if evidence.get('ok') else '失败'}"
        )
        meta.setObjectName("Hint")
        meta.setWordWrap(True)
        layout.addWidget(meta)
        native_command = evidence.get("native_command") or ""
        if native_command:
            command_label = QLabel(f"原生查询：{native_command}")
            command_label.setObjectName("Hint")
            command_label.setWordWrap(True)
            command_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(command_label)
        error = evidence.get("error")
        if error:
            error_label = QLabel(f"错误：{error}")
            error_label.setWordWrap(True)
            error_label.setStyleSheet("color:#a33;")
            layout.addWidget(error_label)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setFont(_mono_font())
        text.setPlainText(json.dumps(evidence.get("data") or {}, ensure_ascii=False, indent=2))
        layout.addWidget(text, 1)
        raw = evidence.get("raw_excerpt") or ""
        if raw:
            raw_group = QGroupBox("命令原始输出摘要（前 4000 字符）")
            raw_layout = QVBoxLayout(raw_group)
            raw_text = QPlainTextEdit()
            raw_text.setReadOnly(True)
            raw_text.setFont(_mono_font())
            raw_text.setMaximumBlockCount(400)
            raw_text.setPlainText(raw)
            raw_layout.addWidget(raw_text)
            layout.addWidget(raw_group, 0)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def _mono_font() -> QFont:
    families = set(QFontDatabase.families())
    for family in ("Cascadia Mono", "Consolas", "Courier New"):
        if family in families:
            return QFont(family, 8)
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


class MainWindow(QMainWindow):
    def __init__(
        self,
        config: AppConfig,
        *,
        session_key: str = "",
        initial_target: str | None = None,
        auto_diagnose: bool = False,
        pick_target: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.session_key = session_key
        self.current_target: DiagnosisTarget | None = None
        self._special_target: DiagnosisTarget | None = None
        self._auto_start_pending = bool(initial_target and auto_diagnose)
        self.current_record: dict[str, Any] | None = None
        self.final_report: dict[str, Any] | None = None
        self.record_path: Path | None = None
        self.warnings: list[str] = []
        self._evidence_by_id: dict[str, dict[str, Any]] = {}
        self._busy = False
        self._thread: QThread | None = None
        self._worker: DiagnosisWorker | None = None
        self._planner_calls = 0
        self._ai_consent_given = False
        self.broker_client = SystemBrokerClient()
        self._stream_buffers: dict[str, list[str]] = {"narration": [], "reasoning": [], "content": []}
        self._stream_timer = QTimer(self)
        self._stream_timer.setInterval(65)
        self._stream_timer.timeout.connect(self._flush_streams)
        self._evidence_total = 0
        self._session_start = 0.0
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_elapsed_label)
        self._pending_target: DiagnosisTarget | None = None

        self.setWindowTitle("Windows Helper Plus — AI 驱动的 Windows 诊断与修复")
        self.setWindowIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.setMinimumSize(1080, 700)
        self.resize(1280, 820)
        self.setAcceptDrops(True)
        self._create_actions()
        self._create_menus()
        self._create_ui()
        self._create_statusbar()
        self._connect_actions()
        self._set_busy(False)
        self._reset_session_ui_placeholder()
        if initial_target:
            self.target_edit.setText(initial_target)
            self._refresh_target_label()
        self._refresh_model_badges()
        self.stream_hint.setText(self._initial_hint())
        self._apply_language()
        if initial_target and auto_diagnose:
            self._set_status("已从右键菜单启动：窗口出现后将自动开始 AI 分析…")
            self.stream_hint.setText(tr_text("右键启动：等待 API Key 确认后自动开始分析…"))
            QTimer.singleShot(400, self._start_context_analysis)
        elif pick_target and auto_diagnose:
            QTimer.singleShot(400, lambda: self._pick_and_start(pick_target))
        elif auto_diagnose:
            QTimer.singleShot(400, self._start_context_analysis)
        QTimer.singleShot(300, self._refresh_broker_status)
        QTimer.singleShot(700, self._startup_self_check)

    # --------------------------------------------------------------------- UI
    def _create_actions(self) -> None:
        style = self.style()
        self.action_open = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton), "打开目标文件…", self
        )
        self.action_open.setShortcut(QKeySequence("Ctrl+O"))
        self.action_open.setStatusTip("选择一个文件进行分析")

        self.action_open_folder = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon), "打开目标文件夹…", self
        )
        self.action_open_folder.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.action_open_folder.setStatusTip("选择一个文件夹/项目目录进行分析")

        self.action_pick_device = QAction("选择设备…", self)
        self.action_pick_printer = QAction("选择打印机…", self)
        self.action_pick_device.setStatusTip("从 PnP 设备列表中选择诊断对象")
        self.action_pick_printer.setStatusTip("从打印机列表中选择诊断对象")

        self.action_export = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton), "导出报告…", self
        )
        self.action_export.setShortcut(QKeySequence("Ctrl+E"))
        self.action_export.setStatusTip("导出 HTML 报告或 JSON 诊断记录")

        self.action_exit = QAction("退出", self)
        self.action_exit.setShortcut(QKeySequence("Alt+F4"))

        self.action_ai = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay), "开始分析", self
        )
        self.action_ai.setShortcut(QKeySequence("F5"))
        self.action_ai.setStatusTip("由 DeepSeek Planner 规划取证、修复与验证，输出有证据引用的结论")

        self.action_stop = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaStop), "停止", self
        )
        self.action_stop.setShortcut(QKeySequence("Ctrl+."))
        self.action_stop.setStatusTip("请求停止当前分析/动作（正在执行的命令会尽快结束）")

        self.action_clear_output = QAction("清空实时输出", self)
        self.action_clear_output.triggered.connect(self._clear_streams)

        self.action_settings = QAction("设置…", self)
        self.action_settings.setShortcut(QKeySequence("Ctrl+,"))
        self.action_settings.setStatusTip("打开设置")

        self.action_broker = QAction("SYSTEM 特权只读代理…", self)
        self.action_broker.setStatusTip("查看/安装 SYSTEM 只读代理（安装与卸载需要管理员权限）")

        self.action_open_config = QAction("打开配置与报告文件夹", self)
        self.action_architecture = QAction("查看架构设计文档", self)
        self.action_readme = QAction("查看使用说明", self)
        self.action_self_check = QAction("系统自检…", self)
        self.action_self_check.setStatusTip("检测 Windows 10/11 兼容性、本地 Qwen、API Key 与沙箱能力")

        self.action_restore_backup = QAction("还原动作文件备份…", self)
        self.action_restore_backup.setStatusTip("从动作执行前的自动备份恢复被修改/删除的文件")

        self.action_about = QAction("关于 Windows Helper Plus", self)

    def _create_menus(self) -> None:
        menu_file = self.menuBar().addMenu("文件(&F)")
        menu_file.addAction(self.action_open)
        menu_file.addAction(self.action_open_folder)
        menu_file.addAction(self.action_export)
        menu_file.addSeparator()
        menu_file.addAction(self.action_exit)

        menu_diag = self.menuBar().addMenu("诊断(&D)")
        menu_diag.addAction(self.action_ai)
        menu_diag.addAction(self.action_stop)
        menu_diag.addSeparator()
        menu_diag.addAction(self.action_pick_device)
        menu_diag.addAction(self.action_pick_printer)
        menu_diag.addSeparator()
        menu_diag.addAction(self.action_clear_output)

        menu_settings = self.menuBar().addMenu("设置(&S)")
        menu_settings.addAction(self.action_settings)
        menu_settings.addAction(self.action_broker)
        menu_settings.addAction(self.action_restore_backup)
        menu_settings.addSeparator()
        menu_settings.addAction(self.action_open_config)

        menu_help = self.menuBar().addMenu("帮助(&H)")
        menu_help.addAction(self.action_architecture)
        menu_help.addAction(self.action_readme)
        menu_help.addAction(self.action_self_check)
        menu_help.addSeparator()
        menu_help.addAction(self.action_about)

    def _create_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 8)
        root.setSpacing(10)
        root.addWidget(self._build_header())

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_left_panel())
        self.splitter.addWidget(self._build_right_panel())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([400, 840])
        root.addWidget(self.splitter, 1)

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("HeaderCard")
        layout = QVBoxLayout(header)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(9)

        title_row = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        title = QLabel("Windows Helper Plus")
        title.setObjectName("AppTitle")
        subtitle = QLabel("AI 驱动的 Windows 全对象诊断 · 交互问题定位 · 受控修复 · 联网检索")
        subtitle.setObjectName("Subtitle")
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        title_row.addLayout(title_col, 1)

        badges = QHBoxLayout()
        badges.setSpacing(10)
        self.elapsed_label = QLabel("用时 00:00")
        self.elapsed_label.setObjectName("ElapsedLabel")
        self.elapsed_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.model_badge = QLabel()
        self.model_badge.setObjectName("ModelBadge")
        self.model_badge.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        badges.addWidget(self.elapsed_label)
        badges.addWidget(self.model_badge)
        title_row.addLayout(badges)
        layout.addLayout(title_row)

        target_row = QHBoxLayout()
        target_row.setSpacing(6)
        target_label = QLabel("目标：")
        target_label.setMinimumWidth(46)
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("文件 / 文件夹 / 驱动器路径（支持相对路径）；留空 = 本机系统分析")
        self.target_edit.setClearButtonEnabled(True)
        self.browse_button = QPushButton("文件…")
        self.browse_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        self.browse_button.setIconSize(QSize(16, 16))
        self.folder_button = QPushButton("文件夹…")
        self.folder_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.folder_button.setIconSize(QSize(16, 16))
        self.object_button = QPushButton("设备/打印机…")
        self.object_button.setToolTip("从 PnP 设备或打印机列表选择诊断对象")
        self.clear_target_button = QPushButton("清除")
        target_row.addWidget(target_label)
        target_row.addWidget(self.target_edit, 1)
        target_row.addWidget(self.browse_button)
        target_row.addWidget(self.folder_button)
        target_row.addWidget(self.object_button)
        target_row.addWidget(self.clear_target_button)
        layout.addLayout(target_row)

        issue_row = QHBoxLayout()
        issue_row.setSpacing(6)
        issue_label = QLabel("问题：")
        issue_label.setMinimumWidth(46)
        self.issue_edit = QLineEdit()
        self.issue_edit.setPlaceholderText(
            "可输入故障描述、命令、相对/绝对路径、品牌/型号、物理现象；AI 会解析并在信息不足时弹窗追问"
        )
        issue_row.addWidget(issue_label)
        issue_row.addWidget(self.issue_edit, 1)
        layout.addLayout(issue_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        hint = QLabel("提示：所有分析由 DeepSeek 完成；可选择文件、文件夹、驱动器、设备或打印机。修复动作必须经你确认。")
        hint.setObjectName("Hint")
        action_row.addWidget(hint, 1)
        self.ai_button = QPushButton("开始分析")
        self.ai_button.setObjectName("PrimaryButton")
        self.ai_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.ai_button.setIconSize(QSize(16, 16))
        self.ai_button.setToolTip(
            "DeepSeek Planner 将制定计划、调用工具、必要时联网/提问/提出修复动作（F5）。\n"
            "分析证据会发送给 DeepSeek API；修复动作执行前一定由你确认。"
        )
        self.stop_button = QPushButton("停止")
        self.stop_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_button.setEnabled(False)
        action_row.addWidget(self.ai_button)
        action_row.addWidget(self.stop_button)
        layout.addLayout(action_row)
        return header

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(340)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        overview_group = QGroupBox("诊断概览")
        form = QFormLayout(overview_group)
        form.setContentsMargins(10, 8, 10, 10)
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.overview_status = QLabel("就绪")
        self.overview_target = QLabel("未选择（系统级诊断）")
        self.overview_system = QLabel("等待取证")
        self.overview_file = QLabel("—")
        self.overview_hash = QLabel("—")
        self.overview_hash.setFont(_mono_font())
        self.overview_evidence = QLabel("0 条")
        for label in (
            self.overview_status,
            self.overview_target,
            self.overview_system,
            self.overview_file,
            self.overview_hash,
        ):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        form.addRow("状态：", self.overview_status)
        form.addRow("目标：", self.overview_target)
        form.addRow("系统：", self.overview_system)
        form.addRow("签名：", self.overview_file)
        form.addRow("SHA-256：", self.overview_hash)
        form.addRow("取证：", self.overview_evidence)
        layout.addWidget(overview_group, 0)

        self.evidence_group = QGroupBox("证据（0）")
        evidence_layout = QVBoxLayout(self.evidence_group)
        evidence_layout.setContentsMargins(8, 8, 8, 8)
        self.evidence_tree = QTreeWidget()
        self.evidence_tree.setColumnCount(3)
        self.evidence_tree.setHeaderLabels(["ID", "来源", "摘要"])
        self.evidence_tree.setRootIsDecorated(False)
        self.evidence_tree.setAlternatingRowColors(True)
        self.evidence_tree.setUniformRowHeights(True)
        self.evidence_tree.setColumnWidth(0, 44)
        self.evidence_tree.setColumnWidth(1, 118)
        self.evidence_tree.header().setStretchLastSection(True)
        self.evidence_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.evidence_tree.setToolTip("双击查看证据的完整 JSON；E# 可在 AI 结论中作为引用依据。")
        evidence_layout.addWidget(self.evidence_tree)
        layout.addWidget(self.evidence_group, 1)
        return panel

    def _build_right_panel(self) -> QWidget:
        self.right_tabs = QTabWidget()
        self.right_tabs.setDocumentMode(True)

        live = QWidget()
        live_layout = QVBoxLayout(live)
        live_layout.setContentsMargins(8, 8, 8, 8)
        live_layout.setSpacing(6)

        # 用户要求：实时诊断标签下不再显示“就绪/模型思考…”等小字提示。
        # 保留对象以兼容内部状态更新，但不加入布局、不显示。
        self.stream_hint = QLabel(self._initial_hint())
        self.stream_hint.setObjectName("Hint")
        self.stream_hint.setWordWrap(True)
        self.stream_hint.hide()

        self.stream_tabs = QTabWidget()
        self.stream_tabs.setDocumentMode(True)
        self.narration_edit = self._make_stream_edit("等待开始…")
        self.reasoning_edit = self._make_stream_edit("模型的深度思考会在这里实时显示。")
        self.raw_edit = self._make_stream_edit("Planner 原始 JSON 响应会保留在这里，便于审计。")
        self.raw_edit.setFont(_mono_font())
        self.stream_tabs.addTab(self.narration_edit, "诊断流（实时）")
        self.stream_tabs.addTab(self.reasoning_edit, "思考过程（实时）")
        self.stream_tabs.addTab(self.raw_edit, "原始响应（审计）")
        live_layout.addWidget(self.stream_tabs, 3)

        audit_group = QGroupBox("工具与修复动作执行")
        audit_layout = QVBoxLayout(audit_group)
        audit_layout.setContentsMargins(8, 8, 8, 8)
        self.audit_tree = QTreeWidget()
        self.audit_tree.setColumnCount(5)
        self.audit_tree.setHeaderLabels(["步骤", "工具", "依据", "证据", "结果"])
        self.audit_tree.setRootIsDecorated(False)
        self.audit_tree.setAlternatingRowColors(True)
        self.audit_tree.setUniformRowHeights(True)
        self.audit_tree.setColumnWidth(0, 44)
        self.audit_tree.setColumnWidth(1, 190)
        self.audit_tree.setColumnWidth(2, 120)
        self.audit_tree.setColumnWidth(3, 56)
        self.audit_tree.header().setStretchLastSection(True)
        self.audit_tree.setToolTip("双击可复制该步骤的完整 JSON。")
        audit_layout.addWidget(self.audit_tree)
        live_layout.addWidget(audit_group, 2)

        self.right_tabs.addTab(live, "实时诊断")

        self.plan_view = QPlainTextEdit()
        self.plan_view.setObjectName("StreamPane")
        self.plan_view.setReadOnly(True)
        self.plan_view.setPlaceholderText("AI 会先给出执行计划，再开始工具调用。")
        self.plan_view.setMaximumBlockCount(2000)
        self.plan_view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.right_tabs.addTab(self.plan_view, "执行计划")

        self.conclusion_view = QTextBrowser()
        self.conclusion_view.setObjectName("ConclusionPane")
        self.conclusion_view.setOpenExternalLinks(False)
        self.conclusion_view.setOpenLinks(False)
        self.conclusion_view.setHtml(self._placeholder_report_html())
        self.right_tabs.addTab(self.conclusion_view, "诊断结论")
        self.right_tabs.setCurrentIndex(0)
        return self.right_tabs

    def _make_stream_edit(self, placeholder: str) -> QPlainTextEdit:
        edit = QPlainTextEdit()
        edit.setObjectName("StreamPane")
        edit.setReadOnly(True)
        edit.setPlaceholderText(placeholder)
        edit.setMaximumBlockCount(8000)
        edit.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        edit.setUndoRedoEnabled(False)
        edit.setCenterOnScroll(False)
        return edit

    def _create_statusbar(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        self.status_label = QLabel("就绪")
        self.status_label.setMinimumWidth(200)
        bar.addWidget(self.status_label, 1)
        self.broker_status_label = QLabel("SYSTEM 代理：检测中…")
        self.broker_status_label.setObjectName("Hint")
        self.status_elapsed = QLabel("用时 00:00")
        self.status_elapsed.setObjectName("Hint")
        self.status_evidence = QLabel("证据 0")
        self.local_status_label = QLabel(
            "本地 Qwen：已配置" if getattr(self.config, "local_model_enabled", True) else "本地 Qwen：关闭"
        )
        self.local_status_label.setObjectName("Hint")
        self.status_model = QLabel()
        self.status_model.setObjectName("Hint")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(110)
        self.progress.setTextVisible(False)
        self.progress.setToolTip("正在运行取证/模型规划/动作…")
        bar.addPermanentWidget(self.broker_status_label)
        bar.addPermanentWidget(self.local_status_label)
        bar.addPermanentWidget(self.status_elapsed)
        bar.addPermanentWidget(self.status_evidence)
        bar.addPermanentWidget(self.status_model)
        bar.addPermanentWidget(self.progress)

    def _connect_actions(self) -> None:
        self.action_open.triggered.connect(self._on_browse)
        self.action_open_folder.triggered.connect(self._on_browse_folder)
        self.action_export.triggered.connect(self._on_export)
        self.action_exit.triggered.connect(self.close)
        self.action_ai.triggered.connect(self._start_analysis)
        self.action_stop.triggered.connect(self._stop_diagnosis)
        self.action_settings.triggered.connect(self._open_settings)
        self.action_broker.triggered.connect(self._show_broker_dialog)
        self.action_pick_device.triggered.connect(self._pick_device)
        self.action_pick_printer.triggered.connect(self._pick_printer)
        self.action_open_config.triggered.connect(self._open_config_folder)
        self.action_architecture.triggered.connect(lambda: self._open_local_doc("架构设计.md"))
        self.action_readme.triggered.connect(lambda: self._open_local_doc("README.md"))
        self.action_about.triggered.connect(self._show_about)
        self.action_self_check.triggered.connect(self._show_self_check)
        self.action_restore_backup.triggered.connect(self._restore_action_backup)
        self.browse_button.clicked.connect(self._on_browse)
        self.folder_button.clicked.connect(self._on_browse_folder)
        self.object_button.clicked.connect(self._pick_object)
        self.clear_target_button.clicked.connect(self._clear_target)
        self.ai_button.clicked.connect(self._start_analysis)
        self.stop_button.clicked.connect(self._stop_diagnosis)
        self.target_edit.editingFinished.connect(self._refresh_target_label)
        self.target_edit.returnPressed.connect(self._start_analysis)
        self.evidence_tree.itemDoubleClicked.connect(self._show_evidence_details)
        self.evidence_tree.customContextMenuRequested.connect(self._evidence_context_menu)
        self.audit_tree.itemDoubleClicked.connect(self._copy_step_json)

    # ------------------------------------------------------------------ status
    def _initial_hint(self) -> str:
        key = get_api_key(self.session_key)
        if is_english():
            if key:
                return (
                    f"Ready · {self.config.model_label} · All analysis is AI-driven; "
                    "files/folders/drives/devices/printers supported"
                )
            return (
                "DeepSeek API Key is not configured. All analysis requires AI; "
                "save an API Key in Settings first."
            )
        if key:
            return f"就绪 · {self.config.model_label} · 所有分析由 AI 完成；可分析文件/文件夹/驱动器/设备/打印机"
        return "尚未配置 DeepSeek API Key。所有分析必须经过 AI，请先在“设置”中保存 API Key。"

    def _refresh_model_badges(self) -> None:
        self.model_badge.setText(self.config.model_label)
        self.status_model.setText(f"{self.config.model} · {self.config.reasoning_effort.upper()}")
        if hasattr(self, "local_status_label"):
            self.local_status_label.setText(
                tr_text("本地 Qwen：已配置")
                if getattr(self.config, "local_model_enabled", True)
                else tr_text("本地 Qwen：关闭")
            )

    def _update_elapsed_label(self) -> None:
        if not self._session_start:
            self.elapsed_label.setText(tr_text("用时 00:00"))
            self.status_elapsed.setText(tr_text("用时 00:00"))
            return
        seconds = int(time.monotonic() - self._session_start)
        text = tr_text(f"用时 {seconds // 60:02d}:{seconds % 60:02d}")
        self.elapsed_label.setText(text)
        self.status_elapsed.setText(text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (
            self.action_open,
            self.action_open_folder,
            self.action_ai,
            self.action_settings,
            self.action_pick_device,
            self.action_pick_printer,
            self.browse_button,
            self.folder_button,
            self.object_button,
            self.clear_target_button,
            self.ai_button,
            self.target_edit,
            self.issue_edit,
        ):
            widget.setEnabled(not busy)
        self.action_stop.setEnabled(busy)
        self.stop_button.setEnabled(busy)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
        self.action_export.setEnabled((not busy) and bool(self.current_record and self.final_report))
        self._update_status_actions()

    def _update_status_actions(self) -> None:
        self.action_export.setEnabled((not self._busy) and bool(self.current_record and self.final_report))

    def _set_status(self, text: str) -> None:
        self.status_label.setText(tr_text(text))
        if not self._busy:
            self.statusBar().showMessage("", 0)

    def _apply_language(self) -> None:
        # 仅在英文模式或从英文切回中文时翻译控件树，避免把用户数据误当作文案反转。
        if is_english() or getattr(self, "_ui_translated_to_english", False):
            translate_widget_tree(self)
        self._ui_translated_to_english = is_english()
        current = self.conclusion_view.toPlainText()
        if "尚未生成分析结论" in current or "No conclusion yet" in current:
            self.conclusion_view.setHtml(self._placeholder_report_html())

    # ------------------------------------------------------------------ target
    def _start_dir(self) -> str:
        if self.current_target and self.current_target.path:
            return str(self.current_target.path if self.current_target.path.is_dir() else self.current_target.path.parent)
        return os.environ.get("SystemRoot", "C:\\Windows")

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择要分析的文件", self._start_dir(), "所有文件 (*.*)")
        if not path:
            return
        self._set_path_target(path)
        self._set_status(f"已选择文件：{Path(path).name}。点击“开始分析”。")

    def _on_browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择要分析的文件夹/项目目录", self._start_dir())
        if not path:
            return
        self._set_path_target(path)
        self._set_status(f"已选择文件夹：{path}。点击“开始分析”。")

    def _set_path_target(self, raw_path: str) -> None:
        self._special_target = None
        self.target_edit.setReadOnly(False)
        self.target_edit.setText(raw_path)
        self.current_target = None
        self._refresh_target_label()

    def _set_special_target(self, target: DiagnosisTarget) -> None:
        self._special_target = target
        self.current_target = target
        self.target_edit.setReadOnly(True)
        self.target_edit.setText(target.display)
        self.overview_target.setText(target.display)
        self.setWindowTitle(f"{target.display} — Windows Helper Plus")

    def _pick_object(self) -> None:
        menu = QMenu(self)
        act_device = menu.addAction("选择设备…")
        act_printer = menu.addAction("选择打印机…")
        chosen = menu.exec(self.object_button.mapToGlobal(self.object_button.rect().bottomLeft()))
        if chosen == act_device:
            self._pick_device()
        elif chosen == act_printer:
            self._pick_printer()

    def _pick_and_start(self, kind: str) -> None:
        if kind == "device":
            self._pick_device(auto_start=True)
        elif kind == "printer":
            self._pick_printer(auto_start=True)

    def _pick_device(self, auto_start: bool = False) -> None:
        devices = list_devices()
        if not devices:
            QMessageBox.information(self, "没有设备", "未枚举到 PnP 设备，或 WMI 查询失败。")
            return
        labels = []
        by_label: dict[str, dict[str, Any]] = {}
        for item in devices:
            name = str(item.get("Name") or item.get("DeviceID") or "未知设备")
            status = str(item.get("Status") or "")
            label = f"{name}  [{status}]  {item.get('DeviceID') or ''}".strip()
            if label in by_label:
                label = f"{label} #{len(by_label)}"
            labels.append(label)
            by_label[label] = item
        selected, ok = QInputDialog.getItem(self, "选择设备", "PnP 设备：", labels, 0, False)
        if not ok or not selected:
            return
        item = by_label.get(selected, {})
        target = DiagnosisTarget.device(
            str(item.get("DeviceID") or selected),
            str(item.get("Name") or selected),
            metadata=item,
        )
        self._set_special_target(target)
        self.issue_edit.setText(
            tr_text(f"诊断设备：{item.get('Name') or selected}；请检查状态/驱动/相关日志。")
        )
        self._set_status("已选择设备；点击“开始分析”。")
        if auto_start:
            QTimer.singleShot(250, self._start_analysis)

    def _pick_printer(self, auto_start: bool = False) -> None:
        printers = list_printers()
        if not printers:
            QMessageBox.information(self, "没有打印机", "未枚举到打印机，或 WMI 查询失败。")
            return
        labels = []
        by_label: dict[str, dict[str, Any]] = {}
        for item in printers:
            name = str(item.get("Name") or "未知打印机")
            driver = str(item.get("DriverName") or "")
            label = f"{name}  [{driver}]"
            if label in by_label:
                label = f"{label} #{len(by_label)}"
            labels.append(label)
            by_label[label] = item
        selected, ok = QInputDialog.getItem(self, "选择打印机", "打印机：", labels, 0, False)
        if not ok or not selected:
            return
        item = by_label.get(selected, {})
        target = DiagnosisTarget.printer(str(item.get("Name") or selected), metadata=item)
        self._set_special_target(target)
        self.issue_edit.setText(
            tr_text(f"诊断打印机：{item.get('Name') or selected}；请检查驱动/端口/队列状态。")
        )
        self._set_status("已选择打印机；点击“开始分析”。")
        if auto_start:
            QTimer.singleShot(250, self._start_analysis)

    def _clear_target(self) -> None:
        self.target_edit.clear()
        self.target_edit.setReadOnly(False)
        self.current_target = None
        self._special_target = None
        self.issue_edit.clear()
        self._refresh_target_label()
        self._set_status("已切换为本机系统分析（目标为空）。")

    def _refresh_target_label(self) -> None:
        if self._special_target is not None:
            self.overview_target.setText(self._special_target.display)
            self.setWindowTitle(f"{self._special_target.display} — Windows Helper Plus")
            return
        text = self.target_edit.text().strip()
        if not text:
            self.overview_target.setText(tr_text("未选择（系统级分析）"))
            return
        try:
            path = validate_user_target(text, allow_directory=True)
        except SafetyError as exc:
            self.overview_target.setText(tr_text(f"无效目标：{exc}"))
            return
        self.overview_target.setText(str(path))
        self.setWindowTitle(f"{path.name} — Windows Helper Plus")

    def _validate_target(self) -> tuple[bool, DiagnosisTarget]:
        if self._special_target is not None:
            self.current_target = self._special_target
            return True, self._special_target
        raw = self.target_edit.text().strip()
        if not raw:
            target = DiagnosisTarget.system()
            self.current_target = target
            return True, target
        try:
            path = validate_user_target(raw, allow_directory=True)
            assert path is not None
            target = DiagnosisTarget.from_path(path)
        except (SafetyError, ValueError) as exc:
            QMessageBox.warning(self, "目标无效", str(exc))
            return False, DiagnosisTarget.system()
        self.target_edit.setText(str(target.path) if target.path else raw)
        self.current_target = target
        self._refresh_target_label()
        return True, target

    # ------------------------------------------------------------------ settings
    def _open_settings(self) -> bool:
        dialog = SettingsDialog(self.config, self.session_key, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        old_language = self.config.ui_language
        self.config = dialog.config
        if dialog.saved:
            self.session_key = dialog.effective_session_key
        if self.config.ui_language != old_language:
            set_language(self.config.ui_language)
            self._apply_language()
        self._refresh_model_badges()
        self.stream_hint.setText(self._initial_hint())
        self._set_status("设置已保存。")
        return True

    def _restore_action_backup(self) -> None:
        from ..core.actions import list_action_backups, restore_action_backup

        backups = list_action_backups()
        if not backups:
            QMessageBox.information(
                self,
                "没有可还原的备份",
                "尚未记录到动作文件备份。\n\n"
                "修复动作只有在 AI 提供 backup_paths 时才会自动备份；"
                "备份位置为 Log\\actions\\<时间>\\backup。",
            )
            return
        labels = [
            f"{item['created_at']} | {item['title'] or '未命名动作'} | {item['paths']} 项"
            for item in backups
        ]
        selected, ok = QInputDialog.getItem(
            self, "还原动作文件备份", "选择要还原的备份：", labels, 0, False
        )
        if not ok or not selected:
            return
        index = labels.index(selected)
        item = backups[index]
        answer = QMessageBox.warning(
            self,
            "确认还原",
            "将按备份覆盖原路径；目录会先删除现有内容再恢复。\n\n"
            f"动作：{item['title'] or '未命名'}\n"
            f"时间：{item['created_at']}\n"
            f"路径数：{item['paths']}\n\n"
            "此操作不会自动创建新的还原点，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = restore_action_backup(item["manifest"])
        if result.get("ok"):
            QMessageBox.information(
                self,
                "还原完成",
                f"已还原 {len(result.get('restored') or [])} 个路径。\n\n清单：{item['manifest']}",
            )
            self._set_status("动作文件备份已还原。")
        else:
            errors = "\n".join(
                f"{entry.get('path')}: {entry.get('error')}"
                for entry in (result.get("errors") or [])[:8]
            )
            QMessageBox.warning(
                self,
                "还原未完全成功",
                f"{result.get('error') or ''}\n{errors}".strip(),
            )

    def _refresh_broker_status(self) -> None:
        try:
            info = self.broker_client.probe()
        except Exception as exc:  # noqa: BLE001
            self.broker_status_label.setText(tr_text("SYSTEM 代理：状态未知"))
            self.broker_status_label.setToolTip(f"无法查询 SYSTEM 代理：{exc}")
            return
        if info.available:
            self.broker_status_label.setText(tr_text("SYSTEM 代理：已连接"))
            self.broker_status_label.setStyleSheet("color:#0b6b3a;")
            detail = (
                f"{info.identity}\nPID：{info.pid or '—'}\n"
                f"心跳：{SystemBrokerClient.format_age(info)}\n"
                f"任务：{info.processed} 次请求"
            )
        else:
            self.broker_status_label.setText(tr_text("SYSTEM 代理：未就绪"))
            self.broker_status_label.setStyleSheet("color:#777;")
            detail = info.detail or "未安装或未启动"
            detail += "\n通过“设置 → SYSTEM 特权只读代理…”安装（需要管理员权限）。"
        self.broker_status_label.setToolTip(detail)

    def _show_broker_dialog(self) -> None:
        info = self.broker_client.probe()
        box = QMessageBox(self)
        box.setWindowTitle("SYSTEM 特权只读代理")
        box.setIcon(QMessageBox.Icon.Information)
        if info.available:
            box.setText(tr_text("SYSTEM 特权只读代理正在运行。"))
            box.setInformativeText(
                f"身份：{info.identity}\n"
                f"最后心跳：{SystemBrokerClient.format_age(info)}\n"
                f"已处理请求：{info.processed}\n\n"
                "运行诊断时，应用会自动请求该代理对所选文件做一次独立的只读校验"
                "（签名、SHA-256、所有者、ACL 等），结果作为额外证据显示。代理不会执行命令、"
                "不会修改系统，也不会自动联网。"
            )
        else:
            box.setText(tr_text("SYSTEM 特权只读代理未就绪。"))
            box.setInformativeText(
                f"{info.detail}\n\n"
                "安装需要管理员权限（通过 UAC 一次）。代理以 NT AUTHORITY\\SYSTEM 运行于"
                "受保护的 Program Files 目录，只接受固定的只读 inspect_target / ping 请求。"
                "普通用户 UI 和 AI Planner 不会获得 SYSTEM 权限。"
            )
        box.setDetailedText(
            "安装脚本：scripts\\install_system_broker.ps1\n"
            "卸载脚本：scripts\\uninstall_system_broker.ps1\n"
            "数据目录：%ProgramData%\\WindowsHelperPlus\\SystemBroker\n"
            "任务名：WindowsHelperPlusSystemBroker\n\n"
            "安全边界：代理只读；目标位于凭据/审计存储目录时会被拒绝；"
            "请求/响应队列只允许当前操作员 SID 访问。"
        )
        refresh_button = box.addButton("刷新状态", QMessageBox.ButtonRole.ActionRole)
        install_button = box.addButton("以管理员身份安装/修复…", QMessageBox.ButtonRole.ActionRole)
        uninstall_button = box.addButton("以管理员身份卸载…", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        clicked = box.clickedButton()
        if clicked == refresh_button:
            self._refresh_broker_status()
        elif clicked == install_button:
            self._run_broker_installer(uninstall=False)
        elif clicked == uninstall_button:
            self._run_broker_installer(uninstall=True)

    def _run_broker_installer(self, *, uninstall: bool) -> None:
        project_root = Path(__file__).resolve().parents[2]
        script = project_root / "scripts" / (
            "uninstall_system_broker.ps1" if uninstall else "install_system_broker.ps1"
        )
        if not script.exists():
            QMessageBox.warning(self, "脚本不存在", f"未找到：{script}")
            return
        if sys.platform != "win32":
            QMessageBox.information(self, "仅 Windows", "SYSTEM 代理安装仅支持 Windows。")
            return
        action_name = "卸载" if uninstall else "安装/修复"
        answer = QMessageBox.question(
            self,
            f"以管理员身份{action_name} SYSTEM 代理",
            f"即将请求管理员权限来{action_name} SYSTEM 只读代理。\n\n"
            "代理将以 NT AUTHORITY\\SYSTEM 常驻运行，但只执行固定的只读文件校验，"
            "不会执行任意命令或修改系统。\n\n"
            "Windows 会显示 UAC 提示；如果使用另一个管理员账户确认，"
            "请确保操作员 SID 已正确传递。\n\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        if not uninstall:
            sid = current_user_sid()
            if not sid:
                QMessageBox.warning(
                    self,
                    "无法获取当前用户 SID",
                    "无法读取当前用户 SID。请以管理员身份运行脚本并手动传入 -OperatorSid。",
                )
                return
            args.extend(["-OperatorSid", sid])
        command_line = subprocess.list2cmdline(args)
        try:
            shell32 = ctypes.windll.shell32
            shell32.ShellExecuteW.argtypes = [
                wintypes.HWND,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                ctypes.c_int,
            ]
            shell32.ShellExecuteW.restype = ctypes.c_void_p
            result = shell32.ShellExecuteW(
                None,
                "runas",
                "powershell.exe",
                command_line,
                str(project_root),
                1,
            )
            code = int(result) if result else 0
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法启动管理员进程", str(exc))
            return
        if code <= 32:
            QMessageBox.warning(
                self,
                "未启动管理员进程",
                "UAC 被取消或管理员进程启动失败。\n"
                "你也可以手动以管理员身份运行 scripts 目录下的脚本。",
            )
            return
        self._set_status("已请求管理员权限；请在 UAC 提示中确认，然后等待代理安装完成。")
        QTimer.singleShot(3000, self._refresh_broker_status)
        QTimer.singleShot(8000, self._refresh_broker_status)
        QTimer.singleShot(15000, self._refresh_broker_status)

    def _ensure_api_key(self) -> bool:
        if get_api_key(self.session_key):
            return True
        QMessageBox.information(
            self,
            "需要 DeepSeek API Key",
            "本版本所有分析都由 DeepSeek 完成，必须配置 API Key。\n\n"
            "接下来会打开设置窗口。填写 Key 后点击“保存”即可；"
            "应用内保存的 Key 会优先于环境变量，并使用 Windows DPAPI 加密。",
        )
        self._open_settings()
        return bool(get_api_key(self.session_key))

    # --------------------------------------------------------------- analysis
    def _start_context_analysis(self) -> None:
        if self._busy:
            return
        self._set_status("右键启动：正在自动开始 AI 分析…")
        self.stream_hint.setText(tr_text("右键启动：自动开始分析…"))
        self._start_analysis()

    def _start_analysis(self) -> None:
        if self._busy:
            return
        if not self._ensure_api_key():
            return
        if not self._ai_consent_given:
            answer = QMessageBox.question(
                self,
                "AI 分析与数据发送",
                "分析会把本次采集的证据（系统信息、文件路径/哈希/签名、事件消息摘要、"
                "驱动/设备/打印机/网络适配器/WER/SetupAPI 摘要、项目清单与依赖摘要等）"
                "发送给 DeepSeek API 用于推理。\n\n"
                "模型还可能调用 DeepSeek Search/Fetch 进行有目的的联网检索；"
                "修复动作会在执行前逐条请你确认。\n\n"
                "不会上传完整文件内容或内存转储；报告默认只保存在本机。是否开始？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._ai_consent_given = True
        valid, target = self._validate_target()
        if not valid:
            return
        api_key = get_api_key(self.session_key)
        if not api_key:
            self._set_status("未配置 API Key，分析未启动。")
            return
        self.current_target = target
        self._reset_session_ui()
        self._session_start = time.monotonic()
        self._elapsed_timer.start()
        self._update_elapsed_label()
        self._set_status("正在启动 AI 分析：先进行全量基线取证，然后展示 Planner 的执行计划…")
        self.stream_hint.setText(
            tr_text("正在采集基线证据；随后 AI 会先生成执行计划并逐步执行。")
        )
        self._append_stream("narration", f"▸ 分析对象：{target.display}\n")
        self._set_busy(True)
        self._planner_calls = 0

        worker = DiagnosisWorker(
            config=self.config,
            target=target,
            issue=self.issue_edit.text().strip(),
            api_key=api_key,
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.phase.connect(self._on_phase)
        worker.reasoning.connect(self._on_reasoning)
        worker.content.connect(self._on_content)
        worker.narration.connect(self._on_narration)
        worker.plan_ready.connect(self._on_plan)
        worker.attempt_started.connect(self._on_attempt_started)
        worker.evidence_added.connect(self._on_evidence)
        worker.step_added.connect(self._on_step)
        worker.warning.connect(self._on_warning)
        worker.usage.connect(self._on_usage)
        worker.final_report.connect(self._on_final_report)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.question_asked.connect(self._on_user_question)
        worker.tool_confirmation_requested.connect(self._on_tool_confirmation)
        worker.actions_proposed.connect(self._on_actions_proposed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished)
        self._worker = worker
        self._thread = thread
        thread.start()

    def _stop_diagnosis(self) -> None:
        if not self._worker:
            return
        self._worker.cancel_event.set()
        self.stop_button.setEnabled(False)
        self.action_stop.setEnabled(False)
        self._set_status("正在请求停止…（当前命令/动作完成后会尽快退出）")
        self.stream_hint.setText(tr_text("正在停止，请稍候…"))
        self._elapsed_timer.stop()

    def _reset_session_ui(self) -> None:
        self.evidence_tree.clear()
        self.audit_tree.clear()
        self.narration_edit.clear()
        self.reasoning_edit.clear()
        self.raw_edit.clear()
        self.plan_view.clear()
        for buffer in self._stream_buffers.values():
            buffer.clear()
        self.conclusion_view.setHtml(self._placeholder_report_html("AI 正在分析…"))
        self.right_tabs.setCurrentIndex(0)
        self.stream_tabs.setCurrentIndex(0)
        self.final_report = None
        self.current_record = None
        self.record_path = None
        self.warnings = []
        self._evidence_by_id = {}
        self._evidence_total = 0
        self.overview_status.setText("进行中")
        target_text = self.current_target.display if self.current_target else "本机系统"
        self.overview_target.setText(target_text)
        self.overview_system.setText("正在取证…")
        self.overview_file.setText("—")
        self.overview_hash.setText("—")
        self.overview_evidence.setText("0 条")
        self.evidence_group.setTitle("证据（0）")
        self.status_evidence.setText("证据 0")
        self.elapsed_label.setText("用时 00:00")
        self.status_elapsed.setText("用时 00:00")
        self.action_export.setEnabled(False)
        self._apply_language()

    def _reset_session_ui_placeholder(self) -> None:
        self.overview_target.setText("未选择（系统级分析）")
        self.overview_status.setText("就绪")
        self._apply_language()

    # ---------------------------------------------------------------- streams
    def _append_stream(self, kind: str, text: str) -> None:
        if not text:
            return
        self._stream_buffers[kind].append(text)
        if not self._stream_timer.isActive():
            self._stream_timer.start()

    def _flush_streams(self) -> None:
        mapping = {
            "narration": self.narration_edit,
            "reasoning": self.reasoning_edit,
            "content": self.raw_edit,
        }
        any_text = False
        for kind, chunks in self._stream_buffers.items():
            if not chunks:
                continue
            any_text = True
            text = "".join(chunks)
            chunks.clear()
            edit = mapping[kind]
            edit.moveCursor(QTextCursor.MoveOperation.End)
            edit.insertPlainText(text)
            edit.moveCursor(QTextCursor.MoveOperation.End)
        if not any_text:
            self._stream_timer.stop()

    def _clear_streams(self) -> None:
        self.narration_edit.clear()
        self.reasoning_edit.clear()
        self.raw_edit.clear()
        for buffer in self._stream_buffers.values():
            buffer.clear()
        self._stream_timer.stop()
        self._set_status("已清空实时输出。")

    # --------------------------------------------------------------- callbacks
    def _on_phase(self, text: str) -> None:
        self._set_status(text)
        self._append_stream("narration", f"\n▸ {text}\n")
        self.stream_hint.setText(text)

    def _on_plan(self, plan: dict[str, Any]) -> None:
        tools = plan.get("tools") or []
        steps = plan.get("steps") or []
        lines = [
            "执行计划",
            "=" * 40,
            f"目标：{plan.get('goal') or ''}",
            "",
            "计划步骤：",
        ]
        for index, step in enumerate(steps, 1):
            lines.append(f"  {index}. {step}")
        if tools:
            lines.extend(["", "预计使用工具：", "  " + "、".join(str(tool) for tool in tools)])
        if plan.get("notes"):
            lines.extend(["", f"备注：{plan['notes']}"])
        lines.extend(["", "计划先展示给用户；随后由 AI 按证据推进，必要时会调整。"])
        self.plan_view.setPlainText("\n".join(lines))
        self.right_tabs.setCurrentWidget(self.plan_view)
        if self.current_record is not None:
            self.current_record["plan"] = plan
        self._set_status("AI 已生成执行计划；开始按计划执行。")
        self._append_stream("narration", "\n📋 执行计划已生成（见“执行计划”标签）。\n")

    def _on_attempt_started(self, attempt: int) -> None:
        self._planner_calls += 1
        if self._planner_calls > 1:
            separator = "\n\n──────── 新的规划请求 ────────\n"
            self._append_stream("narration", separator)
            self._append_stream("reasoning", separator)
            self._append_stream("content", separator)

    def _on_reasoning(self, text: str) -> None:
        self._append_stream("reasoning", text)

    def _on_narration(self, text: str) -> None:
        self._append_stream("narration", text)

    def _on_content(self, text: str) -> None:
        self._append_stream("content", text)

    def _on_warning(self, text: str) -> None:
        self.warnings.append(text)
        self._set_status(f"警告：{text}")
        self._append_stream("narration", f"\n⚠ {text}\n")

    def _on_usage(self, usage: dict[str, Any]) -> None:
        total = usage.get("total_tokens")
        if total:
            self.status_model.setToolTip(f"本次会话累计 tokens：{total}")

    # ------------------------------------------------------------ interactions
    def _on_user_question(self, payload: dict[str, Any]) -> None:
        request_id = str(payload.get("request_id") or "")
        question = payload.get("question") or {}
        self._append_stream("narration", f"\n\n💬 AI 提问：{question.get('prompt') or ''}\n")
        dialog = AskUserDialog(question, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            answer = dialog.answer()
        else:
            answer = "（用户跳过了问题）"
        self._append_stream("narration", f"👤 用户回答：{answer}\n")
        if self._worker is not None:
            self._worker.answer_question(request_id, answer)

    def _on_tool_confirmation(self, payload: dict[str, Any]) -> None:
        request_id = str(payload.get("request_id") or "")
        dialog = ToolConfirmDialog(
            str(payload.get("tool") or ""),
            payload.get("arguments") or {},
            str(payload.get("reason") or ""),
            self,
        )
        approved = dialog.exec() == QDialog.DialogCode.Accepted and bool(
            dialog.confirm_check.isChecked()
        )
        if self._worker is not None:
            self._worker.resolve_tool_confirmation(request_id, approved)

    def _on_actions_proposed(self, payload: dict[str, Any]) -> None:
        request_id = str(payload.get("request_id") or "")
        proposal = payload.get("proposal") or {}
        dialog = ActionConfirmDialog(proposal, self)
        approved = dialog.exec() == QDialog.DialogCode.Accepted
        if self._worker is not None:
            self._worker.resolve_actions(request_id, approved)

    def _on_evidence(self, evidence: dict[str, Any]) -> None:
        evidence_id = str(evidence.get("id") or "")
        if not evidence_id or evidence_id in self._evidence_by_id:
            return
        self._evidence_by_id[evidence_id] = evidence
        self._evidence_total = len(self._evidence_by_id)
        item = QTreeWidgetItem([
            evidence_id,
            str(evidence.get("tool") or ""),
            str(evidence.get("summary") or ""),
        ])
        item.setData(0, Qt.ItemDataRole.UserRole, evidence)
        tooltip = str(evidence.get("summary") or "")
        if not evidence.get("ok"):
            tooltip += f"\n失败：{evidence.get('error') or '未知错误'}"
            for column in range(3):
                item.setForeground(column, QColor("#a23b00"))
        if evidence.get("native_command"):
            tooltip += f"\n\n原生查询：{evidence['native_command']}"
        for column in range(3):
            item.setToolTip(column, tooltip)
        self.evidence_tree.insertTopLevelItem(0, item)
        self.evidence_group.setTitle(tr_text(f"证据（{self._evidence_total}）"))
        self.overview_evidence.setText(f"{self._evidence_total} {tr_text('条')}")
        self.status_evidence.setText(tr_text(f"证据 {self._evidence_total}"))
        self._refresh_overview_from_evidence()
        if is_english():
            self.stream_hint.setText(f"Collected {self._evidence_total} evidence item(s)…")
        else:
            self.stream_hint.setText(f"已采集 {self._evidence_total} 条证据…")

    def _refresh_overview_from_evidence(self) -> None:
        system_evidence = next(
            (item for item in self._evidence_by_id.values() if item.get("tool") == "get_system_info"),
            None,
        )
        if system_evidence and system_evidence.get("ok"):
            data = system_evidence.get("data") or {}
            pending = "是" if data.get("pending_reboot") else "否"
            self.overview_system.setText(
                f"{data.get('os_caption') or 'Windows'} · Build {data.get('build_number') or '?'} · "
                f"{data.get('architecture') or '?'}\n待重启：{pending} · "
                f"系统盘剩余 {data.get('system_drive_free_gb') or '?'} GB"
            )
        file_evidence = next(
            (item for item in self._evidence_by_id.values() if item.get("tool") == "inspect_file"),
            None,
        )
        if file_evidence and file_evidence.get("ok"):
            data = file_evidence.get("data") or {}
            self.overview_file.setText(
                f"{data.get('signature_fact') or data.get('signature_status') or '—'}\n"
                f"{data.get('path_category_label') or ''}"
            )
            hash_value = str(data.get("sha256") or "—")
            self.overview_hash.setText(hash_value)
            self.overview_hash.setToolTip(hash_value)
        elif file_evidence and not file_evidence.get("ok"):
            self.overview_file.setText(f"检查失败：{file_evidence.get('error') or '未知错误'}")

    def _on_step(self, step: dict[str, Any]) -> None:
        ok = bool(step.get("ok"))
        item = QTreeWidgetItem([
            str(step.get("sequence") or ""),
            str(step.get("tool") or ""),
            "、".join(step.get("basis") or []),
            str(step.get("evidence_id") or ""),
            "成功" if ok else "失败",
        ])
        tooltip = (
            f"说明：{step.get('narration') or ''}\n"
            f"原因：{step.get('why') or ''}\n"
            f"参数：{json.dumps(step.get('arguments') or {}, ensure_ascii=False)}\n"
            f"耗时：{step.get('duration_ms') or 0} ms"
        )
        if step.get("error"):
            tooltip += f"\n错误：{step['error']}"
        for column in range(5):
            item.setToolTip(column, tooltip)
        if not ok:
            item.setForeground(4, QColor("#a23b00"))
        item.setData(0, Qt.ItemDataRole.UserRole, step)
        self.audit_tree.insertTopLevelItem(0, item)

    def _on_final_report(self, report: dict[str, Any]) -> None:
        self.final_report = report
        self.conclusion_view.setHtml(render_report_html(report, self._record_view()))
        self.right_tabs.setCurrentWidget(self.conclusion_view)
        self.stream_tabs.setCurrentIndex(0)
        self._set_status("结构化诊断结论已生成。")
        self._update_status_actions()

    def _on_completed(self, record: dict[str, Any]) -> None:
        self.current_record = record
        self.record_path = reports_dir() / f"{record.get('record_id')}.json"
        report = record.get("report")
        if isinstance(report, dict):
            self.final_report = report
            self.conclusion_view.setHtml(render_report_html(report, self._record_view()))
            self.right_tabs.setCurrentWidget(self.conclusion_view)
        else:
            self.conclusion_view.setHtml(
                self._placeholder_report_html(
                    f"诊断状态：{_STATUS_LABELS.get(str(record.get('status')), record.get('status'))}。"
                    + (f"<br>错误：{record.get('errors')[-1]}" if record.get("errors") else "")
                )
            )
        status = str(record.get("status") or "")
        label = _STATUS_LABELS.get(status, status)
        steps = len(record.get("tool_steps") or [])
        evidence = len(record.get("evidence") or [])
        actions = len(record.get("actions") or [])
        elapsed = float(record.get("elapsed_seconds") or 0)
        self.overview_status.setText(label)
        self._set_status(
            f"分析{label}：{evidence} 条证据，{steps} 个步骤，{actions} 个修复动作，"
            f"用时 {int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}"
            + ("；已生成结论。" if self.final_report else "。")
        )
        self.stream_hint.setText(
            f"分析{label} · 证据 {evidence} 条 · 步骤 {steps} 个 · "
            f"自动审计记录：{self.record_path.name if self.record_path else '未保存'}"
        )
        if self.warnings:
            self._append_stream("narration", "\n⚠ 警告：\n" + "\n".join(f"- {item}" for item in self.warnings) + "\n")
        self._update_status_actions()

    def _on_failed(self, message: str) -> None:
        self._elapsed_timer.stop()
        self._set_status(f"分析失败：{message}")
        self.stream_hint.setText(f"分析失败：{message}")
        self.conclusion_view.setHtml(self._placeholder_report_html(f"分析失败：{message}"))
        self.right_tabs.setCurrentWidget(self.conclusion_view)
        QMessageBox.warning(self, "分析失败", message)

    def _on_thread_finished(self) -> None:
        self._elapsed_timer.stop()
        if self.current_record:
            elapsed = float(self.current_record.get("elapsed_seconds") or 0)
            text = f"用时 {int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}"
            self.elapsed_label.setText(text)
            self.status_elapsed.setText(text)
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
        self._set_busy(False)
        self._refresh_broker_status()
        if self.current_record and self.current_record.get("report"):
            self._update_status_actions()

    # ---------------------------------------------------------------- details
    def _record_view(self) -> types.SimpleNamespace:
        record = self.current_record or {}
        return types.SimpleNamespace(
            target=record.get("target") or (self.current_target.display if self.current_target else None),
            issue=record.get("issue") or self.issue_edit.text().strip(),
            mode=record.get("mode") or "ai",
            model=record.get("model") or self.config.model,
            status=record.get("status") or "running",
            started_at=record.get("started_at") or datetime.now().astimezone().isoformat(timespec="seconds"),
            finished_at=record.get("finished_at"),
            record_id=record.get("record_id") or "preview",
            generator=record.get("generator") or "Windows Helper Plus / v2",
            evidence=record.get("evidence") or [],
            tool_steps=record.get("tool_steps") or [],
            actions=record.get("actions") or [],
            interactions=record.get("interactions") or [],
            planner=record.get("planner") or [],
            plan=record.get("plan"),
            target_kind=record.get("target_kind") or (self.current_target.kind if self.current_target else "system"),
            elapsed_seconds=record.get("elapsed_seconds") or 0,
        )

    def _show_evidence_details(self, item: QTreeWidgetItem, _column: int) -> None:
        evidence = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(evidence, dict):
            EvidenceDetailDialog(evidence, self).exec()

    def _evidence_context_menu(self, position) -> None:
        item = self.evidence_tree.itemAt(position)
        if item is None:
            return
        evidence = item.data(0, Qt.ItemDataRole.UserRole) or {}
        menu = QMenu(self)
        action_copy_id = menu.addAction("复制证据 ID")
        action_copy_summary = menu.addAction("复制摘要")
        action_details = menu.addAction("查看完整证据…")
        chosen = menu.exec(self.evidence_tree.viewport().mapToGlobal(position))
        if chosen == action_copy_id:
            QApplication.clipboard().setText(str(evidence.get("id") or ""))
        elif chosen == action_copy_summary:
            QApplication.clipboard().setText(str(evidence.get("summary") or ""))
        elif chosen == action_details:
            EvidenceDetailDialog(evidence, self).exec()

    def _copy_step_json(self, item: QTreeWidgetItem, _column: int) -> None:
        step = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(step, dict):
            QApplication.clipboard().setText(json.dumps(step, ensure_ascii=False, indent=2))
            self._set_status("已复制工具步骤 JSON 到剪贴板。")

    # ------------------------------------------------------------------ export
    def _on_export(self) -> None:
        if not self.current_record or not self.final_report:
            QMessageBox.information(self, "无可导出内容", "请先完成一次 AI 分析。")
            return
        export_dir = default_export_dir()
        default_name = default_report_name(self._record_view(), ".html")
        filters = "HTML 报告 (*.html);;JSON 诊断记录 (*.json)"
        path_text, selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出诊断报告",
            str(export_dir / default_name),
            filters,
        )
        if not path_text:
            return
        path = Path(path_text)
        if path.suffix.lower() not in {".html", ".htm", ".json"}:
            path = path.with_suffix(".json" if "JSON" in selected_filter else ".html")
        try:
            record_view = self._record_view()
            if path.suffix.lower() == ".json":
                path.write_text(
                    json.dumps(self.current_record, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            else:
                path.write_text(
                    render_export_html(record_view, self.final_report),
                    encoding="utf-8",
                )
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"无法写入报告：{exc}")
            return
        QMessageBox.information(self, "导出完成", f"报告已保存到：\n{path}")
        self._set_status(f"报告已导出：{path}")

    # ------------------------------------------------------------------ misc
    def _open_local_doc(self, name: str) -> None:
        path = Path(__file__).resolve().parents[2] / name
        if not path.exists():
            QMessageBox.information(self, "文档不存在", f"未找到 {name}。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_config_folder(self) -> None:
        path = app_data_dir()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _show_self_check(self) -> None:
        SelfCheckDialog(self.config, self).exec()

    def _startup_self_check(self) -> None:
        """启动时只对不受支持的系统弹警告；其他环境问题在“系统自检”中查看。"""
        try:
            from ..system_compat import get_os_info

            info = get_os_info()
            if not info.supported:
                QMessageBox.warning(
                    self,
                    "系统不受支持",
                    f"{info.windows_label}（build {info.build}，{info.architecture}）\n"
                    f"{info.reason}\n\nWindows Helper Plus 仅适配 Windows 10/11 64 位。",
                )
        except Exception:  # noqa: BLE001
            pass

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 Windows Helper Plus",
            f"<b>Windows Helper Plus</b> v{__version__}<br>"
            "AI 驱动的 Windows 全对象诊断、问题定位与受控修复<br><br>"
            f"Planner：<b>{self.config.model}</b>（思考强度 {self.config.reasoning_effort.upper()}）<br>"
            "对象：文件 / 文件夹 / 驱动器 / PnP 设备 / 打印机<br>"
            "能力：Windows 原生取证、DeepSeek 联网检索、交互式提问、用户确认后的修复动作<br><br>"
            "安全提示：修复动作逐条确认，需要管理员权限时通过 UAC 执行；"
            "应用层不限制步数/token，可用“停止”随时中断。"
            "<br><br>架构说明见项目内《架构设计.md》。",
        )

    def _placeholder_report_html(self, text: str = "尚未生成分析结论。") -> str:
        return (
            "<div style=\"font-family:'Segoe UI','Microsoft YaHei UI',sans-serif;padding:18px;color:#333;\">"
            f"<h2 style='color:#0b3d6e;'>{html.escape(tr_text('分析结论'))}</h2>"
            f"<p>{html.escape(tr_text(text))}</p>"
            "</div>"
        )

    # -------------------------------------------------------------- drag/drop
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._busy:
            return
        mime = event.mimeData()
        if mime.hasUrls() and len(mime.urls()) == 1:
            url = mime.urls()[0]
            if url.isLocalFile():
                event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        if self._busy:
            return
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            path = Path(urls[0].toLocalFile())
            self._set_path_target(str(path))
            self._set_status(f"已拖入目标：{path.name}。点击“开始分析”。")
            event.acceptProposedAction()

    # ------------------------------------------------------------------ close
    def closeEvent(self, event) -> None:  # noqa: N802
        if self._busy:
            answer = QMessageBox.question(
                self,
                "分析进行中",
                "分析/修复仍在进行。是否停止并退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if self._worker:
                self._worker.cancel_event.set()
            if self._thread is not None and not self._thread.wait(6000):
                QMessageBox.warning(
                    self,
                    "仍在停止中",
                    "后台请求还没有结束（可能正在等待网络超时）。请稍候再关闭窗口。",
                )
                event.ignore()
                return
        event.accept()
