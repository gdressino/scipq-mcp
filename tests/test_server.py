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


def test_tools_selection(sample_root):
    core, _ = _tools(["serve", "--root", sample_root, "--tools", "core"], cwd=sample_root)
    assert "references" in core and "impact" not in core and "setup" in core
    some, _ = _tools(["serve", "--root", sample_root, "--tools", "impact,references"],
                     cwd=sample_root)
    assert some == {"impact", "references", "setup"}


def test_one_bad_query_does_not_sink_the_batch(sample_root):
    from scipq_mcp.service import Service
    out = Service(sample_root).batch([
        {"tool": "references", "args": {"symbol": "Entity#setState"}},
        {"tool": "callers", "args": {"symbol": "Entity#setState", "depth": "2"}},
    ])
    first, _, second = out.partition("### 2.")
    assert "references" in first and "Service.java" in first
    assert "error: TypeError" in second


def test_batch_arguments_without_the_args_wrapper(sample_root):
    from scipq_mcp.service import Service
    out = Service(sample_root).batch([
        {"tool": "source", "symbol": "Entity#setState"},
        {"tool": "source", "args": {}},
        {"tool": "callers", "symbol": "Entity#setState", "args": {"depth": 1}},
    ])
    first, _, rest = out.partition("### 2.")
    second, _, third = rest.partition("### 3.")
    assert "setState" in first and "error" not in first
    assert "error: source: missing 1 required positional argument: 'symbol' (each query is " \
        "{'tool': 'source', 'args': {…}}" in second and "_b_source" not in second
    assert "callers of Entity#setState" in third


def test_batch_runs_every_tool_but_setup(sample_root):
    import inspect
    from scipq_mcp.server import build
    from scipq_mcp.service import Service
    svc = Service(sample_root)
    tools = anyio.run(build(svc).list_tools)
    assert len(tools) > 30
    for t in tools:
        if t.name in ("setup", "batch"):
            continue
        assert t.name in Service.BATCH_TOOLS, t.name
        params = inspect.signature(getattr(svc, Service.BATCH_TOOLS[t.name])).parameters
        schema = getattr(t, "input_schema", None) or t.inputSchema
        aliases = Service.BATCH_ALIASES.get(t.name, {})
        assert {aliases.get(p, p) for p in schema["properties"]} - {"project_root"} <= \
            set(params), t.name
    out = svc.batch([{"tool": "persistence"}, {"tool": "refactor_help", "topic": "flow"},
                     {"tool": "check_refs", "format": "json"}])
    assert "unknown tool" not in out and "### 2. refactor_help" in out
    assert "TypeError" not in out


def test_batch_of_refactor_help_needs_no_shards(tmp_path):
    from scipq_mcp.service import Service
    (tmp_path / "pom.xml").write_text("<project/>")
    out = Service(str(tmp_path)).batch([{"tool": "refactor_help", "topic": "flow"}])
    assert out.startswith("### 1. refactor_help") and "error" not in out.split("\n")[1]
