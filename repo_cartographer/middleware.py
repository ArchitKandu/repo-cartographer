"""Middleware that narrows the agent's tools to the ones this phase needs.

`create_deep_agent` hands the model a built-in suite alongside the tools you
pass: a workspace filesystem, a shell, and a sub-agent spawner. At Phase 2 none
of those help — the repository is on GitHub, not on disk — and offering them is
actively harmful. Measured on a `psf/requests` mapping run, the model spent 8 of
16 tool calls on `read_file`/`ls` against the empty workspace, including three
identical retries of a path that had already 404'd.

Removing them halves the requests a run costs, which matters when the free-tier
budget is counted in requests per day.

deepagents has its own version of this, reached through
`HarnessProfile(excluded_tools=...)`. It does not fire here: profiles resolve
from a `"provider:model"` string, and this project constructs its chat model as
an instance so it can point at a custom `base_url`. The mechanism is small and
entirely public API, so it lives here instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import (
        ExtendedModelResponse,
        ModelRequest,
        ModelResponse,
        ResponseT,
    )
    from langchain_core.messages import AIMessage

# The deepagents built-ins that Phase 2 has no use for. `read_file` and
# `write_file` come back the moment Phase 3 adds a filesystem backend for the
# agent to offload findings to, and `task` when Phase 4 introduces sub-agents —
# so this set shrinks as the project grows rather than staying fixed.
UNUSED_BUILTIN_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "delete",
        "execute",
        "task",
    }
)


def _tool_name(tool: Any) -> str | None:
    """Read a tool's name, whether it arrives as a `BaseTool` or a dict."""
    name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
    return name if isinstance(name, str) else None


class RestrictToolsMiddleware(AgentMiddleware[Any, Any, Any]):
    """Hide named tools from the model without unbinding them from the graph.

    The filtering happens per model request, which is the level that matters:
    the tool node still knows every tool, but the model is never told they
    exist, so it cannot call them. Pass this last in `middleware=[...]` so it
    runs after the middleware that inject the built-ins.
    """

    def __init__(self, *, excluded: frozenset[str] = UNUSED_BUILTIN_TOOLS) -> None:
        self._excluded = excluded

    def _filter(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        if not self._excluded:
            return request
        kept = [t for t in request.tools if _tool_name(t) not in self._excluded]
        return request.override(tools=kept)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._filter(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        return await handler(self._filter(request))
