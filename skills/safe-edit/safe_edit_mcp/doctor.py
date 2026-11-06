"""Diagnose safe-edit installation drift and MCP connectivity."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .installer import CLIENTS, config_targets


SERVER_NAME = "safe-edit"
EXPECTED_TOOLS = (
    "safe_edit_preflight",
    "safe_edit_stat",
    "safe_edit_transaction",
)
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2024-11-05")
CORE_PATH = Path(__file__).resolve().parents[1] / "safe_edit.py"
REPO_ROOT = Path(__file__).resolve().parents[3]
_VERSION_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def _drift(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    item = {"code": code, "message": message}
    item.update(extra)
    return item


def _warning(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    item = {"code": code, "message": message}
    item.update(extra)
    return item


def _package_version() -> Optional[str]:
    try:
        return importlib.metadata.version("safe-edit-mcp")
    except Exception:
        return None


def _plugin_version() -> Tuple[Optional[str], Optional[str]]:
    path = REPO_ROOT / ".codex-plugin" / "plugin.json"
    if not path.is_file():
        return None, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, str(path)
    if not isinstance(value, dict):
        return None, str(path)
    version = value.get("version")
    return (version if isinstance(version, str) else None), str(path)


def _collect_versions(
    drift: List[Dict[str, Any]], warnings: List[Dict[str, Any]]
) -> Dict[str, Any]:
    package = _package_version()
    plugin, plugin_path = _plugin_version()
    values = [value for value in (package, plugin) if value is not None]
    if package is not None and package != __version__:
        drift.append(
            _drift(
                "package_version_mismatch",
                f"distribution metadata is {package}, module is {__version__}",
            )
        )
    if plugin is not None and plugin != __version__:
        drift.append(
            _drift(
                "plugin_version_mismatch",
                f"{plugin_path} is {plugin}, module is {__version__}",
            )
        )
    if plugin_path is not None and plugin is None:
        warnings.append(
            _warning("plugin_version_unreadable", f"cannot read {plugin_path}")
        )
    return {
        "module": __version__,
        "package": package,
        "plugin": plugin,
        "pluginPath": plugin_path,
        "consistent": all(value == __version__ for value in values),
    }


def _collect_core(
    expect_sha256: Optional[str],
    drift: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Optional[str]]:
    if not CORE_PATH.is_file():
        drift.append(
            _drift("core_missing", f"core script is missing: {CORE_PATH}")
        )
        return {
            "siblingPath": str(CORE_PATH),
            "siblingSha256": None,
            "importedPath": None,
            "importedSha256": None,
            "expectedSha256": expect_sha256,
            "sizeBytes": 0,
            "lineCount": 0,
            "matchesExpected": False,
            "shadowed": False,
        }, None

    core_sha = _sha256(CORE_PATH)
    imported_path: Optional[str] = None
    imported_sha: Optional[str] = None
    try:
        spec = importlib.util.find_spec("safe_edit")
        if spec is not None and spec.origin is not None:
            imported_path = str(Path(spec.origin).resolve())
            imported_file = Path(imported_path)
            if imported_file.is_file():
                imported_sha = _sha256(imported_file)
    except (ImportError, ValueError, OSError):
        imported_path = None

    shadowed = bool(
        imported_path
        and Path(imported_path).resolve() != CORE_PATH.resolve()
    )
    matches = expect_sha256 is None or core_sha == expect_sha256
    if expect_sha256 is not None and not matches:
        drift.append(
            _drift(
                "core_sha256_mismatch",
                f"core is {core_sha}, expected {expect_sha256}",
            )
        )
    if shadowed:
        drift.append(
            _drift(
                "core_shadowed",
                f"imported safe_edit is {imported_path}, sibling is {CORE_PATH}",
            )
        )
    return {
        "siblingPath": str(CORE_PATH),
        "siblingSha256": core_sha,
        "importedPath": imported_path,
        "importedSha256": imported_sha,
        "expectedSha256": expect_sha256,
        "sizeBytes": CORE_PATH.stat().st_size,
        "lineCount": _line_count(CORE_PATH),
        "matchesExpected": matches,
        "shadowed": shadowed,
    }, core_sha


def _load_json(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        return None, str(error)
    try:
        return json.loads(raw.decode("utf-8-sig")), None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, str(error)


def _resolve_path(
    value: str, cwd: Optional[str], base_dir: Path
) -> Optional[Path]:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    base = Path(cwd).expanduser() if cwd else base_dir
    if not base.is_absolute():
        base = base_dir / base
    return base / candidate


def _resolve_command(
    command: str, cwd: Optional[str], base_dir: Path
) -> Optional[str]:
    candidate = Path(command).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = _resolve_path(command, cwd, base_dir)
        if resolved is not None and resolved.is_file():
            return str(resolved)
        return None
    return shutil.which(command)


def _run_text(
    argv: Sequence[str], cwd: Optional[str], timeout: float
) -> Tuple[Optional[str], Optional[str]]:
    try:
        proc = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return None, str(error)
    output = proc.stdout.strip()
    if proc.returncode != 0:
        detail = proc.stderr.strip() or output or f"exit {proc.returncode}"
        return None, detail
    return output, None


def _run_json(
    argv: Sequence[str], cwd: Optional[str], timeout: float
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    output, error = _run_text(argv, cwd, timeout)
    if output is None:
        return None, error
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value, None
    return None, "expected a JSON object"


def _find_script(
    command: str, args: Sequence[str], cwd: Optional[str], base_dir: Path
) -> Optional[Path]:
    for value in [command, *args]:
        if not isinstance(value, str) or not value.endswith(".py"):
            continue
        candidate = _resolve_path(value, cwd, base_dir)
        if candidate is not None and candidate.is_file():
            return candidate
    return None


def _script_reference_is_missing(
    command: str, args: Sequence[str], cwd: Optional[str], base_dir: Path
) -> bool:
    for value in [command, *args]:
        if not isinstance(value, str) or not value.endswith(".py"):
            continue
        candidate = _resolve_path(value, cwd, base_dir)
        if candidate is None or not candidate.is_file():
            return True
    return False


def _module_reference_exists(
    command: str,
    args: Sequence[str],
    cwd: Optional[str],
    base_dir: Path,
    timeout: float,
) -> Optional[bool]:
    resolved = _resolve_command(command, cwd, base_dir)
    if resolved is None or "-m" not in args:
        return None
    resolved_name = Path(resolved).name.lower()
    if not resolved_name.startswith("python") and resolved_name not in (
        "py",
        "py.exe",
    ):
        return None
    index = list(args).index("-m")
    module = args[index + 1] if index + 1 < len(args) else ""
    if not module:
        return False
    source = (
        "import contextlib, importlib.util\n"
        "spec = None\n"
        "with contextlib.suppress(ImportError, ValueError):\n"
        f"    spec = importlib.util.find_spec({module!r})\n"
        "print('true' if spec is not None else 'false')"
    )
    output, _error = _run_text(
        [resolved, "-c", source], cwd, timeout
    )
    if output == "true":
        return True
    if output == "false":
        return False
    return None


def _probe_entry(
    command: str,
    args: Sequence[str],
    cwd: Optional[str],
    base_dir: Path,
    timeout: float,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": "unverifiable",
        "version": None,
        "coreSha256": None,
        "error": None,
    }
    resolved = _resolve_command(command, cwd, base_dir)
    if resolved is None:
        result.update(status="error", error="command not found")
        return result

    resolved_name = Path(resolved).name.lower()
    if resolved_name in ("safe-edit-mcp", "safe-edit-mcp.exe", "safe-edit-mcp.cmd"):
        probe, _ = _run_json(
            [resolved, "doctor", "--core-sha256", "--json"], cwd, timeout
        )
        if probe is not None:
            core = probe.get("core")
            result.update(
                status="ok",
                version=probe.get("version"),
                coreSha256=core.get("sha256") if isinstance(core, dict) else None,
            )
            return result
        version, _ = _run_text([resolved, "--version"], cwd, timeout)
        if version is not None:
            result.update(
                status="stale" if version != __version__ else "unverifiable",
                version=version,
            )
        return result

    if resolved_name.startswith("python") or resolved_name in ("py", "py.exe"):
        args_list = list(args)
        if "-m" in args_list:
            index = args_list.index("-m")
            module = args_list[index + 1] if index + 1 < len(args_list) else ""
            if module == "safe_edit_mcp.server":
                probe, _ = _run_json(
                    [
                        resolved,
                        "-m",
                        "safe_edit_mcp.doctor",
                        "--core-sha256",
                        "--json",
                    ],
                    cwd,
                    timeout,
                )
                if probe is not None:
                    core = probe.get("core")
                    result.update(
                        status="ok",
                        version=probe.get("version"),
                        coreSha256=(
                            core.get("sha256") if isinstance(core, dict) else None
                        ),
                    )
                return result

    script = _find_script(command, args, cwd, base_dir)
    if script is not None:
        candidates = [
            script.parent / "safe_edit.py",
            script.parent.parent / "safe_edit.py",
            script.parent.parent / "skills" / "safe-edit" / "safe_edit.py",
        ]
        for candidate in candidates:
            if candidate.is_file():
                result.update(status="ok", coreSha256=_sha256(candidate))
                break
    return result


def _collect_configs(
    home: Path,
    project_dir: Path,
    clients: Sequence[str],
    scope: str,
    timeout: float,
    drift: List[Dict[str, Any]],
    warnings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for target in config_targets(home, project_dir):
        client = str(target["client"])
        target_scope = str(target["scope"])
        if client not in clients:
            continue
        if scope != "all" and target_scope != scope:
            continue
        path = Path(target["path"])
        key = str(path.resolve(strict=False))
        group = grouped.setdefault(
            key,
            {
                "path": path,
                "section": target["section"],
                "clients": [],
                "scopes": [],
                "shared": bool(target["shared"]),
            },
        )
        if client not in group["clients"]:
            group["clients"].append(client)
        if target_scope not in group["scopes"]:
            group["scopes"].append(target_scope)
        group["shared"] = group["shared"] or bool(target["shared"])

    results: List[Dict[str, Any]] = []
    for group in grouped.values():
        path = group["path"]
        result: Dict[str, Any] = {
            "path": str(path),
            "section": group["section"],
            "clients": group["clients"],
            "scopes": group["scopes"],
            "shared": group["shared"],
            "sharedBy": group["clients"]
            + (["codex-plugin"] if path.name == ".mcp.json" else []),
            "exists": path.is_file(),
            "entryPresent": False,
            "command": None,
            "args": [],
            "commandExists": False,
            "launcherExists": False,
            "probe": None,
            "status": "missing",
            "error": None,
        }
        if not result["exists"]:
            results.append(result)
            continue
        data, error = _load_json(path)
        if error is not None or not isinstance(data, dict):
            result.update(status="invalid", error=error or "root is not an object")
            drift.append(
                _drift(
                    "config_invalid",
                    f"{path}: {result['error']}",
                    path=str(path),
                )
            )
            results.append(result)
            continue
        section = data.get(group["section"])
        if section is None:
            result.update(status="entry_missing")
            warnings.append(
                _warning(
                    "config_entry_missing",
                    f"{path}: no {group['section']} section",
                    path=str(path),
                )
            )
            results.append(result)
            continue
        if not isinstance(section, dict):
            result.update(
                status="invalid",
                error=f"{group['section']} is not an object",
            )
            drift.append(
                _drift(
                    "config_section_invalid",
                    f"{path}: {result['error']}",
                    path=str(path),
                )
            )
            results.append(result)
            continue
        entry = section.get("safe-edit")
        if entry is None:
            result.update(status="entry_missing")
            warnings.append(
                _warning(
                    "config_entry_missing",
                    f"{path}: no safe-edit entry",
                    path=str(path),
                )
            )
            results.append(result)
            continue
        result["entryPresent"] = True
        if not isinstance(entry, dict):
            result.update(status="invalid", error="safe-edit entry is not an object")
            drift.append(
                _drift(
                    "config_entry_invalid",
                    f"{path}: safe-edit entry is not an object",
                    path=str(path),
                )
            )
            results.append(result)
            continue
        command = entry.get("command")
        args = entry.get("args", [])
        if isinstance(command, list):
            if not command:
                result.update(status="invalid", error="command list is empty")
                drift.append(
                    _drift(
                        "config_command_missing",
                        f"{path}: command list is empty",
                        path=str(path),
                    )
                )
                results.append(result)
                continue
            args = list(command[1:]) + list(args if isinstance(args, list) else [])
            command = command[0]
        if not isinstance(args, list):
            args = []
        if not isinstance(command, str) or not command:
            result.update(status="invalid", error="command is missing")
            drift.append(
                _drift(
                    "config_command_missing",
                    f"{path}: safe-edit command is missing",
                    path=str(path),
                )
            )
            results.append(result)
            continue
        cwd = entry.get("cwd")
        cwd_value = cwd if isinstance(cwd, str) else None
        base_dir = path.parent
        resolved = _resolve_command(command, cwd_value, base_dir)
        command_exists = resolved is not None
        script = _find_script(command, args, cwd_value, base_dir)
        script_missing = _script_reference_is_missing(
            command, args, cwd_value, base_dir
        )
        module_exists = _module_reference_exists(
            command, args, cwd_value, base_dir, timeout
        )
        module_missing = module_exists is False
        launcher_exists = script is not None or (
            command_exists and not script_missing and not module_missing
        )
        probe = _probe_entry(command, args, cwd_value, base_dir, timeout)
        result.update(
            command=command,
            args=[str(value) for value in args],
            commandExists=command_exists,
            launcherExists=launcher_exists,
            probe=probe,
        )
        if not command_exists:
            result.update(status="invalid", error="command not found")
            drift.append(
                _drift(
                    "config_command_missing",
                    f"{path}: command not found: {command}",
                    path=str(path),
                )
            )
        elif script_missing or module_missing:
            code = (
                "config_module_missing"
                if module_missing
                else "config_launcher_missing"
            )
            result.update(status="invalid", error="launcher or module not found")
            drift.append(
                _drift(
                    code,
                    f"{path}: safe-edit launcher or module is missing",
                    path=str(path),
                )
            )
        elif not launcher_exists:
            result.update(status="invalid", error="launcher not found")
            drift.append(
                _drift(
                    "config_launcher_missing",
                    f"{path}: safe-edit launcher is missing",
                    path=str(path),
                )
            )
        elif probe["status"] == "stale":
            result.update(status="stale", error="configured server is stale")
            drift.append(
                _drift(
                    "config_server_stale",
                    f"{path}: configured server is not the current release",
                    path=str(path),
                    version=probe.get("version"),
                    coreSha256=probe.get("coreSha256"),
                )
            )
        elif probe["status"] == "ok":
            result.update(status="ok")
        else:
            result.update(status="unverifiable")
            warnings.append(
                _warning(
                    "config_unverifiable",
                    f"{path}: configured server could not be probed",
                    path=str(path),
                )
            )
        results.append(result)
    return results


def _collect_skill_copies(
    home: Path,
    project_dir: Path,
    explicit_dirs: Sequence[Path],
    current_sha: Optional[str],
    drift: List[Dict[str, Any]],
    warnings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    defaults = [
        home / ".agents" / "skills" / "safe-edit",
        home / ".claude" / "skills" / "safe-edit",
        project_dir / ".agents" / "skills" / "safe-edit",
        project_dir / ".claude" / "skills" / "safe-edit",
    ]
    explicit = {str(path.expanduser().resolve(strict=False)) for path in explicit_dirs}
    candidates: List[Path] = list(defaults) + list(explicit_dirs)
    seen = set()
    results: List[Dict[str, Any]] = []
    for candidate in candidates:
        path = candidate.expanduser()
        key = str(path.resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        is_explicit = key in explicit
        exists = path.is_dir()
        core = path / "safe_edit.py"
        result: Dict[str, Any] = {
            "path": str(path),
            "exists": exists,
            "corePath": str(core),
            "coreSha256": None,
            "skillSha256": None,
            "version": None,
            "matchesCurrent": False,
        }
        if exists and core.is_file():
            try:
                result["coreSha256"] = _sha256(core)
            except OSError as error:
                result["error"] = str(error)
            version_path = path / "safe_edit_mcp" / "__init__.py"
            if version_path.is_file():
                try:
                    text = version_path.read_text(encoding="utf-8", errors="replace")
                    match = _VERSION_RE.search(text)
                    if match:
                        result["version"] = match.group(1)
                except OSError:
                    pass
            skill_doc = path / "SKILL.md"
            if skill_doc.is_file():
                result["skillSha256"] = _sha256(skill_doc)
            current_doc = CORE_PATH.parent / "SKILL.md"
            current_doc_sha = (
                _sha256(current_doc) if current_doc.is_file() else None
            )
            core_matches = (
                current_sha is not None and result["coreSha256"] == current_sha
            )
            version_matches = result["version"] == __version__
            docs_match = (
                current_doc_sha is None
                or result["skillSha256"] == current_doc_sha
            )
            result["matchesCurrent"] = (
                core_matches and version_matches and docs_match
            )
            if not core_matches:
                drift.append(
                    _drift(
                        "skill_core_mismatch",
                        f"{path}: skill core differs from this doctor",
                        path=str(path),
                        coreSha256=result["coreSha256"],
                    )
                )
            elif not version_matches:
                drift.append(
                    _drift(
                        "skill_version_mismatch",
                        f"{path}: skill version differs from this doctor",
                        path=str(path),
                        version=result["version"],
                    )
                )
            elif not docs_match:
                drift.append(
                    _drift(
                        "skill_docs_mismatch",
                        f"{path}: SKILL.md differs from this doctor",
                        path=str(path),
                        skillSha256=result["skillSha256"],
                    )
                )
        elif exists and is_explicit:
            warnings.append(
                _warning(
                    "skill_core_missing",
                    f"{path}: safe_edit.py is missing",
                    path=str(path),
                )
            )
        elif not exists and is_explicit:
            warnings.append(
                _warning(
                    "skill_dir_missing",
                    f"{path}: skill directory does not exist",
                    path=str(path),
                )
            )
        results.append(result)
    return results


def _collect_handshake(
    timeout: float,
    skip: bool,
    drift: List[Dict[str, Any]],
) -> Dict[str, Any]:
    argv = [sys.executable, "-m", "safe_edit_mcp.server"]
    result: Dict[str, Any] = {
        "ok": False,
        "skipped": skip,
        "argv": argv,
        "serverName": None,
        "serverVersion": None,
        "protocolVersion": None,
        "tools": [],
        "elapsedMs": None,
        "error": None,
    }
    if skip:
        return result
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {
                    "name": "safe-edit-doctor",
                    "version": __version__,
                },
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    payload = "\n".join(
        json.dumps(request, ensure_ascii=False) for request in requests
    ) + "\n"
    started = time.perf_counter()
    env = dict(os.environ)
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(CORE_PATH.parent) + (
        os.pathsep + pythonpath if pythonpath else ""
    )
    try:
        proc = subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
        drift.append(_drift("handshake_failed", str(error)))
        return result
    result["elapsedMs"] = round((time.perf_counter() - started) * 1_000, 3)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if proc.returncode != 0 or len(lines) < 2:
        detail = proc.stderr.strip() or f"exit {proc.returncode}"
        result["error"] = detail
        drift.append(_drift("handshake_failed", detail))
        return result
    try:
        init = json.loads(lines[0])
        tools = json.loads(lines[1])
    except json.JSONDecodeError as error:
        result["error"] = f"invalid handshake JSON: {error}"
        drift.append(_drift("handshake_failed", result["error"]))
        return result
    init_result = init.get("result") if isinstance(init, dict) else None
    if not isinstance(init_result, dict):
        result["error"] = "initialize returned no result"
        drift.append(_drift("handshake_failed", result["error"]))
        return result
    server_info = init_result.get("serverInfo")
    if not isinstance(server_info, dict):
        server_info = {}
    tool_result = tools.get("result") if isinstance(tools, dict) else None
    tool_list = tool_result.get("tools") if isinstance(tool_result, dict) else None
    tool_names = []
    if isinstance(tool_list, list):
        for tool in tool_list:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                tool_names.append(tool["name"])
    result.update(
        serverName=server_info.get("name"),
        serverVersion=server_info.get("version"),
        protocolVersion=init_result.get("protocolVersion"),
        tools=tool_names,
        ok=True,
    )
    if result["serverName"] != SERVER_NAME:
        result["ok"] = False
        drift.append(
            _drift(
                "handshake_identity_mismatch",
                f"server name is {result['serverName']}",
            )
        )
    if result["serverVersion"] != __version__:
        result["ok"] = False
        drift.append(
            _drift(
                "handshake_version_mismatch",
                f"server version is {result['serverVersion']}, module is {__version__}",
            )
        )
    if result["protocolVersion"] not in SUPPORTED_PROTOCOLS:
        result["ok"] = False
        drift.append(
            _drift(
                "handshake_protocol_mismatch",
                f"protocol is {result['protocolVersion']}",
            )
        )
    if sorted(tool_names) != sorted(EXPECTED_TOOLS):
        result["ok"] = False
        drift.append(
            _drift("handshake_tools_mismatch", f"tools are {tool_names}")
        )
    return result


def _expand_clients(values: Sequence[str]) -> List[str]:
    requested: List[str] = []
    for value in values:
        names = CLIENTS if value == "all" else (value,)
        for name in names:
            if name not in requested:
                requested.append(name)
    return requested


def _collect_report(
    home: Path,
    project_dir: Path,
    clients: Sequence[str],
    scope: str,
    timeout: float,
    skip_handshake: bool,
    skill_dirs: Sequence[Path],
    expect_sha256: Optional[str],
) -> Dict[str, Any]:
    drift: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []
    version = _collect_versions(drift, warnings)
    core, core_sha = _collect_core(expect_sha256, drift)
    configs = _collect_configs(
        home,
        project_dir,
        clients,
        scope,
        timeout,
        drift,
        warnings,
    )
    for config in configs:
        probe = config.get("probe") or {}
        if config["status"] != "ok":
            continue
        if probe.get("version") and probe["version"] != __version__:
            config["status"] = "stale"
            drift.append(
                _drift(
                    "config_server_stale",
                    f"{config['path']}: configured server is not the current release",
                    path=config["path"],
                    version=probe.get("version"),
                )
            )
        elif (
            probe.get("coreSha256")
            and core_sha
            and probe["coreSha256"] != core_sha
        ):
            config["status"] = "stale"
            drift.append(
                _drift(
                    "config_core_mismatch",
                    f"{config['path']}: configured core differs from this doctor",
                    path=config["path"],
                    coreSha256=probe.get("coreSha256"),
                )
            )
    skill_copies = _collect_skill_copies(
        home,
        project_dir,
        skill_dirs,
        core_sha,
        drift,
        warnings,
    )
    handshake = _collect_handshake(timeout, skip_handshake, drift)
    return {
        "ok": not drift,
        "command": "doctor",
        "version": version,
        "core": core,
        "configs": configs,
        "skillCopies": skill_copies,
        "handshake": handshake,
        "drift": drift,
        "warnings": warnings,
    }


def _core_only(args: argparse.Namespace) -> int:
    if not CORE_PATH.is_file():
        report = {
            "ok": False,
            "command": "doctor",
            "version": __version__,
            "error": f"core script is missing: {CORE_PATH}",
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False))
        else:
            print(report["error"], file=sys.stderr)
        return 2
    report = {
        "ok": True,
        "command": "doctor",
        "version": __version__,
        "core": {"path": str(CORE_PATH), "sha256": _sha256(CORE_PATH)},
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(report["core"]["sha256"])
    return 0


def _print_human(report: Dict[str, Any]) -> None:
    version = report["version"]
    core = report["core"]
    print(f"safe-edit-mcp doctor {version['module']}")
    print(f"package version: {version['package']}")
    print(f"plugin version: {version['plugin']}")
    print(f"core: {core['siblingSha256']} {core['siblingPath']}")
    if core["shadowed"]:
        print(f"core shadowed by: {core['importedPath']}")
    for config in report["configs"]:
        print(
            f"config {config['status']}: {config['path']} "
            f"({', '.join(config['clients'])})"
        )
    for copy in report["skillCopies"]:
        if copy["exists"]:
            state = "current" if copy["matchesCurrent"] else "stale"
            print(f"skill {state}: {copy['path']}")
    handshake = report["handshake"]
    if handshake["skipped"]:
        print("handshake: skipped")
    elif handshake["ok"]:
        print(
            f"handshake: ok {handshake['serverName']} "
            f"{handshake['serverVersion']} {handshake['protocolVersion']}"
        )
    else:
        print(f"handshake: failed {handshake['error']}")
    for item in report["warnings"]:
        print(f"warning: {item['code']}: {item['message']}")
    for item in report["drift"]:
        print(f"drift: {item['code']}: {item['message']}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="safe-edit-mcp doctor",
        description=(
            "Report package/core hashes, configured MCP targets, and a live "
            "stdio handshake."
        ),
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--client",
        action="append",
        choices=(*CLIENTS, "all"),
        default=None,
    )
    parser.add_argument(
        "--scope",
        choices=("all", "user", "project"),
        default="all",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--no-handshake", action="store_true")
    parser.add_argument("--core-sha256", action="store_true")
    parser.add_argument("--skill-dir", action="append", type=Path, default=None)
    parser.add_argument("--expect-core-sha256", default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.core_sha256:
        return _core_only(args)
    if args.expect_core_sha256 is not None and not re.fullmatch(
        r"[0-9a-fA-F]{64}", args.expect_core_sha256
    ):
        parser.error("--expect-core-sha256 must be 64 hex characters")
    if args.timeout <= 0 or args.timeout > 120:
        parser.error("--timeout must be greater than 0 and at most 120 seconds")
    home = args.home.expanduser().resolve()
    project_dir = args.project_dir.expanduser().resolve()
    clients = _expand_clients(args.client or ["all"])
    skill_dirs = list(args.skill_dir or [])
    try:
        report = _collect_report(
            home,
            project_dir,
            clients,
            args.scope,
            args.timeout,
            args.no_handshake,
            skill_dirs,
            args.expect_core_sha256.lower()
            if args.expect_core_sha256
            else None,
        )
    except Exception as error:
        report = {
            "ok": False,
            "command": "doctor",
            "version": {
                "module": __version__,
                "package": None,
                "plugin": None,
                "consistent": False,
            },
            "error": str(error),
            "drift": [_drift("doctor_failed", str(error))],
            "warnings": [],
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"safe-edit-mcp doctor: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
