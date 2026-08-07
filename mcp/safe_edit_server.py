#!/usr/bin/env python3
"""Repository launcher for the packaged safe-edit MCP server."""

import os
import sys


SKILL_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
    "skills",
    "safe-edit",
)
if SKILL_ROOT not in sys.path:
    sys.path.insert(0, SKILL_ROOT)

if __name__ == "__main__":
    from safe_edit_mcp.cli import mcp_main

    raise SystemExit(mcp_main())

from safe_edit_mcp.server import (  # noqa: E402,F401
    core,
    execute_tool,
    handle_message,
    main,
    serve,
)
