import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "calculator-test",
    host="127.0.0.1",
    port=int(os.environ.get("MCP_TEST_PORT", "8000")),
)


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


if __name__ == "__main__":
    if os.environ.get("MCP_TEST_TRANSPORT", "stdio") == "streamable-http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
