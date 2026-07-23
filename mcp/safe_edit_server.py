#!/usr/bin/env python3
"""Repository launcher for the packaged safe-edit MCP server."""

import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "safe-edit"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from safe_edit_mcp.server import (  # noqa: E402,F401
    core,
    execute_tool,
    handle_message,
    main,
    serve,
)


if __name__ == "__main__":
    raise SystemExit(main())
