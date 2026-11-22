"""阶段 2/3 交互对话框：用户提问、工具确认、动作确认。"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..i18n import is_english, translate_widget_tree
from ..system_compat import collect_self_check, format_self_check_text


class SelfCheckDialog(QDialog):
    def __init__(self, config=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Windows Helper Plus — 系统自检")
        self.setMinimumSize(680, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        result = collect_self_check(config)
        browser = QTextBrowser()
        browser.setObjectName("ConclusionPane")
        browser.setPlainText(format_self_check_text(result))
        layout.addWidget(browser, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.result_data = result
        if is_english():
            translate_widget_tree(self)


class AskUserDialog(QDialog):
    def __init__(self, question: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Helper Plus — 需要补充信息")
        self.setMinimumWidth(620)
        self._question = question
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)
        prompt = QLabel(str(question.get("prompt") or ""))
        prompt.setWordWrap(True)
        prompt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(prompt)
        options = [str(item) for item in (question.get("options") or [])]
        other_option = "其他（请自行补充）"
        if not any("其他" in option for option in options):
            options.append(other_option)
        self.combo: QComboBox | None = None
        self.text: QPlainTextEdit | None = None
        self.other_option = next((item for item in options if "其他" in item), other_option)
        if options:
            form = QFormLayout()
            self.combo = QComboBox()
            for option in options:
                self.combo.addItem(option)
            form.addRow("可选回答：", self.combo)
            layout.addLayout(form)
        # 用户要求始终可以补充“其他情况/未出现的情况”。
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("补充未列出的情况、其他现象或你的判断（选择“其他”时请在此填写）")
        self.text.setMaximumBlockCount(20)
        self.text.setMinimumHeight(100)
        layout.addWidget(self.text)

        def _sync_other_hint() -> None:
            if self.combo is not None and self.combo.currentText() == self.other_option:
                self.text.setFocus()
                self.text.setPlaceholderText("请填写其他情况/未出现的情况…")
            else:
                self.text.setPlaceholderText("补充未列出的情况、其他现象或你的判断")

        if self.combo is not None:
            self.combo.currentTextChanged.connect(lambda _text: _sync_other_hint())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("提交回答")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("跳过")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if is_english():
            translate_widget_tree(self)

    def answer(self) -> str:
        parts: list[str] = []
        if self.combo is not None:
            chosen = self.combo.currentText().strip()
            if chosen and chosen != self.other_option:
                parts.append(chosen)
        if self.text is not None:
            text = self.text.toPlainText().strip()
            if text:
                parts.append(("其他：" if self.combo is not None and self.combo.currentText() == self.other_option else "") + text)
        return "\n".join(part for part in parts if part) or "（用户未提供信息）"


class ToolConfirmDialog(QDialog):
    def __init__(
        self,
        tool: str,
        arguments: dict[str, Any],
        reason: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认执行诊断工具")
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)
        title = QLabel(f"即将执行：{tool}")
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        title.setFont(font)
        layout.addWidget(title)
        if reason:
            reason_label = QLabel(reason)
            reason_label.setWordWrap(True)
            reason_label.setStyleSheet("color:#8a4b00;")
            layout.addWidget(reason_label)
        args = QPlainTextEdit()
        args.setReadOnly(True)
        args.setFont(QFont("Consolas", 9))
        args.setPlainText(json.dumps(arguments or {}, ensure_ascii=False, indent=2))
        args.setMaximumHeight(180)
        layout.addWidget(args)
        self.confirm_check = QCheckBox("我已了解该操作及其耗时/权限影响，确认执行")
        layout.addWidget(self.confirm_check)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        buttons.button(QDialogButtonBox.StandardButton.Yes).setText("执行")
        buttons.button(QDialogButtonBox.StandardButton.Yes).setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.No).setText("取消")
        self.confirm_check.toggled.connect(buttons.button(QDialogButtonBox.StandardButton.Yes).setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if is_english():
            translate_widget_tree(self)


class ActionConfirmDialog(QDialog):
    def __init__(self, proposal: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认修复动作 — 需要审批")
        self.setMinimumSize(860, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        narration = QLabel(str(proposal.get("narration") or "模型提出了以下修复动作。"))
        narration.setWordWrap(True)
        layout.addWidget(narration)
        admin_count = sum(1 for item in proposal.get("actions") or [] if item.get("requires_admin"))
        warning = QLabel(
            "以下命令将被真实执行。请逐条核对；"
            + (f"其中 {admin_count} 条需要管理员权限，执行时会触发 UAC。" if admin_count else "本次动作不需要管理员权限。")
            + "执行结果会写入诊断记录与审计日志。"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color:#8a4b00;font-weight:600;")
        layout.addWidget(warning)

        from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

        tree = QTreeWidget()
        tree.setColumnCount(4)
        tree.setHeaderLabels(["动作", "风险", "管理员", "命令"])
        tree.setRootIsDecorated(False)
        tree.setColumnWidth(0, 240)
        tree.setColumnWidth(1, 70)
        tree.setColumnWidth(2, 70)
        tree.setColumnWidth(3, 420)
        for index, action in enumerate(proposal.get("actions") or [], 1):
            item = QTreeWidgetItem(
                [
                    f"{index}. {action.get('title') or ''}",
                    str(action.get("risk") or "medium"),
                    "是" if action.get("requires_admin") else "否",
                    str(action.get("command") or ""),
                ]
            )
            tree.addTopLevelItem(item)
        tree.setMinimumHeight(200)
        layout.addWidget(tree, 1)

        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setFont(QFont("Consolas", 9))
        detail.setMaximumHeight(150)
        lines: list[str] = []
        for index, action in enumerate(proposal.get("actions") or [], 1):
            lines.append(f"[{index}] {action.get('title')}")
            lines.append(f"命令：{action.get('command')}")
            lines.append(
                f"Shell：{action.get('shell')} | 风险：{action.get('risk')} | "
                f"管理员：{'是（执行时触发 UAC）' if action.get('requires_admin') else '否'}"
            )
            lines.append(f"原因：{action.get('rationale')}")
            backup_paths = action.get("backup_paths") or []
            if backup_paths:
                lines.append("自动备份：" + "；".join(backup_paths))
            lines.append(f"回滚说明：{action.get('rollback')}")
            if action.get("rollback_command"):
                lines.append(
                    f"回滚命令（{action.get('rollback_shell') or 'cmd'}）：{action.get('rollback_command')}"
                )
            lines.append("")
        detail.setPlainText("\n".join(lines))
        layout.addWidget(detail)

        self.confirm_check = QCheckBox(
            "我已阅读并理解所有命令、自动备份路径、风险与回滚说明，并同意执行上述动作"
        )
        layout.addWidget(self.confirm_check)
        self.typed_confirm = None
        if any(str(action.get("risk")) == "high" for action in proposal.get("actions") or []):
            typed_row = QFormLayout()
            from PySide6.QtWidgets import QLineEdit

            self.typed_confirm = QLineEdit()
            self.typed_confirm.setPlaceholderText("高风险动作：输入 RUN 以确认")
            typed_row.addRow("确认码：", self.typed_confirm)
            layout.addLayout(typed_row)
            self.typed_confirm.textChanged.connect(self._update_ok)
        self.confirm_check.toggled.connect(self._update_ok)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Yes)
        self.ok_button.setText("执行上述动作")
        self.ok_button.setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.No).setText("拒绝")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if is_english():
            translate_widget_tree(self)

    def _update_ok(self) -> None:
        ok = self.confirm_check.isChecked()
        if self.typed_confirm is not None:
            ok = ok and self.typed_confirm.text().strip() == "RUN"
        self.ok_button.setEnabled(ok)
