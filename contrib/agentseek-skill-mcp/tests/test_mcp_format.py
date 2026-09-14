from __future__ import annotations

from agentseek_skill_mcp.loader import _convert_to_mcp_servers_format


def test_stdio_format() -> None:
    sdk_config = {
        "tavily": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "tavily-mcp"],
            "env": {"KEY": "val"},
        }
    }
    result = _convert_to_mcp_servers_format(sdk_config)
    assert result == {
        "mcpServers": {
            "tavily": {
                "command": "npx",
                "args": ["-y", "tavily-mcp"],
                "env": {"KEY": "val"},
            }
        }
    }


def test_http_format() -> None:
    sdk_config = {
        "remote": {
            "transport": "http",
            "url": "https://mcp.example.com/sse",
        }
    }
    result = _convert_to_mcp_servers_format(sdk_config)
    assert result == {
        "mcpServers": {
            "remote": {
                "url": "https://mcp.example.com/sse",
            }
        }
    }


def test_empty_input() -> None:
    result = _convert_to_mcp_servers_format({})
    assert result == {"mcpServers": {}}


def test_multiple_servers() -> None:
    sdk_config = {
        "tavily": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "tavily-mcp"],
        },
        "remote": {
            "transport": "http",
            "url": "https://mcp.example.com/sse",
        },
    }
    result = _convert_to_mcp_servers_format(sdk_config)
    assert set(result["mcpServers"].keys()) == {"tavily", "remote"}


def test_transport_key_removed() -> None:
    sdk_config = {
        "a": {"transport": "stdio", "command": "node"},
        "b": {"transport": "http", "url": "https://x.example.com"},
    }
    result = _convert_to_mcp_servers_format(sdk_config)
    for server_config in result["mcpServers"].values():
        assert "transport" not in server_config
