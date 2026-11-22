import sys

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

EXPECTED_TOOLS = {"find_symbols", "references", "definition", "implementations", "supertypes",
                  "outline", "callers", "callees"}


def test_stdio_server(sample_root):
    async def run():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "scipq_mcp", "serve", "--root", sample_root])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                assert EXPECTED_TOOLS <= tools
                res = await session.call_tool("references", {"symbol": "Entity#setState"})
                text = res.content[0].text
                assert "7 references in 4 files" in text
                res = await session.call_tool("find_symbols", {"name": "setState"})
                assert "2 definitions" in res.content[0].text

    anyio.run(run)
