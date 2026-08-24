"""The tool layer: thirteen handlers behind one dispatcher.

Importing this package registers every handler. `check_registry()` then asserts
the registry and `gate.TOOL_SPECS` agree in both directions, so a handler with
no capability-matrix entry, or a matrix entry with no handler, fails at import
in the tests rather than at the first model call.
"""

from . import handlers  # noqa: F401  — import for the side effect of registering
from .runtime import (
    REGISTRY,
    ToolDef,
    ToolResult,
    check_registry,
    dispatch,
    queue_confirmation,
    register,
    tool_schemas_for,
    validate_args,
)

__all__ = [
    "REGISTRY", "ToolDef", "ToolResult", "check_registry", "dispatch",
    "queue_confirmation", "register", "tool_schemas_for", "validate_args",
]
