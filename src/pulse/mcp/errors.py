"""Every way talking to an MCP server can fail, as types the pipeline can act on."""

from __future__ import annotations

from pulse.mcp.contract import ErrorCode


class McpClientError(Exception):
    """Base for MCP failures. Never carries a credential or review text."""


class McpUnavailable(McpClientError):
    """The server could not be reached or did not complete `initialize`."""


class McpCredentialError(McpClientError):
    """Our bearer token for the server was rejected (HTTP 401/403)."""


class McpTokenMissing(McpCredentialError):
    """The env var that should hold the server's bearer token is not set."""


class ContractMismatch(McpClientError):
    """The server is not the one, or not the version, this agent was built against."""


class MissingTool(McpClientError):
    """A configured tool is not in the server's `list_tools()`, or must never be bound."""


class McpProtocolError(McpClientError):
    """The server replied, but not in the shape the contract promises, or went away mid-call."""


class McpToolError(McpClientError):
    """A tool call failed. `code` is classified from the server's message (contract rules)."""

    def __init__(self, tool: str, code: ErrorCode, message: str) -> None:
        super().__init__(f"{tool} failed ({code.value}): {message}")
        self.tool = tool
        self.code = code
        self.server_message = message
