"""Official Python MCP SDK stdio adapter for fixed-profile memory tools."""

import json

from .retry import safe_error
from .standalone import call_tool, open_runtime
from .tools import schemas


def create_server(runtime):
    """Bind the four fixed-scope tools to an official SDK server instance."""
    import anyio
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

    async def list_tools():
        """Advertise only the shared explicit-memory schemas."""
        # Both SDKs require an async handler; allow cancellation before building schemas.
        await anyio.lowlevel.checkpoint()
        return [
            Tool(name=item["name"], description=item["description"], inputSchema=item["parameters"])
            for item in schemas()
        ]

    async def invoke(name, arguments):
        """Validate and dispatch without echoing sensitive invalid arguments."""
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

    if hasattr(Server, "list_tools"):
        # SDK 1.x registers handlers with decorators.
        server = Server("qdrant-memory")
        server.list_tools()(list_tools)
        server.call_tool(validate_input=False)(invoke)
        return server

    async def on_list_tools(context, params):
        """Adapt SDK 2.x request handlers to the shared tool catalogue."""
        return ListToolsResult(tools=await list_tools())

    async def on_call_tool(context, params):
        """Keep validation and sanitized tool errors identical across SDK versions."""
        return await invoke(params.name, params.arguments)

    return Server("qdrant-memory", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def serve(home):
    """Hold the shared writer lease throughout one local stdio server session."""
    import anyio
    from mcp.server.stdio import stdio_server

    with open_runtime(home) as runtime:
        server = create_server(runtime)

        async def run():
            """Run the SDK protocol loop until the local stdio session ends."""
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())

        anyio.run(run)
