"""The app's tools as Strands tools.

Each tool keeps its JSON schema, argument validation, read-only database access and circuit breaker from
agent/tools.py. The per-request ToolContext and the event list arrive through Strands' invocation_state,
so concurrent fans never share state.
"""

import time

from strands.tools.tools import PythonAgentTool

from ..agent.tools import TOOLS, run_tool


def strands_tool(name: str, agent_name: str) -> PythonAgentTool:
    tool = TOOLS[name]

    async def call(tool_use, **state):
        t0 = time.monotonic()
        # Small models pass null for arguments they mean to leave out ("season": null): drop those.
        args = {k: v for k, v in (tool_use.get("input") or {}).items() if v not in (None, "", "null")}
        result, ok = await run_tool(state["ctx"], name, args)
        state["events"].append({"type": "tool", "agent": agent_name, "name": name, "args": args,
                                "ok": ok, "ms": round((time.monotonic() - t0) * 1000), "chosen_by": "llm"})
        return {"toolUseId": tool_use["toolUseId"], "status": "success" if ok else "error", "content": [{"text": result}]}

    spec = {"name": name, "description": tool.description, "inputSchema": {"json": tool.parameters}}
    return PythonAgentTool(name, spec, call)


def strands_tools(names: list[str], agent_name: str) -> list[PythonAgentTool]:
    return [strands_tool(n, agent_name) for n in names]
