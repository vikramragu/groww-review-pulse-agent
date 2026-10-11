"""Groww Review Pulse — the agent half of the Review Pulse system.

Converts public Play Store reviews of the Groww app into a weekly one-page pulse, then
publishes it to Google Docs and creates a Gmail draft by calling MCP servers it does not
own. See docs/architecture.md for the design and docs/implementation-plan.md for the build
order.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
