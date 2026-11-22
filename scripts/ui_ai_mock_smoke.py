"""开发期：用本地假 DeepSeek SSE 服务跑通「AI 诊断」完整 UI 流程。

不访问真实 DeepSeek，不需要 API Key。输出两张截图到 .runtime。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".runtime"
OUT.mkdir(exist_ok=True)
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
os.environ["HELPER_PLUS_DATA_DIR"] = str(OUT / "ui-ai-data")
os.environ["DEEPSEEK_API_KEY"] = "sk-mock-ui-test"
os.environ["HELPER_PLUS_MODEL"] = "deepseek-flash"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
sys.path.insert(0, str(ROOT))

from helper_plus.config import AppConfig  # noqa: E402
from helper_plus.ui.main_window import MainWindow  # noqa: E402
from helper_plus.ui.theme import create_application  # noqa: E402


PLAN = json.dumps(
    {
        "narration": "基线证据 [E1][E2] 已就绪，先向用户展示执行计划。",
        "action": "plan",
        "basis": ["E1", "E2"],
        "plan": {
            "goal": "核对 kernel32.dll 的签名、版本、相关系统日志与 SYSTEM 特权证据，判断是否存在异常。",
            "confirmed_user_goal": "主模型确认：核对一个系统 DLL 的签名与相关系统日志，判断是否存在异常。",
            "steps": [
                "确认目标文件签名、哈希与版本",
                "检查 System/Application 错误与 WER/可靠性记录",
                "关联 SYSTEM 特权代理的独立校验结果",
                "如果证据不足，再定向查询驱动/更新/联网资料",
            ],
            "tools": ["inspect_file", "get_event_logs", "web_search"],
            "notes": "这是只读验证计划；任何修复动作都需要用户单独确认。",
        },
    },
    ensure_ascii=False,
)

FINAL = json.dumps(
    {
        "narration": "已获得系统 [E1] 与文件签名 [E2] 证据，可以给出保守结论。",
        "action": "final",
        "basis": ["E1", "E2"],
        "final": {
            "headline": "目标文件签名正常；现有证据未显示明确的文件损坏。",
            "summary": (
                "kernel32.dll 的 Authenticode 签名验证通过，且位于 System32 系统目录 [E2]；"
                "系统基线为 Windows 11 Build 22631 [E1]。当前证据不足以定位额外根因，建议结合具体症状继续排查。"
            ),
            "findings": [
                {
                    "text": "kernel32.dll 签名状态为有效（Microsoft Windows 签名），SHA-256 已记录。",
                    "evidence": ["E2"],
                    "confidence": 0.97,
                },
                {
                    "text": "系统为 Windows 11 Pro Build 22631，系统盘剩余空间正常，未发现挂起重启。",
                    "evidence": ["E1"],
                    "confidence": 0.95,
                },
            ],
            "candidate_causes": [
                {
                    "cause": "目标系统文件本身损坏或被替换的可能性较低。",
                    "confidence": 0.25,
                    "evidence": ["E2"],
                    "supporting": "签名有效且位于受保护系统目录。",
                    "against": "签名有效不能证明运行期未被利用；未运行 SFC 深入检查。",
                }
            ],
            "next_steps": [
                {
                    "title": "在事件查看器中按故障时间复核 System/Application 日志及相关 Event ID。",
                    "why": "补全时间线；本工具只读取了摘要，未展开所有事件详情。",
                    "risk": "read-only",
                    "executed": False,
                    "evidence": ["E1"],
                },
                {
                    "title": "如问题持续且与系统文件有关，可创建还原点后运行 SFC 验证。",
                    "why": "SFC 是微软官方系统文件完整性检查；需要管理员权限并自行确认风险。",
                    "risk": "requires-admin",
                    "executed": False,
                    "evidence": ["E2"],
                },
            ],
            "limitations": [
                "未执行修复动作。",
                "未解析转储或读取硬件传感器。",
            ],
        },
    },
    ensure_ascii=False,
)

RESPONSES = [
    {"reasoning": "先展示计划，再执行工具。", "content": PLAN},
    {"reasoning": "证据已足够生成初步结论，所有结论绑定到证据 ID。", "content": FINAL},
]


class Handler(BaseHTTPRequestHandler):
    count = 0
    lock = threading.Lock()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        with Handler.lock:
            index = Handler.count
            Handler.count += 1
        if index >= len(RESPONSES):
            self.send_error(500, "no scripted response")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        item = RESPONSES[index]
        for chunk in _split(item["reasoning"], 12):
            self._event({"choices": [{"delta": {"reasoning_content": chunk}, "finish_reason": None}]})
        for chunk in _split(item["content"], 110):
            self._event({"choices": [{"delta": {"content": chunk}, "finish_reason": None}]})
        self._event({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 1200}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _event(self, obj) -> None:
        self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def log_message(self, format, *args):  # noqa: A002
        return


def _split(text: str, size: int):
    return [text[i : i + size] for i in range(0, len(text), size)]


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    os.environ["DEEPSEEK_BASE_URL"] = f"http://127.0.0.1:{port}"

    app = create_application(["ui_ai_mock_smoke"])
    config = AppConfig.load()
    print("model:", config.model, config.reasoning_effort, "base:", config.base_url)
    config.local_model_enabled = False  # mock 只验证主模型链路，避免 CPU 本地模型拖慢
    window = MainWindow(config, session_key="")
    target_env = os.environ.get("HELPER_PLUS_SMOKE_TARGET")
    target = (
        Path(target_env)
        if target_env
        else Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "kernel32.dll"
    )
    window.target_edit.setText(str(target))
    window.issue_edit.setText(
        "测试：检查目标目录/项目的结构与相关系统日志。"
        if target.is_dir()
        else "测试：检查一个系统 DLL 的签名与相关系统日志。"
    )
    window._refresh_target_label()
    window.resize(1280, 820)
    window._ai_consent_given = True  # 测试脚本跳过交互式数据发送确认
    window.show()
    app.processEvents()
    time.sleep(0.2)
    window.ai_button.click()
    deadline = time.monotonic() + 90
    while window._busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.03)
    app.processEvents()
    time.sleep(0.3)
    app.processEvents()
    print(
        "busy:", window._busy,
        "record:", bool(window.current_record),
        "report:", bool(window.final_report),
        "evidence:", window.evidence_tree.topLevelItemCount(),
        "audit:", window.audit_tree.topLevelItemCount(),
        "warnings:", window.warnings,
    )
    window.grab().save(str(OUT / "ui-ai-conclusion.png"))
    window.right_tabs.setCurrentIndex(0)
    app.processEvents()
    time.sleep(0.2)
    window.grab().save(str(OUT / "ui-ai-live.png"))
    # 单独保存执行计划页
    for index in range(window.right_tabs.count()):
        if window.right_tabs.tabText(index) == "执行计划":
            window.right_tabs.setCurrentIndex(index)
            app.processEvents()
            window.grab().save(str(OUT / "ui-ai-plan.png"))
            break
    print("saved", OUT / "ui-ai-conclusion.png", OUT / "ui-ai-live.png", OUT / "ui-ai-plan.png")
    window.close()
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()
