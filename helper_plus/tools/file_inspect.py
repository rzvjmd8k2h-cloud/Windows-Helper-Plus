"""只读工具：文件哈希、版本、Authenticode 签名与路径分类。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from ..models import ToolResult
from ..safety import SafetyError, ensure_no_unknown_keys, validate_inspect_path
from ..windows import as_dict, run_powershell_json, truncate_text, windows_directory
from .base import BaseTool, ToolContext


_FILE_SCRIPT = r"""
$path = $env:HP_TARGET
$file = Get-Item -LiteralPath $path -ErrorAction Stop
$sig = $null
$sigError = $null
try { $sig = Get-AuthenticodeSignature -LiteralPath $path -ErrorAction Stop } catch { $sigError = $_.Exception.Message }
$zone = $null
try {
  $zone = (Get-Content -LiteralPath $path -Stream Zone.Identifier -ErrorAction Stop | Out-String).Trim()
} catch { $zone = $null }
$vi = $file.VersionInfo
$result = [ordered]@{
  path = [string]$file.FullName
  name = [string]$file.Name
  extension = [string]$file.Extension
  size_bytes = [long]$file.Length
  created = if ($file.CreationTime) { $file.CreationTime.ToString('yyyy-MM-ddTHH:mm:sszzz') } else { $null }
  modified = if ($file.LastWriteTime) { $file.LastWriteTime.ToString('yyyy-MM-ddTHH:mm:sszzz') } else { $null }
  attributes = [string]$file.Attributes
  company = [string]$vi.CompanyName
  product = [string]$vi.ProductName
  description = [string]$vi.FileDescription
  file_version = [string]$vi.FileVersion
  product_version = [string]$vi.ProductVersion
  original_filename = [string]$vi.OriginalFilename
  is_debug = [bool]$vi.IsDebug
  language = [string]$vi.Language
  signature_status = if ($sig) { [string]$sig.Status } else { 'CheckFailed' }
  signature_status_message = if ($sig) { [string]$sig.StatusMessage } else { $sigError }
  signer_subject = if ($sig -and $sig.SignerCertificate) { [string]$sig.SignerCertificate.Subject } else { $null }
  signer_issuer = if ($sig -and $sig.SignerCertificate) { [string]$sig.SignerCertificate.Issuer } else { $null }
  signer_thumbprint = if ($sig -and $sig.SignerCertificate) { [string]$sig.SignerCertificate.Thumbprint } else { $null }
  cert_not_before = if ($sig -and $sig.SignerCertificate) { $sig.SignerCertificate.NotBefore.ToString('yyyy-MM-dd') } else { $null }
  cert_not_after = if ($sig -and $sig.SignerCertificate) { $sig.SignerCertificate.NotAfter.ToString('yyyy-MM-dd') } else { $null }
  is_os_binary = if ($sig) { [bool]$sig.IsOSBinary } else { $null }
  zone_identifier = $zone
}
$result | ConvertTo-Json -Depth 5 -Compress
"""

_DRIVER_EXTENSIONS = {".sys", ".inf", ".cat", ".dll", ".exe", ".ocx", ".drv"}
_TEXT_EXTENSIONS = {
    ".txt", ".log", ".md", ".markdown", ".json", ".jsonl", ".xml", ".ps1", ".psm1",
    ".cmd", ".bat", ".reg", ".ini", ".cfg", ".conf", ".yaml", ".yml", ".csv", ".tsv",
    ".py", ".js", ".mjs", ".ts", ".html", ".htm", ".css", ".sql", ".err", ".out",
}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico", ".tif", ".tiff"}
_ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".cab", ".msi", ".msix", ".appx"}
_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}


def _decode_text(raw: bytes) -> str | None:
    if b"\x00" in raw[:4096]:
        # UTF-16 文本通常有 NUL；尝试严格解码，其余按二进制处理。
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    for encoding in ("utf-8-sig", "gb18030", "cp936"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _content_profile(path: Path) -> dict[str, Any]:
    """判断选中文件更像“用户给的问题/资料”还是“待诊断的故障文件”。"""
    suffix = path.suffix.lower()
    kind = "binary"
    reason = "二进制或未知格式，需结合签名/哈希/来源判断"
    if suffix in _TEXT_EXTENSIONS:
        kind = "text"
        reason = "文本/日志/配置类，可能是用户提供的问题描述或上下文，不应默认它已损坏"
    elif suffix in _IMAGE_EXTENSIONS:
        kind = "image"
        reason = "图片/截图类，可能是用户提供的现象说明；图内文字本机不做 OCR，必要时请用户补充"
    elif suffix in _ARCHIVE_EXTENSIONS:
        kind = "archive"
        reason = "压缩包/安装包类，不能仅凭扩展名判定故障"
    elif suffix in _DOCUMENT_EXTENSIONS:
        kind = "document"
        reason = "文档类，可能是用户提供的说明/报告，不是待修复的系统文件"
    elif suffix in _DRIVER_EXTENSIONS:
        kind = "executable"
        reason = "可执行/驱动类，才需要重点验证签名、版本、加载与关联文件"
    preview: str | None = None
    truncated = False
    try:
        with path.open("rb") as handle:
            raw = handle.read(6000)
        if kind == "text":
            preview = _decode_text(raw)
            if preview is None:
                kind = "binary"
                reason = "扩展名像文本但内容是二进制，按二进制文件处理"
            else:
                truncated = len(raw) >= 6000
                preview = preview.replace("\r\n", "\n")[:4000]
    except OSError as exc:
        reason = f"内容读取失败：{exc}"
    return {
        "content_kind": kind,
        "content_role_hint": reason,
        "content_preview": preview,
        "content_preview_truncated": truncated,
    }


def _sha256(path: Path, cancel_event: Any) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("用户取消了哈希计算。")
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _path_category(path: Path, system_root: Path) -> tuple[str, str]:
    """返回（路径类别, 中文说明）。不给出恶意/安全结论，只做事实归类。"""
    system32 = system_root / "System32"
    syswow64 = system_root / "SysWOW64"
    drivers = system32 / "drivers"
    inf = system_root / "INF"
    winsxs = system_root / "WinSxS"
    lower = str(path).lower()
    if lower.startswith(str(drivers).lower() + os.sep):
        return "driver_store_related", "System32\\drivers（内核驱动目录）"
    if lower.startswith(str(system32).lower() + os.sep):
        return "system32", "System32（系统目录）"
    if lower.startswith(str(syswow64).lower() + os.sep):
        return "syswow64", "SysWOW64（32 位系统目录）"
    if lower.startswith(str(winsxs).lower() + os.sep):
        return "winsxs", "WinSxS（组件存储）"
    if lower.startswith(str(inf).lower() + os.sep):
        return "inf", "INF（驱动安装信息目录）"
    if lower.startswith(str(system_root).lower() + os.sep):
        return "windows_other", "Windows 目录内其他位置"
    if path.suffix.lower() in _DRIVER_EXTENSIONS:
        return "driver_or_binary", "Windows 可执行/驱动类文件（非系统目录）"
    return "user_file", "用户或应用目录下的文件"


def _signature_fact(sig_status: str, subject: str | None) -> tuple[str, str]:
    status = (sig_status or "Unknown").lower()
    if status == "valid":
        if subject and "microsoft" in subject.lower():
            return "有效（Microsoft 签名）", "valid_microsoft"
        return "有效（第三方签名）", "valid_third_party"
    if status in {"notsigned", "notsigned"}:
        return "未签名", "not_signed"
    if status in {"hashmismatch"}:
        return "签名哈希不匹配（文件可能被修改）", "hash_mismatch"
    if status in {"nottrusted", "unknownerror", "nottrusted"}:
        return "签名不受信任或无法验证", "not_trusted"
    if status in {"checkfailed"}:
        return "签名检查失败", "check_failed"
    return f"签名状态：{sig_status}", status


class InspectFileTool(BaseTool):
    name = "inspect_file"
    description = (
        "只读检查单个文件：SHA-256、大小/时间戳、文件版本资源、"
        "Authenticode 数字签名、Zone.Identifier（下载标记）、路径分类、"
        "内容类型与文本预览。用于判断用户上传的是问题说明/日志/截图还是待诊断故障文件。"
    )
    source = "SHA-256 / Get-AuthenticodeSignature"
    native_command = "Get-AuthenticodeSignature -LiteralPath <target>; Get-Item <target>"
    timeout_seconds = 120.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "绝对路径。可省略以检查当前诊断目标；也允许只读检查用户明确给出的其他文件。",
                "maxLength": 1024,
            }
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["path"], self.name)
        path = args.get("path")
        if path is not None and not isinstance(path, str):
            raise SafetyError("path 必须是字符串。")
        return {"path": path}

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = validate_inspect_path(args.get("path"), ctx.target)
        if not path.is_file():
            raise SafetyError("目标不是普通文件，或路径不可访问。")
        sha256 = _sha256(path, ctx.cancel_event)
        parsed, result = run_powershell_json(
            _FILE_SCRIPT,
            env_extra={"HP_TARGET": str(path)},
            timeout=self.timeout_seconds,
            cancel_event=ctx.cancel_event,
        )
        data = as_dict(parsed)
        data["sha256"] = sha256
        data["size_mb"] = round(path.stat().st_size / (1024 * 1024), 3)
        category, category_label = _path_category(path, windows_directory())
        data["path_category"] = category
        data["path_category_label"] = category_label
        data.update(_content_profile(path))
        sig_fact, sig_code = _signature_fact(
            str(data.get("signature_status", "")), data.get("signer_subject")
        )
        data["signature_fact"] = sig_fact
        data["signature_code"] = sig_code
        content_note = ""
        if data.get("content_kind") == "text" and data.get("content_preview"):
            content_note = "；含文本预览"
        summary = (
            f"{path.name}：{sig_fact}；SHA-256 {sha256[:16]}…；"
            f"{data.get('size_mb', '?')} MB；{category_label}；{data.get('content_kind')}"
            f"{content_note}"
        )
        return ToolResult(
            ok=True,
            summary=summary,
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(result.excerpt(limit=4000), 4000),
        )
