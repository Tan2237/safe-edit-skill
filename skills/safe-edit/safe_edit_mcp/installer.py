"""Install safe-edit's stdio MCP server into supported AI clients."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


CLIENTS = ("claude-code", "cursor", "opencode", "vscode")
SERVER_KEY = "safe-edit"


class InstallError(Exception):
    """Raised before a client configuration is changed."""


@dataclass(frozen=True)
class Invocation:
    command: str
    args: Tuple[str, ...] = ()


@dataclass
class FileAction:
    client: str
    path: Path
    content: bytes
    changed: bool
    original: Optional[bytes]


@dataclass(frozen=True)
class CommandAction:
    client: str
    argv: Tuple[str, ...]


def _server_invocation() -> Invocation:
    override = os.environ.get("SAFE_EDIT_MCP_EXECUTABLE")
    if override:
        return Invocation(str(Path(override).expanduser().resolve()))
    launcher = shutil.which("safe-edit-mcp")
    if launcher:
        return Invocation(str(Path(launcher).resolve()))
    source_root = Path(__file__).resolve().parents[3]
    repository_launcher = source_root / "mcp" / "safe_edit_server.py"
    if repository_launcher.is_file():
        return Invocation(
            sys.executable,
            (str(repository_launcher.resolve()),),
        )
    return Invocation(sys.executable, ("-m", "safe_edit_mcp.server"))


def _server_entry(client: str, invocation: Invocation) -> Dict[str, Any]:
    if client == "opencode":
        return {
            "type": "local",
            "command": [invocation.command, *invocation.args],
            "enabled": True,
        }
    entry: Dict[str, Any] = {
        "command": invocation.command,
        "args": list(invocation.args),
    }
    if client == "vscode":
        entry["type"] = "stdio"
    return entry


def _target_path(
    client: str, scope: str, home: Path, project_dir: Path
) -> Optional[Path]:
    if scope == "project":
        return {
            "claude-code": project_dir / ".mcp.json",
            "cursor": project_dir / ".cursor" / "mcp.json",
            "opencode": project_dir / "opencode.json",
            "vscode": project_dir / ".vscode" / "mcp.json",
        }[client]
    if client == "vscode":
        return None
    return {
        "claude-code": home / ".claude.json",
        "cursor": home / ".cursor" / "mcp.json",
        "opencode": home / ".config" / "opencode" / "opencode.json",
    }[client]


def _section_name(client: str) -> str:
    if client == "opencode":
        return "mcp"
    if client == "vscode":
        return "servers"
    return "mcpServers"


def _load_object(path: Path) -> Tuple[Dict[str, Any], Optional[bytes]]:
    if not path.exists():
        return {}, None
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise InstallError(f"cannot read {path}: {error}") from error
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstallError(
            f"refusing to rewrite non-JSON config {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise InstallError(f"config root must be an object: {path}")
    return value, raw


def _render_config(
    client: str, path: Path, invocation: Invocation
) -> FileAction:
    root, original = _load_object(path)
    section_name = _section_name(client)
    section = root.get(section_name, {})
    if not isinstance(section, dict):
        raise InstallError(
            f"{section_name} must be an object in {path}"
        )
    updated_section = dict(section)
    updated_section[SERVER_KEY] = _server_entry(client, invocation)
    updated = dict(root)
    updated[section_name] = updated_section
    content = (
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    return FileAction(
        client=client,
        path=path,
        content=content,
        changed=content != original,
        original=original,
    )


def _next_backup(path: Path) -> Path:
    candidate = path.with_name(path.name + ".safe-edit.bak")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(
            f"{path.name}.safe-edit.bak.{counter}"
        )
        counter += 1
    return candidate


def _write_atomic(action: FileAction) -> Optional[Path]:
    if not action.changed:
        return None
    action.path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if action.original is not None:
        backup = _next_backup(action.path)
        shutil.copy2(str(action.path), str(backup))
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{action.path.name}.",
        suffix=".tmp",
        dir=str(action.path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(action.content)
            stream.flush()
            os.fsync(stream.fileno())
        if action.path.exists():
            os.chmod(temporary, action.path.stat().st_mode)
        os.replace(str(temporary), str(action.path))
    finally:
        if temporary.exists():
            temporary.unlink()
    return backup


def _restore_file(action: FileAction) -> None:
    if action.original is None:
        action.path.unlink(missing_ok=True)
        return
    rollback = FileAction(
        client=action.client,
        path=action.path,
        content=action.original,
        changed=True,
        original=None,
    )
    _write_atomic(rollback)


def _vscode_user_action(
    invocation: Invocation, code_cli: Optional[str]
) -> CommandAction:
    executable = code_cli or shutil.which("code")
    if not executable:
        raise InstallError(
            "VS Code user install requires the 'code' CLI; "
            "use --scope project or enable the shell command first"
        )
    payload = {
        "name": SERVER_KEY,
        "type": "stdio",
        "command": invocation.command,
        "args": list(invocation.args),
    }
    return CommandAction(
        client="vscode",
        argv=(
            executable,
            "--add-mcp",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ),
    )


def _expand_clients(values: Iterable[str]) -> List[str]:
    requested: List[str] = []
    for value in values:
        names = CLIENTS if value == "all" else (value,)
        for name in names:
            if name not in requested:
                requested.append(name)
    return requested


def install_configs(
    clients: Sequence[str],
    scope: str,
    home: Path,
    project_dir: Path,
    dry_run: bool = False,
    invocation: Optional[Invocation] = None,
    code_cli: Optional[str] = None,
) -> Dict[str, Any]:
    selected = _expand_clients(clients)
    server = invocation or _server_invocation()
    files: List[FileAction] = []
    commands: List[CommandAction] = []

    for client in selected:
        if client not in CLIENTS:
            raise InstallError(f"unsupported client: {client}")
        target = _target_path(client, scope, home, project_dir)
        if target is None:
            commands.append(_vscode_user_action(server, code_cli))
        else:
            files.append(_render_config(client, target, server))

    results: List[Dict[str, Any]] = []
    applied: List[FileAction] = []
    try:
        for action in files:
            backup = None if dry_run else _write_atomic(action)
            if not dry_run and action.changed:
                applied.append(action)
            results.append(
                {
                    "client": action.client,
                    "kind": "file",
                    "path": str(action.path),
                    "changed": action.changed,
                    "backup": str(backup) if backup else None,
                    "config": json.loads(action.content.decode("utf-8")),
                }
            )

        for action in commands:
            if not dry_run:
                subprocess.run(list(action.argv), check=True)
            results.append(
                {
                    "client": action.client,
                    "kind": "command",
                    "argv": list(action.argv),
                    "changed": not dry_run,
                    "backup": None,
                }
            )
    except (OSError, subprocess.CalledProcessError) as error:
        for action in reversed(applied):
            try:
                _restore_file(action)
            except OSError:
                pass
        raise InstallError(
            f"configuration failed; completed file writes were rolled back: "
            f"{error}"
        ) from error

    return {
        "ok": True,
        "dryRun": dry_run,
        "scope": scope,
        "server": {
            "command": server.command,
            "args": list(server.args),
        },
        "results": results,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="safe-edit-mcp install",
        description=(
            "Register the persistent safe-edit stdio MCP server without "
            "replacing unrelated client configuration."
        ),
    )
    parser.add_argument(
        "--client",
        action="append",
        required=True,
        choices=(*CLIENTS, "all"),
        help="client to configure; repeat or use 'all'",
    )
    parser.add_argument(
        "--scope",
        choices=("user", "project"),
        default="user",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
    )
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        summary = install_configs(
            clients=args.client,
            scope=args.scope,
            home=args.home.expanduser().resolve(),
            project_dir=args.project_dir.expanduser().resolve(),
            dry_run=args.dry_run,
        )
    except InstallError as error:
        if args.json:
            print(json.dumps({"ok": False, "error": str(error)}))
        else:
            print(f"safe-edit-mcp: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        verb = "would configure" if args.dry_run else "configured"
        for result in summary["results"]:
            destination = result.get("path") or " ".join(result["argv"])
            state = verb if result["changed"] else "already configured"
            print(f"{result['client']}: {state} {destination}")
            if result.get("backup"):
                print(f"  backup: {result['backup']}")
    return 0
