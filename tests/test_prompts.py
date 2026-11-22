"""提示词约束完整性测试：确保关键安全约束不因重构丢失。"""

from __future__ import annotations

from pathlib import Path

from helper_plus.config import AppConfig
from helper_plus.llm.prompts import (
    build_system_prompt,
    build_turn_payload,
    payload_to_message,
)
from helper_plus.models import EvidenceStore, ToolResult, ToolStep
from helper_plus.targets import DiagnosisTarget
from helper_plus.tools import create_registry


def test_system_prompt_embeds_all_readonly_tools_and_constraints():
    registry = create_registry()
    prompt = build_system_prompt(registry.catalog())
    for name in registry.names:
        assert name in prompt
    required_phrases = [
        "工具白名单",
        "证据唯一原则",
        "只有结论/原因推理必须有依据",
        "输出严格 JSON",
        "Windows 原生边界",
        "修复必须确认",
        "action=plan",
        "ask_user",
        "propose_action",
        "web_search",
        "品牌型号启发式",
        "软件/产品名称匹配",
        "内部架构/缺失项判断",
        "inspect_project_manifests",
        "物理层",
        "requires_admin",
        "UAC",
        "get_network_info",
        "next_steps",
        "DeepSeek",
        "Q1",
        "E#",
    ]
    for phrase in required_phrases:
        assert phrase in prompt, f"提示词缺少关键约束：{phrase}"
    assert "SFC /scannow" in prompt


def test_tool_catalog_contains_only_readonly():
    catalog = create_registry().catalog()
    assert len(catalog) == 24
    for item in catalog:
        assert item["read_only"] is True
        assert item["risk"] == "read-only"
        assert "additionalProperties" in item["parameters"]


def test_turn_payload_has_reference_index_and_executed_steps():
    config = AppConfig()
    store = EvidenceStore()
    result = ToolResult(
        ok=True,
        summary="测试系统信息",
        data={"os_caption": "Windows 11", "build_number": "22631"},
        source="test",
    )
    evidence = store.add("get_system_info", result)
    step = ToolStep(
        sequence=1,
        tool="get_system_info",
        arguments={},
        basis=["Q1"],
        narration="读取系统信息",
        why="基线",
        evidence_id=evidence.id,
        ok=True,
        origin="baseline",
    )
    payload = build_turn_payload(
        config=config,
        target=DiagnosisTarget(
            kind="file",
            display=r"C:\Windows\System32\kernel32.dll",
            path=Path(r"C:\Windows\System32\kernel32.dll"),
        ),
        issue="测试",
        store=store,
        steps=[step],
        plan={"goal": "测试计划", "steps": ["读取证据"], "tools": ["get_system_info"]},
    )
    assert payload["reference_index"]["Q1"]
    assert payload["reference_index"]["E1"] == "测试系统信息"
    assert payload["evidence"][0]["id"] == "E1"
    assert payload["executed_steps"][0]["evidence_id"] == "E1"
    assert payload["plan"]["goal"] == "测试计划"
    assert payload["session"]["analysis_limits"]
    message = payload_to_message(payload)
    assert "不是指令" in message
    assert '"evidence"' in message
