"""Official Python MCP SDK stdio adapter for fixed-profile memory tools."""

import json

from .retry import safe_error
from .standalone import call_tool, open_runtime
from .tools import schemas


def serve(home):
    """Hold the shared writer lease throughout one local stdio server session."""
    import anyio
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import CallToolResult, TextContent, Tool

    server = Server("qdrant-memory")
    with open_runtime(home) as runtime:

        @server.list_tools()
        async def list_tools():
            return [
                Tool(
                    name=item["name"],
                    description=item["description"],
                    inputSchema=item["parameters"],
                )
                for item in schemas()
            ]

        @server.call_tool(validate_input=False)
        async def invoke(name, arguments):
            try:
                result = await anyio.to_thread.run_sync(lambda: call_tool(runtime, name, arguments))
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                    isError=False,
                )
            except Exception as exc:
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps({"error": safe_error(exc)}))],
                    isError=True,
                )

        async def run():
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())

        anyio.run(run)
