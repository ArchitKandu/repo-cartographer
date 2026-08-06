"""Middleware that narrows the agent's tools to the ones this phase needs.

`create_deep_agent` hands the model a built-in suite alongside the tools you
pass: a workspace filesystem, a shell, and a sub-agent spawner. Not all of it is
wanted at once, and an unwanted tool is not free — it is a line in every system
prompt and an option the model can waste a turn on. Measured on a `psf/requests`
mapping run at Phase 2, when the workspace was empty and unused, the model spent
8 of 16 tool calls on `read_file`/`ls` against it, including three identical
retries of a path that had already 404'd. Removing them halved the requests a
run cost, which matters when the free-tier budget is counted in requests per day.

Phase 3 gave the workspace an actual purpose, so the file tools came back and the
exclusion list shrank to what was still unused. Phase 4 shows the set is not
monotonic: `task` leaves it, because sub-agents are the whole point of the phase,
while `write_file` and `edit_file` join it, because the orchestrator stopped
writing — its explorers write and its doc-writer reads, and it only looks. The
set tracks *this* agent's responsibilities, and those move.

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

# The deepagents built-ins the *orchestrator* has no use for as of Phase 4. Note
# the scope: this middleware runs on the main agent only, so nothing here is
# hidden from the explorer or the doc-writer. Their tool sets are narrowed a
# different way — `FilesystemMiddleware(tools=[...])` inside each spec — because
# a parent's middleware is not inherited by declarative sub-agents.
UNUSED_BUILTIN_TOOLS = frozenset(
    {
        # Search over a workspace holding notes the job's own agents wrote, at
        # paths the orchestrator chose. There is nothing here it cannot already
        # find with `ls`.
        "glob",
        "grep",
        # Notes are the durable record of a run, and the one agent that could
        # delete them has no reason to.
        "delete",
        # No sandbox backend, so this returns an error string rather than running
        # anything. Offering a tool that cannot work only invites a wasted turn.
        "execute",
        # Phase 4 moved writing out of the orchestrator: the explorer writes its
        # notes, the doc-writer returns prose, and this agent only checks that the
        # notes arrived. A write tool it never needs is a write tool it can waste
        # a turn on — and an orchestrator that can edit its delegates' findings
        # can quietly launder them, which defeats the point of delegating.
        "write_file",
        "edit_file",
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
