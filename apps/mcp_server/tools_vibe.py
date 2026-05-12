

from mcp.types import ToolAnnotations

from . import mcp
from apps.vibe_bridge import tools as vibe_tools

_VIBE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


@mcp.tool(name="vibe_list_skills", annotations=_VIBE_ANNOTATIONS)
async def vibe_list_skills() -> str:
    """List all Vibe-Trading finance skills (69 skills across 7 categories).

    Use this to discover available financial analysis capabilities before
    delegating work to Vibe-Trading agents. Helps decide which swarm preset
    or market data function to invoke.

    Returns:
        str: A formatted list of all 69 skills organized by 7 categories
            (trading strategy, risk management, market analysis, etc.).

    Example: vibe_list_skills()

    Note: Requires the external Vibe-Trading MCP server process to be
    running. If the server is unavailable, returns an error message.
    """
    return await vibe_tools.vibe_list_skills()


@mcp.tool(name="vibe_get_market_data", annotations=_VIBE_ANNOTATIONS)
async def vibe_get_market_data(
    codes: list[str],
    start_date: str,
    end_date: str,
    source: str = "auto",
    interval: str = "1D",
) -> str:
    """Fetch cross-market data via Vibe-Trading covering A-shares, HK/US equities, crypto, futures, and forex.

    Use for pre-trade analysis before MT5 execution. Provides multi-source
    market data when you need OHLCV, price history, or cross-market context
    that goes beyond what MT5's terminal offers.

    Args:
        codes (list[str]): Asset codes to fetch (e.g., ["000001.SZ", "BTC-USD"]).
            Supports multiple codes in a single call.
        start_date (str): Start date in YYYY-MM-DD format. Must be earlier
            than end_date.
        end_date (str): End date in YYYY-MM-DD format. Must be later than
            start_date.
        source (str): Data source override (default: "auto"). Options include
            "auto", "akshare", "yahoo", "eastmoney". Auto selects based on
            code pattern.
        interval (str): Bar interval (default: "1D"). Accepted values:
            "1m", "5m", "15m", "30m", "60m", "1D", "1W", "1M".

    Returns:
        str: Formatted market data table with OHLCV columns and date index.
            Structure varies by asset type.

    Example: vibe_get_market_data(["000001.SZ", "BTC-USD"], "2024-01-01", "2024-01-31")

    Note: Requires the external Vibe-Trading MCP server process to be
    running. Data availability depends on the selected source. Some
    sources may have rate limits or require API keys configured on the
    Vibe-Trading side.
    """
    return await vibe_tools.vibe_get_market_data(codes, start_date, end_date, source, interval)


@mcp.tool(name="vibe_run_swarm", annotations=_VIBE_ANNOTATIONS)
async def vibe_run_swarm(preset: str, variables: dict[str, str]) -> str:
    """Run a Vibe-Trading multi-agent swarm team for collaborative market analysis.

    Use when you need AI agents to debate, analyze, and reach consensus on
    market conditions. Each preset simulates a team of specialists (e.g.,
    investment_committee acts as fund managers debating allocation).

    Presets: investment_committee, crypto_trading_desk, quant_strategy_desk,
    risk_committee, global_allocation_committee.

    Args:
        preset (str): Swarm team preset name. Must be one of the documented
            presets. Invalid presets return an error.
        variables (dict[str, str]): Key-value pairs to parameterize the swarm
            (e.g., {"symbol": "BTC-USD", "regime": "bullish"}). Keys and
            values are both strings.

    Returns:
        str: Multi-agent analysis report with individual agent views, debate
            summary, and consensus recommendation.

    Example: vibe_run_swarm("investment_committee", {"symbol": "BTC-USD", "timeframe": "1D"})

    Note: Requires the external Vibe-Trading MCP server process to be
    running. Swarm execution may take 10-60+ seconds depending on preset
    complexity and the number of agents involved.
    """
    return await vibe_tools.vibe_run_swarm(preset, variables)


@mcp.tool(name="vibe_backtest", annotations=_VIBE_ANNOTATIONS)
async def vibe_backtest(run_dir: str) -> str:
    """Run a backtest via the external Vibe-Trading engine (not local).

    Use to validate a trading strategy against historical data before
    deploying live. The backtest runs on the Vibe-Trading side, not
    within TradeBridge.

    Args:
        run_dir (str): Absolute path to the backtest directory on the
            Vibe-Trading server. Must contain config.json (strategy
            parameters) and signal_engine.py (signal generation logic).

    Returns:
        str: Backtest results including performance metrics (Sharpe,
            max drawdown, win rate, total return) and equity curve summary.

    Example: vibe_backtest("/home/vibe/backtests/ma_crossover_2024-01")

    Note: Requires the external Vibe-Trading MCP server process to be
    running. Backtesting is executed remotely — the run_dir path must
    be accessible from the Vibe-Trading server's filesystem, not the
    TradeBridge host. Ensure config.json and signal_engine.py exist
    and are valid.
    """
    return await vibe_tools.vibe_backtest(run_dir)


@mcp.tool(name="vibe_swarm_to_signal", annotations=_VIBE_ANNOTATIONS)
async def vibe_swarm_to_signal(report: str, preset: str = "") -> str:
    """Convert a Vibe-Trading swarm analysis report into a structured TradeBridge trade signal.

    Use after vibe_run_swarm to transform the multi-agent consensus into
    actionable order parameters. This is the bridge between AI analysis
    and MT5 execution.

    Args:
        report (str): The full text output from a previous vibe_run_swarm
            call. Must contain analysis results with trade recommendations.
        preset (str): The swarm preset that generated the report (default: "").
            Helps the parser understand the report format. Can be left empty
            for auto-detection.

    Returns:
        str: Structured trade signal with TradeBridge-compatible order
            parameters: direction (BUY/SELL), entry price, stop-loss (SL),
            take-profit (TP), position size suggestion, and reasoning summary.

    Example: vibe_swarm_to_signal(swarm_report, "investment_committee")

    Note: Requires the external Vibe-Trading MCP server process to be
    running. Signal extraction quality depends on the report structure.
    If the swarm output lacks clear trade recommendations, the signal
    may be incomplete or return an error.
    """
    return await vibe_tools.vibe_swarm_to_signal(report, preset)


@mcp.tool(name="vibe_web_search", annotations=_VIBE_ANNOTATIONS)
async def vibe_web_search(query: str, max_results: int = 5) -> str:
    """Search the web for market news, sentiment, and macro events via Vibe-Trading.

    Use for fundamental context gathering — news-driven analysis, earnings
    reports, economic indicators, and sentiment checks that complement
    technical analysis from MT5 data.

    Args:
        query (str): Natural language search query (e.g., "Fed interest rate
            decision March 2024 impact on USD"). Supports operators like
            site:, before:, after: for targeted searches.
        max_results (int): Maximum results to return (default: 5, range: 1-20).
            Higher values increase response time.

    Returns:
        str: Formatted search results with title, source, snippet, and URL
            for each result. Sorted by relevance.

    Example: vibe_web_search("crude oil supply glut April 2024", max_results=5)

    Note: Requires the external Vibe-Trading MCP server process to be
    running. Search quality depends on the underlying search provider
    configured in Vibe-Trading. May not return results for highly
    specific or niche queries.
    """
    return await vibe_tools.vibe_web_search(query, max_results)
