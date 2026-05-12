from typing import Any, Optional
from mcp.types import ToolAnnotations
from . import mcp

_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

_data_store = None


def _get_data_store():
    global _data_store
    if _data_store is None:
        try:
            from mt5_mcp.services.data_store import DataStore

            _data_store = DataStore()
        except Exception as e:
            _data_store = None
            return None
    return _data_store


@mcp.tool(annotations=_READ_ANNOTATIONS)
def mt5_data_import(
    data_type: str,
    format: str,
    content: str,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> dict:
    """Import bars, ticks, or deals data into the local DataStore cache.

    Use this to populate the local data cache with historical market data for
    offline analysis. Data is stored locally and queried by mt5_data_bars,
    mt5_data_ticks, mt5_data_deals. Supports CSV and JSON import formats.

    Args:
        data_type (str): Type of data to import. One of: "bars", "ticks",
            "deals". Determines which cache table is populated.
        format (str): Import format. One of: "csv", "json". Note: JSON ticks
            and CSV deals import are not yet supported.
        content (str): Raw data content as a string. For CSV: comma-separated
            with header row. For JSON: JSON array of objects.
        symbol (Optional[str]): Symbol associated with the data (required for
            CSV bars/ticks import). (default: None)
        timeframe (Optional[str]): Timeframe for bars data (e.g. "M1", "H1",
            "D1"). Required for CSV bars import. (default: None)

    Returns:
        dict: Import result with keys:
            - imported (int): Number of new records successfully imported
            - duplicates_skipped (int): Number of duplicate records skipped
            - errors (list, present on failure): List of error messages
            - error (str, present on failure): Error description

    Example: mt5_data_import(data_type="bars", format="csv",
        content="timestamp,open,high,low,close,volume\n...",
        symbol="EURUSD", timeframe="H1")

    Note: This tool writes to a LOCAL in-memory or Redis-backed cache, NOT
    directly to MT5. Data persists for the lifetime of the server process.
    Imported data may be out of sync with live MT5 data — always verify
    freshness before making trading decisions.
    """
    try:
        store = _get_data_store()
        if store is None:
            return {"error": "Data store unavailable"}
        if data_type == "bars" and format == "csv":
            result = store.import_bars_csv(content, symbol, timeframe)
            return {
                "imported": result.get("imported", 0),
                "duplicates_skipped": result.get("duplicates_skipped", 0),
            }
        if data_type == "bars" and format == "json":
            result = store.import_bars_json(content)
            return {
                "imported": result.get("imported", 0),
                "duplicates_skipped": result.get("duplicates_skipped", 0),
            }
        if data_type == "ticks" and format == "csv":
            result = store.import_ticks_csv(content, symbol)
            return {
                "imported": result.get("imported", 0),
                "duplicates_skipped": result.get("duplicates_skipped", 0),
            }
        if data_type == "ticks" and format == "json":
            return {"error": "JSON ticks import not yet supported"}
        if data_type == "deals" and format == "json":
            result = store.import_deals_json(content)
            return {
                "imported": result.get("imported", 0),
                "duplicates_skipped": result.get("duplicates_skipped", 0),
            }
        if data_type == "deals" and format == "csv":
            return {"error": "CSV deals import not yet supported"}
        return {
            "error": f"Unsupported data_type/format combination: {data_type}/{format}"
        }
    except Exception as e:
        return {
            "error": str(e),
            "imported": 0,
            "duplicates_skipped": 0,
            "errors": [str(e)],
        }


@mcp.tool(annotations=_READ_ANNOTATIONS)
def mt5_data_bars(
    symbol: str,
    timeframe: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """Query historical OHLCV bars from the local DataStore cache.

    Use this for backtesting, technical indicator calculation, and market
    structure analysis. Data must first be imported via mt5_data_import or
    populated by the background sync service.

    Args:
        symbol (str): Trading symbol to query (e.g. "EURUSD").
        timeframe (Optional[str]): Bar timeframe. Common values: "M1", "M5",
            "M15", "M30", "H1", "H4", "D1", "W1", "MN". (default: None, uses
            whatever is available in the store)
        start_time (Optional[str]): Filter start time (ISO 8601 format, e.g.
            "2024-01-01T00:00:00Z"). (default: None, no lower bound)
        end_time (Optional[str]): Filter end time (ISO 8601 format).
            (default: None, no upper bound)
        limit (int): Maximum number of bars to return (default: 100,
            range: 1-100000). Higher values increase response time.

    Returns:
        dict: Query result with keys:
            - data (list): Array of bar dicts, each with:
                - timestamp (str): Bar open time (ISO 8601)
                - open (float): Open price
                - high (float): High price
                - low (float): Low price
                - close (float): Close price
                - volume (float): Tick volume
                - spread (int, optional): Average spread in points
                - real_volume (int, optional): Real volume if available
            - error (str, present on failure): Error description

    Example: mt5_data_bars(symbol="EURUSD", timeframe="H1", limit=50)

    Note: Queries a LOCAL cache (DataStore), NOT live MT5 data. Results may
    be stale if the cache has not been recently synced. Use mt5_symbol_info
    for live price data. Returns {"data": []} if no cached data for symbol.
    """
    try:
        store = _get_data_store()
        if store is None:
            return {"error": "Data store unavailable", "data": []}
        result = store.query_bars(symbol, timeframe, start_time, end_time, limit)
        return {"data": result}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def mt5_data_ticks(
    symbol: str,
    start_time_ms: Optional[int] = None,
    end_time_ms: Optional[int] = None,
    limit: int = 100,
) -> dict:
    """Query tick-level price data from the local DataStore cache.

    Use this for high-frequency analysis, spread calculation, and tick volume
    profiling. Tick data must first be imported via mt5_data_import.

    Args:
        symbol (str): Trading symbol to query (e.g. "EURUSD").
        start_time_ms (Optional[int]): Filter start time as Unix timestamp in
            milliseconds. (default: None, no lower bound)
        end_time_ms (Optional[int]): Filter end time as Unix timestamp in
            milliseconds. (default: None, no upper bound)
        limit (int): Maximum number of ticks to return (default: 100,
            range: 1-100000). Higher values increase response time and memory
            usage.

    Returns:
        dict: Query result with keys:
            - data (list): Array of tick dicts, each with:
                - timestamp_ms (int): Tick time as Unix timestamp (ms)
                - bid (float): Bid price
                - ask (float): Ask price
                - last (float, optional): Last trade price
                - volume (float, optional): Tick volume
                - flags (int, optional): Tick flags bitmask
            - error (str, present on failure): Error description

    Example: mt5_data_ticks(symbol="EURUSD", limit=1000)

    Note: Queries a LOCAL cache (DataStore), NOT live MT5 data. For real-time
    prices use mt5_symbol_info or mt5_account_summary. Returns {"data": []}
    if no cached ticks for the given symbol. Millisecond timestamps are Unix
    time, not Python datetime.
    """
    try:
        store = _get_data_store()
        if store is None:
            return {"error": "Data store unavailable", "data": []}
        result = store.query_ticks(symbol, start_time_ms, end_time_ms, limit)
        return {"data": result}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def mt5_data_deals(symbol: Optional[str] = None, limit: int = 100) -> dict:
    """Query historical deal records from the local DataStore cache.

    Use this to review past trade execution data stored locally. Unlike
    mt5_deals_history (which queries MT5 live), this reads from the local
    cache for faster repeated access. Useful for post-session analysis.

    Args:
        symbol (Optional[str]): Filter by trading symbol (e.g. "EURUSD").
            None returns deals for all symbols. (default: None)
        limit (int): Maximum number of deals to return (default: 100,
            range: 1-10000).

    Returns:
        dict: Query result with keys:
            - data (list): Array of deal dicts (structure matches MT5 deal
              records with keys like deal_id, order_id, symbol, side, volume,
              price, profit, commission, swap, time, comment, entry).
            - error (str, present on failure): Error description

    Example: mt5_data_deals(symbol="EURUSD", limit=50)

    Note: Queries a LOCAL cache (DataStore), NOT MT5 directly. The cache may
    be out of sync with MT5 if not recently populated by mt5_data_import or
    background sync. For live deal history, use mt5_deals_history instead.
    Returns {"data": []} if no cached deals.
    """
    try:
        store = _get_data_store()
        if store is None:
            return {"error": "Data store unavailable", "data": []}
        result = store.query_deals(symbol, limit)
        return {"data": result}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def mt5_data_stats() -> dict:
    """Get summary statistics about the local DataStore cache contents.

    Use this to check what data is available in the local cache — see which
    symbols have cached bars, ticks, or deals, date ranges covered, and total
    record counts. Helps determine if mt5_data_import is needed before
    analysis.

    Args:
        None

    Returns:
        dict: Store statistics with keys:
            - total_bars (int): Total number of bar records cached
            - total_ticks (int): Total number of tick records cached
            - total_deals (int): Total number of deal records cached
            - symbols (list): List of symbols present in the cache
            - date_range (dict, optional): Min/max timestamps for each
              data type
            - storage_backend (str): "memory" or "redis"
            - error (str, present on failure): Error description

    Example: mt5_data_stats()

    Note: Reports on the LOCAL cache (DataStore) only — not a substitute for
    mt5_terminal_status or mt5_bridge_status. Returns {"error": "Data store
    unavailable"} if the DataStore is not initialized.
    """
    try:
        store = _get_data_store()
        if store is None:
            return {"error": "Data store unavailable"}
        return store.get_stats()
    except Exception as e:
        return {"error": str(e)}
