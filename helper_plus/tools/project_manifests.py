"""只读工具：项目/软件清单与内部架构线索。

当目标命名混乱、没有明确品牌/厂商，或需要判断“项目内部架构、依赖与缺失项”时，
读取项目目录中的已知清单文件（package.json、requirements.txt、pyproject.toml、
*.csproj、go.mod、pom.xml 等）、README 标题/描述和 Python import 缺失情况。

安全边界：
- 只在目标目录子树内扫描；
- 跳过 .git/node_modules/venv 等目录；
- 只读取已知文本清单，限制文件大小；
- 不读取 .env、私钥、证书等敏感文件；
- 只返回结构化摘要/短摘录，不返回完整源码。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
import xml.etree.ElementTree as ET

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

from ..models import ToolResult
from ..safety import SafetyError, ensure_no_unknown_keys, require_int, validate_directory_path
from ..windows import truncate_text
from .base import BaseTool, ToolContext


_SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", "env",
    "bin", "obj", "target", "dist", "build", ".idea", ".vs", ".gradle", ".next",
    ".nuxt", "vendor", "packages", "Debug", "Release", "x64", "x86",
}
_MANIFEST_NAMES = {
    "package.json", "requirements.txt", "requirements-dev.txt", "pyproject.toml",
    "setup.py", "setup.cfg", "pipfile", "poetry.lock", "package-lock.json",
    "yarn.lock", "pnpm-lock.yaml", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts", "gradle.properties", "cargo.toml",
    "go.mod", "go.sum", "composer.json", "gemfile", "dockerfile", "cmakelists.txt",
    "makefile", "packages.config", "directory.build.props", "global.json",
}
_MANIFEST_SUFFIXES = {".csproj", ".fsproj", ".vbproj", ".vcxproj", ".sln", ".props", ".targets"}
_README_NAMES = {"readme.md", "readme.txt", "readme", "readme.rst"}
_SKIP_FILE_NAMES = {".env", ".env.local", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|passwd|secret|client[_-]?secret)\b\s*[:=]\s*[\"']?[^\"'\s,}]+"
)
_DIST_NAME_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._@/-]*$")


def _redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)


def _normalize_dist_name(name: str) -> str:
    """PEP 503 规范化：大小写与 -/_/. 等价。"""
    return re.sub(r"[-_.]+", "-", str(name)).strip().lower()


def _installed_python_distributions() -> set[str]:
    installed: set[str] = set()
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover
        return installed
    try:
        distributions = metadata.distributions()
    except Exception:  # noqa: BLE001
        return installed
    for dist in distributions:
        try:
            name = dist.metadata["Name"] or ""
        except Exception:  # noqa: BLE001
            continue
        if name:
            installed.add(_normalize_dist_name(name))
    return installed


def _node_module_installed(manifest_path: Path, project_root: Path, name: str) -> bool:
    current = manifest_path.parent
    while True:
        if (current / "node_modules" / name).exists():
            return True
        if current == project_root or project_root not in current.parents:
            break
        current = current.parent
    return False


def _nuget_package_installed(name: str) -> bool:
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    return (home / ".nuget" / "packages" / name.lower()).exists()


def _declared_dependencies_status(
    manifests: list[dict[str, Any]], project_root: Path
) -> tuple[list[dict[str, Any]], int]:
    """检查“声明了但本机未安装”的依赖，覆盖 Python / Node / .NET。"""
    missing: list[dict[str, Any]] = []
    checked = 0
    installed_python: set[str] | None = None
    seen: set[tuple[str, str, str]] = set()
    for manifest in manifests:
        ecosystem = manifest.get("project_type")
        if ecosystem not in {"python", "node", "dotnet"}:
            continue
        manifest_path = Path(manifest.get("absolute_path") or project_root)
        for dependency in manifest.get("dependencies") or []:
            name = str(dependency).strip()
            if not name or not _DIST_NAME_RE.match(name):
                continue
            checked += 1
            if ecosystem == "python":
                if installed_python is None:
                    installed_python = _installed_python_distributions()
                installed = _normalize_dist_name(name) in installed_python
            elif ecosystem == "node":
                installed = _node_module_installed(manifest_path, project_root, name)
            else:
                installed = _nuget_package_installed(name)
            if installed:
                continue
            key = (ecosystem, name.lower(), str(manifest.get("path")))
            if key in seen:
                continue
            seen.add(key)
            missing.append(
                {
                    "name": name,
                    "ecosystem": ecosystem,
                    "manifest": manifest.get("path"),
                }
            )
            if len(missing) >= 100:
                return missing, checked
    return missing, checked


def _read_text(path: Path, limit: int = 8000) -> tuple[str, bool]:
    try:
        if path.stat().st_size > 512 * 1024:
            return "", True
        data = path.read_bytes()
    except OSError:
        return "", False
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "mbcs"):
        try:
            text = data.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        return "", False
    return _redact(text[:limit]), len(text) > limit


def _unique(values: list[str], limit: int = 300) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item.lower() in seen:
            continue
        seen.add(item.lower())
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _json_manifest(path: Path) -> dict[str, Any]:
    text, _ = _read_text(path, 200_000)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    deps: list[str] = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies", "require"):
        values = data.get(section)
        if isinstance(values, dict):
            deps.extend(str(key) for key in values)
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    entrypoints = [str(key) for key in scripts if key in {"start", "build", "test", "dev", "main"}]
    return {
        "name": data.get("name"),
        "description": data.get("description"),
        "version": data.get("version"),
        "dependencies": deps,
        "entrypoints": entrypoints,
    }


def _toml_manifest(path: Path) -> dict[str, Any]:
    if tomllib is None:
        return {}
    text, _ = _read_text(path, 500_000)
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, TypeError):
        return {}
    deps: list[str] = []

    def _collect(values: Any) -> None:
        if isinstance(values, list):
            for item in values:
                if isinstance(item, str):
                    deps.append(re.split(r"[<>=!~\[\s;]", item, maxsplit=1)[0])
                elif isinstance(item, dict):
                    for key in item:
                        deps.append(str(key))
        elif isinstance(values, dict):
            deps.extend(str(key) for key in values)

    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    _collect(project.get("dependencies"))
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        # extras 的键是分组名（如 dev/test），不能当作依赖名。
        for group in optional.values():
            _collect(group)
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        _collect(data.get(section))
    for key in ("tool.poetry.dependencies", "tool.poetry.dev-dependencies"):
        node: Any = data
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        _collect(node)
    package = data.get("package") if isinstance(data.get("package"), dict) else {}
    return {
        "name": project.get("name") or package.get("name"),
        "description": project.get("description") or package.get("description"),
        "version": project.get("version") or package.get("version"),
        "dependencies": deps,
        "target": project.get("requires-python"),
    }


def _requirements_manifest(path: Path) -> dict[str, Any]:
    text, _ = _read_text(path, 400_000)
    deps = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(("-", "git+", "http://", "https://")):
            continue
        name = re.split(r"[<>=!~;\[\s@]", line, maxsplit=1)[0].strip()
        if name:
            deps.append(name)
    return {"dependencies": deps}


def _xml_manifest(path: Path) -> dict[str, Any]:
    text, _ = _read_text(path, 500_000)
    if not text.strip():
        return {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    deps: list[str] = []
    frameworks: list[str] = []
    refs: list[str] = []
    assemblies: list[str] = []
    name = None
    for element in root.iter():
        tag = element.tag.split("}")[-1]
        if tag in {"PackageReference", "ProjectReference", "Reference", "Dependency"}:
            include = element.attrib.get("Include") or element.attrib.get("Update")
            if tag == "ProjectReference" and include:
                refs.append(include)
            elif tag == "Reference" and include:
                # .NET Framework 的 Reference 多为 GAC/框架程序集，不属于 NuGet 包依赖。
                assemblies.append(include.split(",")[0])
            elif include:
                deps.append(include.split(",")[0])
        elif tag in {"TargetFramework", "TargetFrameworks", "TargetFrameworkVersion"}:
            if element.text:
                frameworks.append(element.text.strip())
        elif tag in {"AssemblyName", "RootNamespace"} and element.text and not name:
            name = element.text.strip()
    return {
        "name": name,
        "dependencies": deps,
        "target": "、".join(_unique(frameworks, 6)),
        "local_references": refs,
        "assembly_references": assemblies,
    }


def _generic_manifest(path: Path) -> dict[str, Any]:
    text, _ = _read_text(path, 400_000)
    deps: list[str] = []
    for pattern in (r"(?m)^\s*require\s+([\w./-]+)", r"(?m)^\s*require\s*\(\s*[\"']([^\"']+)"):
        deps.extend(re.findall(pattern, text)[:80])
    for pattern in (r"(?m)^\s*([\w.-]+)\s+v?[0-9]", r"(?m)^\s*<dependency>.*?<artifactId>([^<]+)"):
        deps.extend(re.findall(pattern, text)[:80])
    title = None
    for line in text.splitlines()[:40]:
        line = line.strip()
        if line and not line.startswith(("#", "//", "<!--", "[", "{")):
            title = line[:180]
            break
    return {"description": title, "dependencies": deps}


def _python_missing_imports(root: Path, ctx: ToolContext) -> tuple[list[str], int]:
    local_modules: set[str] = set()
    py_files: list[Path] = []
    for path in root.rglob("*.py"):
        if ctx.cancel_event.is_set():
            raise RuntimeError("操作已取消。")
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        py_files.append(path)
        if path.name == "__init__.py":
            local_modules.add(path.parent.name)
        else:
            local_modules.add(path.stem)
        if len(py_files) >= 200:
            break
    imports: set[str] = set()
    for path in py_files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    missing: list[str] = []
    for module in sorted(imports):
        if module in stdlib or module in local_modules or module.startswith("_"):
            continue
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(module)
        except (ImportError, ValueError, ModuleNotFoundError):
            missing.append(module)
        if len(missing) >= 100:
            break
    return missing, len(py_files)


def _missing_local_references(root: Path, manifests: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for manifest in manifests:
        base = Path(manifest.get("absolute_path") or root).parent
        for ref in manifest.get("local_references") or []:
            try:
                candidate = (base / str(ref).replace("\\", "/")).resolve()
            except OSError:
                continue
            if root.resolve() in candidate.parents or candidate == root.resolve():
                if not candidate.exists():
                    missing.append(f"{manifest.get('path')} -> {ref}")
    return _unique(missing, 50)


class InspectProjectManifestsTool(BaseTool):
    name = "inspect_project_manifests"
    description = (
        "只读扫描目标项目/软件目录中的清单与架构线索：package.json、requirements.txt、"
        "pyproject.toml、*.csproj/*.sln、go.mod、Cargo.toml、pom.xml、CMakeLists.txt、README 等；"
        "提取软件名/描述/版本、依赖、目标框架、本地引用，检测 Python 已导入但未安装的模块，"
        "并检查 Python/Node/.NET 中“已声明但本机未安装”的依赖。"
        "用于命名混乱或无明确品牌时推断内部架构和缺失依赖；不读取完整源码/敏感文件。"
    )
    source = "项目清单只读解析（JSON/TOML/XML/文本）+ Python import 静态检查"
    native_command = "Project manifest inspection (read-only)"
    timeout_seconds = 60.0
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "maxLength": 1024,
                "description": "可选：目标目录或其子目录；省略表示当前目标目录。",
            },
            "max_files": {
                "type": "integer",
                "minimum": 1,
                "maximum": 40,
                "default": 20,
                "description": "最多读取/返回多少个清单文件。",
            },
        },
        "additionalProperties": False,
    }

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        ensure_no_unknown_keys(args, ["path", "max_files"], self.name)
        path = args.get("path")
        if path is not None and not isinstance(path, str):
            raise SafetyError("path 必须是字符串。")
        return {
            "path": path,
            "max_files": require_int(args.get("max_files", 20), "max_files", minimum=1, maximum=40),
        }

    def collect(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = validate_directory_path(args.get("path"), ctx.target)
        if not root.is_dir():
            raise RuntimeError("目标必须是项目目录；请选择文件夹作为目标。")
        max_files = int(args["max_files"])
        manifests: list[dict[str, Any]] = []
        project_types: list[str] = []
        declared_dependencies: list[str] = []
        readme_excerpt: str | None = None
        warnings: list[str] = []
        truncated = False
        root_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if ctx.cancel_event.is_set():
                raise RuntimeError("操作已取消。")
            current = Path(dirpath)
            depth = len(current.parts) - root_depth
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            if depth >= 3:
                dirnames[:] = []
            for filename in filenames:
                if len(manifests) >= max_files:
                    truncated = True
                    break
                lower = filename.lower()
                path = current / filename
                if lower in _SKIP_FILE_NAMES or lower.endswith((".key", ".pem", ".pfx", ".cer", ".crt")):
                    continue
                is_manifest = lower in _MANIFEST_NAMES or any(lower.endswith(suffix) for suffix in _MANIFEST_SUFFIXES)
                is_readme = lower in _README_NAMES
                if not is_manifest and not is_readme:
                    continue
                if is_readme:
                    if readme_excerpt is None:
                        readme_excerpt, _ = _read_text(path, 1600)
                    continue
                parsed: dict[str, Any] = {}
                if lower.endswith(".json"):
                    parsed = _json_manifest(path)
                elif lower.endswith(".toml"):
                    parsed = _toml_manifest(path)
                elif lower.startswith("requirements") and lower.endswith(".txt"):
                    parsed = _requirements_manifest(path)
                elif lower.endswith((".csproj", ".fsproj", ".vbproj", ".vcxproj", ".props", ".targets", ".xml")):
                    parsed = _xml_manifest(path)
                else:
                    parsed = _generic_manifest(path)
                name = parsed.get("name") or parsed.get("description") or filename
                dependencies = _unique([str(item) for item in (parsed.get("dependencies") or [])], 120)
                declared_dependencies.extend(dependencies)
                project_type = "python" if lower in {"pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"} else ""
                if lower.endswith((".csproj", ".fsproj", ".vbproj", ".vcxproj", ".sln")):
                    project_type = "dotnet"
                elif lower == "package.json":
                    project_type = "node"
                elif lower == "pom.xml":
                    project_type = "java-maven"
                elif lower.startswith("build.gradle"):
                    project_type = "java-gradle"
                elif lower == "go.mod":
                    project_type = "go"
                elif lower == "cargo.toml":
                    project_type = "rust"
                elif lower == "composer.json":
                    project_type = "php"
                elif lower == "cmakelists.txt":
                    project_type = "cmake"
                if project_type:
                    project_types.append(project_type)
                manifests.append(
                    {
                        "path": str(path.relative_to(root)),
                        "absolute_path": str(path),
                        "type": lower.rsplit(".", 1)[-1],
                        "project_type": project_type or None,
                        "name": truncate_text(str(name), 240),
                        "description": truncate_text(str(parsed.get("description") or ""), 500),
                        "version": truncate_text(str(parsed.get("version") or ""), 80),
                        "target": truncate_text(str(parsed.get("target") or ""), 200),
                        "dependencies": dependencies,
                        "local_references": _unique([str(x) for x in (parsed.get("local_references") or [])], 80),
                        "entrypoints": _unique([str(x) for x in (parsed.get("entrypoints") or [])], 20),
                    }
                )
            if len(manifests) >= max_files:
                truncated = True
                break
        try:
            missing_python_modules, python_files = _python_missing_imports(root, ctx)
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            missing_python_modules = []
            python_files = 0
            warnings.append(f"Python import 检查失败：{exc}")
        missing_local = _missing_local_references(root, manifests)
        missing_declared, declared_checked = _declared_dependencies_status(manifests, root)
        data = {
            "root": str(root),
            "project_types": _unique(project_types, 20),
            "manifests": manifests,
            "declared_dependencies": _unique(declared_dependencies, 200),
            "missing_local_references": missing_local,
            "missing_python_modules": missing_python_modules,
            "missing_declared_dependencies": missing_declared,
            "declared_dependency_checked": declared_checked,
            "declared_dependency_scope": {
                "python": (
                    "打包内置环境（可能不代表目标项目环境）"
                    if getattr(sys, "frozen", False)
                    else sys.executable
                ),
                "node": "项目及其上层的 node_modules",
                "dotnet": "~/.nuget/packages",
            },
            "python_files_scanned": python_files,
            "readme_excerpt": readme_excerpt,
            "truncated": truncated,
            "warnings": warnings,
        }
        summary_parts = [
            f"项目架构扫描：{len(manifests)} 个清单",
            f"项目类型 {'/'.join(data['project_types']) or '未识别'}",
            f"声明依赖 {len(data['declared_dependencies'])} 个",
        ]
        if missing_local:
            summary_parts.append(f"本地引用缺失 {len(missing_local)} 个")
        if missing_python_modules:
            summary_parts.append(f"Python 导入未安装 {len(missing_python_modules)} 个")
        if missing_declared:
            summary_parts.append(f"声明依赖未安装 {len(missing_declared)} 个")
        return ToolResult(
            ok=True,
            summary="；".join(summary_parts) + "。",
            data=data,
            source=self.source,
            native_command=self.native_command,
            raw_excerpt=truncate_text(str(manifests), 4000),
        )
