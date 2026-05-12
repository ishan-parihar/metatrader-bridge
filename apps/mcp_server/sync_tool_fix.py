"""Patch FastMCP to run sync tools in a thread pool.

Root cause: FastMCP calls sync tools directly in the async event loop.
Our sync tools block via future.result() on TCP calls, which blocks the
entire event loop including stdio transport I/O. The MCP client times out
with -32001 because no response can be written to stdout.

Fix: Monkey-patch FuncMetadata.call_fn_with_arg_validation to run sync
functions via asyncio.to_thread(), preventing event loop blocking.
This preserves the original argument validation (pre_parse_json,
model_validate, model_dump) and only wraps the sync function call itself
in a thread pool executor.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata

_original_call = FuncMetadata.call_fn_with_arg_validation


async def _patched_call_fn_with_arg_validation(
    self: FuncMetadata,
    fn: Callable[..., Any],
    fn_is_async: bool,
    arguments_to_validate: dict[str, Any],
    arguments_to_pass_directly: dict[str, Any] | None,
) -> Any:
    # Always perform the full validation (same as original FastMCP code).
    # This normalizes types, applies defaults, and handles JSON pre-parsing
    # before the function is ever called.
    arguments_pre_parsed = self.pre_parse_json(arguments_to_validate)
    arguments_parsed_model = self.arg_model.model_validate(arguments_pre_parsed)
    arguments_parsed_dict = arguments_parsed_model.model_dump_one_level()
    arguments_parsed_dict |= arguments_to_pass_directly or {}

    if fn_is_async:
        return await fn(**arguments_parsed_dict)

    # Run sync function in thread pool to avoid blocking the event loop.
    # Without this, a sync fn that calls future.result() stalls the entire
    # asyncio event loop, preventing SSE transport from writing responses.
    return await asyncio.to_thread(fn, **arguments_parsed_dict)


FuncMetadata.call_fn_with_arg_validation = _patched_call_fn_with_arg_validation
