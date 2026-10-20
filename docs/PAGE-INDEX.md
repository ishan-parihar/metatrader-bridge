# Page index — every active doc, the question it owns

Every page under `docs/`, with the question it answers. Pages named `README.md` or `index.md` are directory entry points reached through the doc that owns them, so they are not listed here.

| File | Status | Owns | Not for |
|---|---|---|---|
| [`AGENT_UX_AUDIT_AND_UPGRADE_PLAN.md`](AGENT_UX_AUDIT_AND_UPGRADE_PLAN.md) | PLAN | MetaTraderBridge: AI-Agent UX Audit & Upgrade Plan | — |
| [`INTEGRATION_AUDIT_20260409.md`](INTEGRATION_AUDIT_20260409.md) | PLAN | MetaTraderBridge Integration Audit: Tools ↔ SKILL.md Alignment | — |
| [`RECORDS.md`](RECORDS.md) | LIVE | Records — decisions (AD) and regressions (RG) | — |
| [`RUNBOOK.md`](RUNBOOK.md) | LIVE | Runbook — exact commands, environment traps | — |
| [`UPGRADE_MANDATE_20260408.md`](UPGRADE_MANDATE_20260408.md) | PLAN | MetaTraderBridge Upgrade Mandate | — |
| [`UPGRADE_PLAN_20260408.md`](UPGRADE_PLAN_20260408.md) | PLAN | MetaTraderBridge Upgrade Plan | — |
| [`adr-002-latency-optimization.md`](adr-002-latency-optimization.md) | LIVE | ADR-002: Latency Optimization for MetaTraderBridge Trading System | — |
| [`adr-003-ownership-and-idempotency-contract.md`](adr-003-ownership-and-idempotency-contract.md) | LIVE | ADR-003: Canonical Ownership and Idempotency Contract | — |
| [`audit-trading-agent-stop-20260409.md`](audit-trading-agent-stop-20260409.md) | EVIDENCE | MetaTraderBridge Trading Agent Infrastructure Audit | — |
| [`audit-trading-system-2026-04-04.md`](audit-trading-system-2026-04-04.md) | EVIDENCE | MetaTraderBridge Trading System Audit Report | — |
| [`comprehensive_audit_report_20260409.md`](comprehensive_audit_report_20260409.md) | EVIDENCE | MetaTraderBridge Comprehensive Codebase Audit — Full Bug Report | — |
| [`comprehensive_audit_report_20260409_pass2.md`](comprehensive_audit_report_20260409_pass2.md) | EVIDENCE | MetaTraderBridge Comprehensive Codebase Audit — Second Pass | — |
| [`fix_report_20260409.md`](fix_report_20260409.md) | EVIDENCE | MetaTraderBridge Comprehensive Codebase Audit — Fix Report | — |
| [`STATE.md`](handoff/STATE.md) | LIVE | State | — |
| [`ASKS.md`](kb/ASKS.md) | LIVE | Operator questions and rulings | — |
| [`CLOSED.md`](kb/CLOSED.md) | LIVE | CLOSED — tried and rejected | — |
| [`WORKLOG.md`](kb/WORKLOG.md) | LIVE | Worklog | — |
| [`feed.md`](kb/feed.md) | LIVE | Agent feed | — |
| [`proposal-vibe-trading-tool-cleanup.md`](proposal-vibe-trading-tool-cleanup.md) | PLAN | Proposal: MetaTraderBridge MCP Tool Consolidation with Vibe-Trading Integration | — |
| [`2026-04-14-vibe-trading-integration.md`](superpowers/plans/2026-04-14-vibe-trading-integration.md) | PLAN | Vibe-Trading × MetaTraderBridge Integration Plan | — |
| [`trading_audit_report.md`](trading_audit_report.md) | EVIDENCE | MCP Trading System — Post-Session Audit & Improvement Report | — |
| [`waiting_tools_bug_report.md`](waiting_tools_bug_report.md) | EVIDENCE | Waiting Tools Bug Report | — |
| [`waiting_tools_migration_plan.md`](waiting_tools_migration_plan.md) | PLAN | Migration Plan: Waiting Tools → Official MCP SDK | — |
| [`EA_COMPILATION_FIX.md`](EA_COMPILATION_FIX.md) | LIVE | EA Compilation Fix — The MT5 SymbolSelect() function requires 2 parameters: - symbol - The symbol name - select - true to… | — |
| [`MCP_TOOL_USAGE_REPORT.md`](kb/MCP_TOOL_USAGE_REPORT.md) | LEDGER | MetaTraderBridge Tool Usage Report: Autonomous Trading Agent "Jesse" — The MetaTraderBridge server is functionally operational for core market data and order management bu… | — |
| [`2026-04-02-PLAN.md`](plans/2026-04-02-PLAN.md) | PLAN | MetaTrader 5 ↔ MCP Bridge Blueprint for Arch Linux — Version: 1.0 Audience: R&D engineering, platform engineering, quant engineering, infra/security Stat… | — |
| [`2026-04-09-RND_ANALYSIS_ENGINE.md`](audit/2026-04-09-RND_ANALYSIS_ENGINE.md) | EVIDENCE | R&D Recommendation: Analysis Engine Enhancements — A full audit of the legacy Chinese quant system (~/.agents/skills/trading-quant/, 1011-line CLI with… | — |
| [`2026-04-07-ROADMAP.md`](audit/2026-04-07-ROADMAP.md) | EVIDENCE | MT5 Python Algo-Trading Roadmap — After auditing 5 AlphaForge projects, 2 external frameworks (FinRL, NautilusTrader), and 20+ externa… | — |
| [`SYMBOL_SUFFIX_FIX.md`](SYMBOL_SUFFIX_FIX.md) | LIVE | Symbol Suffix Fix for MT5-MCP Bridge — Added new configuration option: python symbolsuffix: str = os.getenv("MT5SYMBOLSUFFIX", "m")… | — |
| [`2026-05-06-dockerization-session-memory.md`](legacy/2026-05-06-dockerization-session-memory.md) | EVIDENCE | Session memory from the dockerization and optimization pass — the tool quirks cost hours to find (compose v2 dropped `version`, Poetry 2.x dropped `--no-update`, asyncio.DEVNULL does not exist), and the image-size numbers. |
