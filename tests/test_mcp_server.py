import asyncio
import inspect

import pytest

mcp_server = pytest.importorskip("flywizz.mcp_server")

EXPECTED_TOOLS = {
    "find_fares",
    "cheapest_day",
    "price_around",
    "explore_destinations",
    "flight_status",
}


def _list_tools():
    tools = mcp_server.mcp.list_tools()
    if inspect.isawaitable(tools):  # mcp < 2.0 exposed list_tools as async
        tools = asyncio.run(tools)
    return tools


def test_expected_tools_are_registered():
    assert {t.name for t in _list_tools()} == EXPECTED_TOOLS


def test_every_tool_is_described():
    for tool in _list_tools():
        assert tool.description, f"{tool.name} has no description"


def test_import_does_not_build_a_client():
    # Importing the module must never touch the network.
    assert mcp_server._client is None
