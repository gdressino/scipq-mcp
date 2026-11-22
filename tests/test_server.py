import sys

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

EXPECTED_TOOLS = {"setup", "find_symbols", "references", "definition", "source", "impact",
                  "path", "changed", "deps", "dead_code", "census", "cache_status",
                  "implementations", "supertypes", "members", "family", "overloads", "outline",
                  "callers", "callees", "annotated", "typed", "tests", "index_status", "batch"}


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
                assert "8 references in 5 files" in text
                res = await session.call_tool("find_symbols", {"name": "setState"})
                assert "2 definitions" in res.content[0].text
                res = await session.call_tool("batch", {"queries": [
                    {"tool": "typed", "args": {"symbol": "Entity", "categories": ["field"]}},
                    {"tool": "source", "args": {"symbol": "Entity#setState"}}]})
                text = res.content[0].text
                assert "field 2" in text and "this.state = state;" in text
                prompts = await session.list_prompts()
                assert "setup" in {p.name for p in prompts.prompts}

    anyio.run(run)


def _tools(args, cwd=None):
    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "scipq_mcp", *args],
                                       cwd=cwd)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                init = await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                info = getattr(init, "server_info", None) or getattr(init, "serverInfo")
                return tools, info.version

    return anyio.run(run)


def test_idle_outside_java_projects(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    tools, version = _tools(["serve"], cwd=str(tmp_path))
    assert tools == set()
    assert version
    tools, _ = _tools(["serve", "--always"], cwd=str(tmp_path))
    assert EXPECTED_TOOLS <= tools


def test_active_in_uncompiled_java_project(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    tools, _ = _tools(["serve"], cwd=str(tmp_path))
    assert "setup" in tools and "references" in tools
