"""Alpaca integration: trading client, market data, and order execution.

Nested under ``desk`` deliberately — a top-level ``alpaca`` package here would
shadow the installed ``alpaca-py`` library this module wraps.
"""

from desk.alpaca.client import AlpacaClients, PaperOnlyError, get_clients

__all__ = ["AlpacaClients", "PaperOnlyError", "get_clients"]
