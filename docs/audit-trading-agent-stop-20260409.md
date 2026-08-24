# MetaTraderBridge Trading Agent Infrastructure Audit

**Date:** 2026-04-09
**Trigger:** Trading agent stopped mid-cycle after hitting 404 errors on analysis endpoints. Expected: continuous polling until session end. Actual: asked user a question and stopped.

---

## Executive Summary

The agent stopped because of a **three-layer failure cascade**:

1. **Tool unavailability** (404 on analysis endpoints) — the MCP server is running an older version without the analysis endpoints that the SKILL.md references
2. **No fallback protocol** — the SKILL.md says "skip any that return insufficient data" but doesn't cover what to do when tools return HTTP 404
3. **Conflicting system instructions** — `agent_prompt.py` (7-phase legacy workflow) directly conflicts with `SKILL.md` (12-phase modern workflow). The agent received both and followed neither correctly.

The agent then defaulted to the safest behavior: ask the user and stop. This is rational behavior for an LLM that doesn't know what to do. The problem is that **the SKILL.md explicitly forbids this**: "Your trading cycle never stops mid-flow" (line 471).

---

## Root Cause Analysis

### Layer 1: Tool Unavailability (The Trigger)

**What happened:** The agent called `analysis/divergence` and `analysis/volume_profile` per Phase 3.25 of the SKILL.md. Both returned 404.

**Why:** The endpoints ARE defined in `apps/mcp_server/main.py` (lines 2420, 2441, 2460, 2479). But the running MCP server instance is an older version that was not restarted after these endpoints were added. This is a **deployment issue**, not a code issue.

**Impact:** The agent lost 2 of 4 analysis dimensions (divergence, volume). Only momentum and pattern recognition were available (momentum also returned 404 in the trace).

### Layer 2: No Fallback Protocol (The Escalation)

The SKILL.md Phase 3.25 says:
> "skip any that return insufficient data, but do NOT skip based on hoping the signal is favorable."

**The problem:** HTTP 404 is not "insufficient data" — it's an error. The SKILL.md has no guidance for:
- What to do when analysis tools are unavailable
- Whether to proceed with partial analysis
- Whether to retry later
- Whether to fall back to bracket orders without analysis confirmation

**What the agent did:** It presented the partial analysis, asked "Should I proceed?" and stopped.

**What the agent should have done (per SKILL.md intent):** Set up bracket orders at S/R levels, or used `wait/delay(600)` to wait 10 minutes and re-scan.

### Layer 3: Conflicting System Instructions (The Structural Problem)

**This is the most critical finding.**

The agent receives TWO instruction sets:

| Document | Phases | Mentions Polling? | Mentions Wait Tools? | Mentions 10-min cadence? | Mentions Tier 1/2/3? |
|---|---|---|---|---|---|
| `agent_prompt.py` | 7 | ❌ | ❌ | ❌ | ❌ |
| `SKILL.md` | 12 | ✅ | ✅ | ✅ | ✅ |

**agent_prompt.py (lines 160-208) prescribes a 7-phase workflow:**
1. Session Start → 2. Market Scan → 3. Trade Decision → 4. Entry → 5. Management → 6. Exit & Reflection → 7. End of Session

**SKILL.md prescribes a 12-phase workflow:**
1. State Triage → 2. Mode Determination → 3. Market Discovery → 3.25 Analysis Pipeline → 3.5 Correlation Gate → 4. Trade Viability Gate → 4.5 RSI Filter → 5. Risk Framework → 6. Position Sizing → 7. Execution → 8. Position Management → 9. Journaling → 10. Polling Protocol → 11. Wait Protocol → 12. Continuous Cycle

**The agent_prompt.py workflow ENDS at Phase 7 (End of Session).** It has no concept of continuous cycling, polling, or waiting. When the agent follows agent_prompt.py, the natural conclusion is "session is over."

**When the agent follows SKILL.md, it hits Phase 3.25 (Analysis Pipeline), gets 404s, and has no fallback.**

The agent is receiving contradictory instructions from two different sources and ends up following neither.

---

## Detailed Gap Analysis

### Gap 1: agent_prompt.py Workflow is Completely Outdated
**Severity: CRITICAL** | **File:** `src/mt5_mcp/services/agent_prompt.py`

The entire workflow section (lines 160-208) is a legacy 7-phase workflow that predates the modern SKILL.md. It:
- Prescribes individual indicator calls instead of `trading/decision_support()` (one-call replacement)
- Doesn't mention `market/scan()` (multi-symbol batch scan)
- Doesn't mention `market/snapshot()` (one-call market context)
- Doesn't mention `economic_calendar()` (event awareness)
- Doesn't mention ANY wait tools (wait/trade_monitor, wait/delay, wait/indicator, wait_for_price)
- Doesn't mention polling tiers (Tier 1/2/3 strategy)
- Doesn't mention the 10-minute default cadence
- Doesn't mention the trailing checklist with `position.health` fields
- Doesn't mention the analysis pipeline fallback behavior
- Doesn't mention `action_required` or `next_action_guidance` (our new features)
- Doesn't mention `snapshot_metadata` or `next_recommended_check_seconds`

**Result:** When the agent_prompt.py is injected as system context (which it always is), it gives the agent a fundamentally wrong mental model of how the system works.

### Gap 2: No Tool Availability Health Check
**Severity: HIGH** | **File:** N/A (missing feature)

The agent has no way to know which tools are actually available at session start. It discovers tool failures reactively (by calling them and getting 404).

**Should exist:** A `trading/tool_health()` endpoint that returns which tool categories are available, so the agent can adapt its workflow at the start of each cycle.

### Gap 3: No Fallback for Analysis Pipeline Failures
**Severity: HIGH** | **File:** `skills/mt5-trading/SKILL.md`

Phase 3.25 has no guidance for when tools return 404 or other errors. The "skip any that return insufficient data" line is insufficient — it doesn't distinguish between:
- Tool returned `{data: []}` (no data — skip is fine)
- Tool returned HTTP 404 (endpoint doesn't exist — needs different handling)
- Tool returned HTTP 500 (server error — retry later?)

**Should exist:** Explicit fallback protocol: "If analysis tools return 404, proceed with decision_support + support_resistance only, set up bracket orders, and use wait/delay(600) to re-check next cycle."

### Gap 4: No "Decision Deadline" or "Analysis Timeout"
**Severity: MEDIUM** | **File:** `skills/mt5-trading/SKILL.md`

The agent can get stuck in analysis paralysis. The SKILL.md says (line 229):
> "If you have analyzed the same setup 3+ times without executing, either execute or skip — do not analyze a fourth time."

But this doesn't prevent the agent from analyzing 3 different setups, each taking 4 tool calls, burning 12+ tool calls before making any decision.

**Should exist:** "Maximum 8 tool calls between decision points. If you've made 8 calls without submitting an order or entering a wait state, you must either place bracket orders or wait/delay and re-scan."

### Gap 5: No "Wait + Re-scan" Loop Instruction
**Severity: HIGH** | **File:** Both SKILL.md and agent_prompt.py

Neither document explicitly tells the agent what to do after presenting analysis to the user and waiting for a response. The SKILL.md says "never stop mid-flow" but doesn't provide the loop structure:

```
Analyze → Decide → (if no clear entry) → wait/delay(600) → Re-scan → Repeat
```

The agent needs explicit instruction that "presenting analysis to the user and waiting for their input" is NOT the end of the cycle — it's a decision point within the cycle.

### Gap 6: README Redundancy
**Severity: MEDIUM** | **File:** `README.md`

The README is 344 lines of curl-based API reference that:
- Duplicates information already in the SKILL.md
- Is useful for human developers but irrelevant for AI agents
- Doesn't mention any of the modern features (market/scan, wait tools, analysis pipeline, polling protocol)
- Should be a concise entry point with a link to the SKILL.md for AI agent usage

---

## What the Agent Trace Reveals

Looking at the actual trading run:

| Phase | Expected (SKILL.md) | What Actually Happened |
|---|---|---|
| Phase 1: State Triage | ✅ 5 parallel checks | ✅ Completed correctly |
| Phase 2: Market Scan | ✅ market/scan() + economic_calendar | ✅ Completed correctly |
| Phase 3: Market Discovery | ✅ trading/decision_support x3 | ✅ Completed correctly |
| Phase 3.25: Analysis Pipeline | 4 tools (patterns, divergence, volume, momentum) | ❌ 2 tools returned 404 |
| Phase 3.5: Correlation Gate | Check correlation | ✅ Noted EURUSD/GBPUSD correlation |
| Phase 4: Trade Viability | 5 criteria check | ✅ Evaluated setups |
| S/R Check | support_resistance() | ✅ Found levels |
| **DECISION POINT** | Place bracket orders OR wait/delay + re-scan | ❌ Asked user and STOPPED |

The agent did everything right until the decision point. At that point, it had:
- GBPUSD LONG setup (near support, 5.2:1 RR, but suboptimal session)
- EURUSD LONG setup (near support, but higher in range)
- Both analysis tools failed (404)

A properly instructed agent would have:
1. Placed bracket orders at S/R levels (GBPUSD support 1.3176, resistance 1.3484)
2. Logged the decision with confidence level reduced due to missing analysis
3. Used `wait/delay(600)` to wait 10 minutes
4. Re-scanned on resume

Instead, it asked "Should I proceed?" and stopped.

---

## Implementation Plan

### Phase 1: Fix the Structural Problem (agent_prompt.py alignment)

**What:** Rewrite `agent_prompt.py` workflow section to match the 12-phase SKILL.md workflow.

**Why:** This is the root cause. The agent gets conflicting instructions from two sources.

**How:** Replace the 7-phase workflow (lines 160-208) with a condensed reference to the SKILL.md phases, emphasizing:
- Continuous cycling (never stop mid-flow)
- Wait tools over polling
- 10-minute default cadence
- Analysis pipeline fallback behavior
- Tool availability awareness

### Phase 2: Add Analysis Pipeline Fallback to SKILL.md

**What:** Add explicit guidance to Phase 3.25 for when tools return 404 or errors.

**Why:** The agent needs to know what to do when analysis tools are unavailable.

**How:** Add a "Fallback Protocol" subsection:
- If 1-2 tools fail → proceed with available tools, reduce confidence
- If 3+ tools fail → skip analysis, use decision_support + support_resistance only
- Always fall back to bracket orders + wait/delay if analysis is incomplete
- Log missing analysis as a `tool_unavailable` note in the decision journal

### Phase 3: Add "Wait + Re-scan" Loop Instruction

**What:** Explicit instruction that the cycle continues even after presenting analysis.

**Why:** The agent stopped because it thought presenting analysis was the end goal.

**How:** Add to SKILL.md Phase 10 (Continuous Cycle):
- After presenting analysis, if no entry is taken: use `wait/delay(600)` and re-scan
- The cycle is: Scan → Analyze → Decide → Execute/Wait → Repeat
- Never end a cycle by asking the user a question — make a decision and act on it

### Phase 4: Add Analysis Tool Count Limit

**What:** Maximum tool calls between decision points.

**Why:** Prevents analysis paralysis.

**How:** Add to SKILL.md: "Maximum 8 tool calls between Scan and Execute phases. If you've made 8 calls without placing an order or entering a wait state, you must place bracket orders or wait/delay(600) and re-scan."

### Phase 5: Rewrite README

**What:** Concise entry point, remove API duplication.

**Why:** README is 344 lines of redundant curl examples.

**How:** Reduce to:
- Architecture overview (keep)
- Quick start (keep, simplify)
- Link to SKILL.md for AI agent usage
- Link to API docs for human developers
- Remove all curl examples (they're in the SKILL.md tool descriptions)

### Phase 6: Add Tool Health Endpoint (Future)

**What:** `trading/tool_health()` endpoint that returns available tool categories.

**Why:** Proactive tool availability check.

**How:** New endpoint that tests each tool category and returns a health map. Add to SKILL.md Phase 1 (State Triage): "Call `trading/tool_health()` at session start to know which tools are available."

---

## Priority Matrix

| Priority | Change | Effort | Impact |
|---|---|---|---|
| **P0** | Rewrite agent_prompt.py workflow | 30 min | Eliminates conflicting instructions |
| **P0** | Add analysis pipeline fallback to SKILL.md | 15 min | Prevents agent from stopping on 404 |
| **P0** | Add "wait + re-scan" loop instruction | 10 min | Ensures continuous cycling |
| **P1** | Add analysis tool count limit | 5 min | Prevents analysis paralysis |
| **P1** | Rewrite README | 30 min | Removes redundancy |
| **P2** | Add tool health endpoint | 45 min | Proactive tool awareness |

**Total P0+P1 effort: ~90 minutes.** All changes are documentation/prompt edits except README rewrite and the future tool health endpoint.

---

## The 404 Root Cause

The 404 errors on analysis endpoints are a **deployment issue**, not a code issue. The endpoints exist in the codebase but the running MCP server instance hasn't been restarted. 

**Immediate fix:** Restart the MCP server:
```bash
# Kill the running MCP server
# Then restart:
poetry run uvicorn apps.mcp_server.main:app --host 127.0.0.1 --port 8010
```

**Long-term fix:** The SKILL.md should handle tool unavailability gracefully regardless of deployment state. This audit addresses that.
