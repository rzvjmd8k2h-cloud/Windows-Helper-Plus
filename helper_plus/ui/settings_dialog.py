"""设置对话框：DeepSeek 模型、推理强度、API Key（DPAPI）与轮次限制。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..autostart import autostart_enabled, install_autostart, remove_autostart
from ..i18n import LANGUAGES, is_english, translate_widget_tree, tr_text
from ..config import (
    AppConfig,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    MODEL_OPTIONS,
    REASONING_EFFORTS,
    env_api_key,
)
from ..secrets import (
    api_key_source,
    clear_api_key,
    has_stored_api_key,
    save_api_key,
)


_EFFORT_LABELS = {
    "low": "Low — 更快",
    "high": "High — 均衡",
    "max": "Max — 最强推理（推荐）",
}


class SettingsDialog(QDialog):
    def __init__(
        self,
        config: AppConfig,
        session_key: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._original = config
        self._session_key = session_key
        self._effective_session_key = session_key
        self._clear_key_requested = False
        self._saved = False
        self.setWindowTitle("设置 — Windows Helper Plus")
        self.setModal(True)
        self.setMinimumWidth(680)
        self.resize(760, 620)
        self._build_ui()
        self._load(config, session_key)
        if is_english():
            translate_widget_tree(self)

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        title = QLabel("设置")
        title_font = QFont(self.font())
        title_font.setPointSize(14)
        title_font.setWeight(QFont.Weight.DemiBold)
        title.setFont(title_font)
        root.addWidget(title)
        intro = QLabel(
            "Planner 默认使用 DeepSeek deepseek-flash（V4.1 Flash-1017），"
            "开启思考模式并固定 Max 推理强度。分析时会把证据发送给 DeepSeek API；"
            "需要联网检索时，会通过 DeepSeek Search/Fetch 接口获取网页。"
        )
        intro.setWordWrap(True)
        intro.setObjectName("Hint")
        root.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 2, 6, 2)
        content_layout.setSpacing(8)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        ui_group = QGroupBox("界面")
        ui_form = QFormLayout(ui_group)
        ui_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        ui_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.language_combo = QComboBox()
        for code, label in LANGUAGES.items():
            self.language_combo.addItem(label, code)
        ui_form.addRow("界面语言：", self.language_combo)
        language_note = QLabel(
            "切换后立即刷新当前窗口；AI 输出与已生成报告保持原语言。"
        )
        language_note.setWordWrap(True)
        language_note.setObjectName("Hint")
        ui_form.addRow("", language_note)
        content_layout.addWidget(ui_group)

        model_group = QGroupBox("推理模型")
        form = QFormLayout(model_group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.model_combo = QComboBox()
        for value, label in MODEL_OPTIONS:
            self.model_combo.addItem(label, value)
        self.model_combo.setMinimumContentsLength(30)
        form.addRow("模型：", self.model_combo)
        self.effort_combo = QComboBox()
        for effort in REASONING_EFFORTS:
            self.effort_combo.addItem(_EFFORT_LABELS.get(effort, effort), effort)
        form.addRow("思考强度：", self.effort_combo)
        self.model_note = QLabel("模型名使用 DeepSeek 官方别名，指向当前最新 V4.1 Flash 模型。")
        self.model_note.setWordWrap(True)
        self.model_note.setObjectName("Hint")
        form.addRow("", self.model_note)
        content_layout.addWidget(model_group)

        api_group = QGroupBox("DeepSeek API")
        api_form = QFormLayout(api_group)
        api_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        api_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText(DEFAULT_BASE_URL)
        api_form.addRow("Base URL：", self.base_url_edit)

        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-…（留空表示保留当前配置）")
        key_layout.addWidget(self.key_edit, 1)
        self.show_key_check = QCheckBox("显示")
        self.show_key_check.toggled.connect(self._toggle_key_visibility)
        key_layout.addWidget(self.show_key_check)
        clear_button = QPushButton("清除已保存")
        clear_button.setToolTip("删除 Windows DPAPI 加密保存的 API Key")
        clear_button.clicked.connect(self._clear_saved_key)
        key_layout.addWidget(clear_button)
        api_form.addRow("API Key：", key_row)

        self.remember_check = QCheckBox("记住 API Key（使用 Windows DPAPI 加密保存，仅当前 Windows 用户可解密）")
        self.remember_check.setChecked(True)
        api_form.addRow("", self.remember_check)

        self.key_status_label = QLabel()
        self.key_status_label.setWordWrap(True)
        self.key_status_label.setObjectName("Hint")
        api_form.addRow("当前来源：", self.key_status_label)
        link = QLabel(
            '<a href="https://platform.deepseek.com/api_keys">在 DeepSeek 开放平台获取 API Key</a>'
        )
        link.setOpenExternalLinks(True)
        link.setObjectName("Hint")
        api_form.addRow("", link)
        content_layout.addWidget(api_group)

        advanced_group = QGroupBox("上下文与执行")
        advanced = QFormLayout(advanced_group)
        advanced.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        advanced.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(30, 900)
        self.timeout_spin.setSuffix(" 秒")
        advanced.addRow("API 超时：", self.timeout_spin)
        self.context_spin = QSpinBox()
        self.context_spin.setRange(50_000, 5_000_000)
        self.context_spin.setSingleStep(50_000)
        self.context_spin.setSuffix(" 字符")
        self.context_spin.setToolTip("超长对话时，证据 data 按此预算压缩；ID/摘要/日志始终保留。")
        advanced.addRow("上下文压缩预算：", self.context_spin)
        self.action_timeout_spin = QSpinBox()
        self.action_timeout_spin.setRange(60, 86400)
        self.action_timeout_spin.setSuffix(" 秒")
        advanced.addRow("动作超时：", self.action_timeout_spin)
        self.unlimited_label = QLabel(
            "应用层不限制步数和 token：不发送 max_tokens，不设最大轮次。"
            "上下文由压缩模块控制；可随时点击“停止”。"
        )
        self.unlimited_label.setWordWrap(True)
        self.unlimited_label.setObjectName("Hint")
        advanced.addRow("限制：", self.unlimited_label)
        safety_note = QLabel(
            "所有分析由 AI 完成。写入/删除/驱动/服务/注册表等修复只能通过动作提案执行，"
            "并且每一条都必须在 UI 中由用户明确确认；需要管理员时触发 UAC。"
        )
        safety_note.setWordWrap(True)
        safety_note.setObjectName("Hint")
        advanced.addRow("", safety_note)
        content_layout.addWidget(advanced_group)

        local_group = QGroupBox("本地 Qwen 上下文官")
        local_form = QFormLayout(local_group)
        local_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        local_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.local_enabled_check = QCheckBox("启用本地 Qwen 上下文整理/病历/日志翻译")
        self.local_enabled_check.setToolTip("CPU 推理，首次加载约 15–20 秒；不产生最终诊断，只做主模型的参考上下文。")
        local_form.addRow("启用：", self.local_enabled_check)
        self.trusted_mode_check = QCheckBox("最高权限/信任模式（调用默认放行；中高风险动作仍确认/UAC）")
        self.trusted_mode_check.setToolTip("关闭后切换 strict：所有动作都需要用户确认；路径只读工具仍按最高权限模式放行。")
        local_form.addRow("执行权限：", self.trusted_mode_check)
        self.autostart_check = QCheckBox("Windows 登录时自启动本地 Qwen 预检/预热")
        self.autostart_check.setToolTip("在当前用户 Startup 文件夹创建/删除快捷方式；不需要管理员。")
        local_form.addRow("开机自启动：", self.autostart_check)
        self.local_info_label = QLabel()
        self.local_info_label.setWordWrap(True)
        self.local_info_label.setObjectName("Hint")
        local_form.addRow("模型：", self.local_info_label)
        content_layout.addWidget(local_group)
        content_layout.addStretch(1)

        footer = QHBoxLayout()
        reset_button = QPushButton("恢复推荐值")
        reset_button.clicked.connect(self._reset_defaults)
        footer.addWidget(reset_button)
        self_check_button = QPushButton("系统自检…")
        self_check_button.clicked.connect(self._run_self_check)
        footer.addWidget(self_check_button)
        footer.addStretch(1)
        version_label = QLabel(f"v{__version__}")
        version_label.setObjectName("Hint")
        footer.addWidget(version_label)
        root.addLayout(footer)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        save_button.setText("保存")
        save_button.setDefault(True)
        save_button.setAutoDefault(True)
        cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_button.setText("取消")
        cancel_button.setAutoDefault(False)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

    # ------------------------------------------------------------------- load
    def _load(self, config: AppConfig, session_key: str) -> None:
        language_index = self.language_combo.findData(getattr(config, "ui_language", "zh"))
        self.language_combo.setCurrentIndex(max(0, language_index))
        model_index = self.model_combo.findData(config.model)
        if model_index < 0:
            self.model_combo.addItem(f"{config.model}（自定义）", config.model)
            model_index = self.model_combo.count() - 1
        self.model_combo.setCurrentIndex(model_index)
        effort_index = self.effort_combo.findData(config.reasoning_effort)
        self.effort_combo.setCurrentIndex(max(0, effort_index))
        self.base_url_edit.setText(config.base_url)
        self.remember_check.setChecked(config.remember_api_key)
        self.timeout_spin.setValue(config.timeout_seconds)
        self.context_spin.setValue(config.context_budget_chars)
        self.action_timeout_spin.setValue(config.action_timeout_seconds)
        self.local_enabled_check.setChecked(bool(getattr(config, "local_model_enabled", True)))
        self.trusted_mode_check.setChecked(
            str(getattr(config, "permission_mode", "trusted")) == "trusted"
        )
        self.autostart_check.setChecked(autostart_enabled())
        if is_english():
            self.local_info_label.setText(
                f"Qwen3-1.7B-Q8_0.gguf\n{getattr(config, 'local_model_path', '')}\n"
                f"llama-server port {getattr(config, 'local_model_port', 8124)}, "
                f"context {getattr(config, 'local_model_context', 8192)}; logs in Log/."
            )
        else:
            self.local_info_label.setText(
                f"Qwen3-1.7B-Q8_0.gguf\n{getattr(config, 'local_model_path', '')}\n"
                f"llama-server 端口 {getattr(config, 'local_model_port', 8124)}，"
                f"上下文 {getattr(config, 'local_model_context', 8192)}；日志输出到 Log/。"
            )
        if env_api_key():
            self.key_edit.setPlaceholderText("环境变量已设置；输入新 Key 保存后将优先使用应用内 Key")
        elif has_stored_api_key():
            self.key_edit.setPlaceholderText("已保存（DPAPI 加密；留空保持不变）")
        self.key_edit.returnPressed.connect(self._save)
        self._refresh_key_status()

    def _refresh_key_status(self) -> None:
        source = api_key_source(self._effective_session_key)
        self.key_status_label.setText(tr_text(source))
        if env_api_key() and has_stored_api_key():
            self.key_status_label.setText(
                tr_text("应用内 DPAPI Key 已保存（优先于环境变量）")
            )

    # ---------------------------------------------------------------- actions
    def _toggle_key_visibility(self, visible: bool) -> None:
        self.key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )

    def _clear_saved_key(self) -> None:
        clear_api_key()
        self._clear_key_requested = True
        self._effective_session_key = ""
        self.key_edit.clear()
        self.key_edit.setPlaceholderText("未配置 API Key")
        self.remember_check.setChecked(True)
        self._refresh_key_status()
        self.key_status_label.setText("已清除本地保存的 API Key（环境变量不受影响）")

    def _reset_defaults(self) -> None:
        model_index = self.model_combo.findData(DEFAULT_MODEL)
        self.model_combo.setCurrentIndex(max(0, model_index))
        effort_index = self.effort_combo.findData(DEFAULT_REASONING_EFFORT)
        self.effort_combo.setCurrentIndex(max(0, effort_index))
        self.base_url_edit.setText(DEFAULT_BASE_URL)
        self.timeout_spin.setValue(180)
        self.context_spin.setValue(700_000)
        self.action_timeout_spin.setValue(3600)
        self.local_enabled_check.setChecked(True)
        self.trusted_mode_check.setChecked(True)
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData("zh")))
        self.autostart_check.setChecked(autostart_enabled())

    def _run_self_check(self) -> None:
        from ..system_compat import collect_self_check, format_self_check_text

        result = collect_self_check(self._original)
        QMessageBox.information(self, "系统自检", format_self_check_text(result))

    def _save(self) -> None:
        base_url = self.base_url_edit.text().strip().rstrip("/") or DEFAULT_BASE_URL
        if not (base_url.startswith("https://") or base_url.startswith("http://")):
            QMessageBox.warning(self, "设置无效", "Base URL 必须以 http:// 或 https:// 开头。")
            return
        model = str(self.model_combo.currentData() or self.model_combo.currentText()).strip()
        effort = str(self.effort_combo.currentData() or DEFAULT_REASONING_EFFORT)
        ui_language = str(self.language_combo.currentData() or "zh")
        typed_key = self.key_edit.text().strip()
        remember = self.remember_check.isChecked()

        if typed_key and remember:
            try:
                save_api_key(typed_key)
                # 验证确实能被读取；避免“提示保存成功但实际用不到”的情况。
                from ..secrets import load_stored_api_key

                if load_stored_api_key() != typed_key:
                    raise OSError("DPAPI 回读校验失败。")
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    "API Key 保存失败",
                    "无法使用 Windows DPAPI 保存 API Key：\n"
                    f"{exc}\n\n密钥仍可在本次会话中使用，但不会写盘。",
                )
                remember = False
        if typed_key:
            self._effective_session_key = typed_key
        elif self._clear_key_requested:
            self._effective_session_key = ""
        want_autostart = self.autostart_check.isChecked()
        if want_autostart != autostart_enabled():
            ok, detail = install_autostart() if want_autostart else remove_autostart()
            if not ok:
                QMessageBox.warning(
                    self,
                    "开机自启动设置失败",
                    f"无法{'安装' if want_autostart else '移除'}本地 Qwen 自启动快捷方式：\n{detail}",
                )
                return
        config = self._original.copy(
            base_url=base_url,
            model=model,
            reasoning_effort=effort,
            max_rounds=0,
            max_tokens=0,
            timeout_seconds=self.timeout_spin.value(),
            context_budget_chars=self.context_spin.value(),
            action_timeout_seconds=self.action_timeout_spin.value(),
            local_model_enabled=self.local_enabled_check.isChecked(),
            permission_mode="trusted" if self.trusted_mode_check.isChecked() else "strict",
            ui_language=ui_language,
            remember_api_key=remember,
        )
        try:
            config.save()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "保存失败", f"无法保存设置：{exc}")
            return
        self._config = config
        self._saved = True
        self._save_succeeded = True
        self.accept()

    # ------------------------------------------------------------------ props
    @property
    def config(self) -> AppConfig:
        return getattr(self, "_config", self._original)

    @property
    def saved(self) -> bool:
        return self._saved

    @property
    def effective_session_key(self) -> str:
        return self._effective_session_key
