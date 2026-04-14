---
name: money-trading-change
description: Modify or explain Money repo trading behavior. Use when the user asks to change buy logic, sell logic, tranches, stop loss, trailing stop, partial exits, asset class behavior, position sizing, risk limits, strategy ranking, or ML entry and exit gating. Do not use for pure deployment questions.
---

# Money trading change

Use this skill for repo-specific trading behavior changes and explanations.

## Objectives

- Change trading logic without breaking paper execution flow
- Preserve hard risk controls
- Keep logging and diagnostics consistent with behavior changes
- Avoid hidden side effects across execution, monitoring, and persistence

## Workflow

1. Identify which layer the request touches:
   - strategy generation
   - risk filter
   - ML scoring
   - execution sizing
   - exit policy
   - Discord/logging side effects
2. Inspect nearby modules before editing.
3. Preserve the repo architecture:
   - strategy proposes
   - risk filters decide safety
   - ML assists ranking/filtering
   - execution submits
   - monitoring/logging record outcomes
4. If changing BUY/SELL behavior, also review:
   - persistence/log artifacts
   - diagnostics routes
   - tests covering auto trader and execution
5. Explain how the change affects paper behavior and what to verify.

## Mandatory constraints

- Hard risk exits remain authoritative
- ML must not override hard stops or emergency exits
- News cannot place orders directly
- RL remains sandbox-only
- Do not silently change asset class defaults without documenting the effect

## Verification checklist

After a trading change, verify at least:

- targeted tests for touched modules
- API startup
- `/auto/status`
- one manual `run-once` call with a representative symbol
- relevant diagnostics route if the change touches risk, strategy, or tranches

## Good outputs

- exact file list to modify
- change plan by subsystem
- concrete verification commands
- note on backwards compatibility and default behavior
