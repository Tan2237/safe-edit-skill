"""Lightweight console dispatchers for safe-edit entry points."""

import sys

from . import __version__


def _arguments(argv=None):
    return list(sys.argv[1:] if argv is None else argv)


def main(argv=None):
    """Run the file-editing CLI, importing the core only when needed."""
    args = _arguments(argv)
    if args == ["--version"]:
        print(__version__)
        return 0

    import safe_edit

    return safe_edit.main(args)


def mcp_main(argv=None):
    """Dispatch MCP metadata/install commands without importing the core."""
    args = _arguments(argv)
    if args == ["--version"]:
        print(__version__)
        return 0
    if args and args[0] == "install":
        from .installer import main as install_main

        return install_main(args[1:])
    if args:
        print(
            "usage: safe-edit-mcp [--version] | "
            "safe-edit-mcp install [options]",
            file=sys.stderr,
        )
        return 2

    from .server import serve

    serve()
    return 0
