"""
Live Inspection & Codegen Agent Service
=========================================
Backend service that drives an autonomous browser-inspection + code-generation
session using the Claude Agent SDK, with a Playwright MCP server as the
browser tool. This replaces the "Claude + Chrome DevTools in a chat" pattern
with something that runs unattended, from your own infrastructure.

Real, verified building blocks this is built on:
  - claude_agent_sdk (pip install claude-agent-sdk) - ClaudeSDKClient,
    ClaudeAgentOptions, HookMatcher, message/content block types.
  - @playwright/mcp (npm) - MCP server exposing browser automation tools
    over structured accessibility snapshots. Run with HTTP transport
    (`npx @playwright/mcp@latest --port 8931 --host 0.0.0.0`) so a stateless
    worker process can connect to it as a URL rather than spawning a subprocess
    per job.

TOKEN/COST DESIGN (2026-08-20 refactor)
----------------------------------------
Production cost data (cost_ledger.jsonl) showed 4 of 5 jobs blowing through
the $0.75/job default budget (actual spend $0.77-$1.10) and dying via
`error_max_budget_usd` with nothing to show for it. The dominant cost driver
was `cache_read_input_tokens` in the 340K-545K range per job, compounding
across 14-21 turns of one long browser-driving + code-writing session -
every earlier tool result (page snapshots, network logs) stays in context
and gets re-paid on every later turn.

This version splits the job into two independent agent sessions instead of
one monolithic one:
  1. RECON phase - browser tools only, produces a small structured JSON of
     *findings* (selectors, page type, sample data) - NOT code. Guarded by
     a redundant-call dedup hook and an anti-bot early-exit so a doomed
     inspection doesn't burn its whole step budget after the site has
     already signaled a block.
  2. CODEGEN phase - no browser tools at all, starts a *fresh* context
     containing only the compact recon findings + the requester's rules,
     and writes the script. It never pays for the accumulated snapshot/
     network-log history from recon, which is the single biggest lever
     here: codegen's input context is now the findings JSON (small),
     not the full multi-turn browser transcript.

Each phase gets its own share of the job's budget (see cost_metering.
split_budget_ceiling) and the ceiling handed to the SDK is shaved by a
safety margin so a single expensive turn is less likely to blow past the
job's actual target spend before the guard can stop it.

This file is a working service skeleton: correct SDK usage, real permission
scoping, real hooks for audit logging and step/cost capping. It is NOT wired
to a live job queue or database - see api_service.py for how it plugs into
the rest of Scraper Studio.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
)

from auth_config import require_credentials_at_startup, CredentialError
from cost_metering import (
    CostRecord,
    CostSink,
    StdoutCostSink,
    build_cost_record,
    build_fallback_cost_record,
    combine_cost_records,
    resolve_budget_ceiling,
    split_budget_ceiling,
    SDK_BUDGET_SAFETY_MARGIN,
)

# --------------------------------------------------------------------------
# Job contract
# --------------------------------------------------------------------------

@dataclass
class InspectionJob:
    """Mirrors the fields submitted from the Builder page."""
    job_id: str
    target_url: str
    sample_keyword: str | None = None  # optional: one concrete value for the
                                        # agent to actually test navigation/
                                        # search with during live inspection.
                                        # NOT a runtime setting - the delivered
                                        # script's real keyword/URL handling
                                        # comes from input.xlsx at run time.
    search_rules: str = ""
    filter_rules: str = ""
    navigation_rules: str = ""
    extraction_rules: str = ""
    max_steps: int = 40          # hard cap on RECON phase tool calls (cost control)
    max_seconds: int = 600       # hard cap on wall-clock time for the RECON phase
    max_budget_usd: float | None = None  # hard cap on TOTAL spend across both
                                          # phases; None -> service default
    recon_model: str | None = None    # optional model override for the browser-
                                       # driving phase - a cheaper/faster model
                                       # is usually fine here since the task is
                                       # mostly mechanical navigation + reporting
                                       # facts, not writing code. None -> SDK/
                                       # service default.
    codegen_model: str | None = None  # optional model override for the code-
                                       # writing phase. None -> SDK/service
                                       # default.


@dataclass
class InspectionResult:
    job_id: str
    status: str                  # "completed" | "failed" | "step_limit_hit" | "time_limit_hit" | "budget_limit_hit" | "anti_bot_detected"
    generated_code: str = ""
    requirements_txt: str = ""
    confidence_notes: list[str] = field(default_factory=list)
    action_log: list[dict] = field(default_factory=list)
    total_tool_calls: int = 0
    total_cost_usd: float | None = None
    cost_record: dict | None = None   # combined CostRecord across both phases,
                                       # serialized, for the API layer. Has the
                                       # same top-level keys as the original
                                       # single-phase design (total_cost_usd,
                                       # num_turns, ...) for backward
                                       # compatibility, plus a "phases" list
                                       # breaking spend down by recon/codegen.
    anti_bot_marker: str | None = None  # set if recon's early-exit fired (see
                                         # ANTI_BOT_MARKERS) - surfaced as a
                                         # first-class field, not just buried
                                         # in confidence_notes text, so the
                                         # API/dashboard can flag it directly.


# --------------------------------------------------------------------------
# Playwright MCP server connection
# --------------------------------------------------------------------------
# Run once, shared by the worker fleet (not spawned per-job):
#   npx @playwright/mcp@latest --port 8931 --host 0.0.0.0 --isolated
# --isolated gives each MCP session a fresh browser profile - important so
# one customer's job can't see another's cookies/storage state.

def playwright_mcp_config(base_url: str = "http://localhost:8931") -> dict:
    return {
        "browser": {
            "type": "http",
            "url": f"{base_url}/mcp",
        }
    }


# --------------------------------------------------------------------------
# Guardrails: allowed tools, hooks for audit log + step/time/repeat capping
# --------------------------------------------------------------------------
# The recon agent should only ever be able to look at a webpage - never
# write files, run shell commands, or touch anything outside the browser.
# The codegen agent (below) gets none of these - it never touches a browser.
RECON_ALLOWED_TOOLS = [
    "mcp__browser__browser_navigate",
    "mcp__browser__browser_snapshot",
    "mcp__browser__browser_click",
    "mcp__browser__browser_type",
    "mcp__browser__browser_press_key",
    "mcp__browser__browser_wait_for",
    "mcp__browser__browser_network_requests",
    "mcp__browser__browser_take_screenshot",
]
ALLOWED_TOOLS = RECON_ALLOWED_TOOLS  # backward-compat alias

# Tools whose *result* is expensive (a full accessibility snapshot, every
# network request, a screenshot) and whose value comes from being fresh -
# calling one of these twice in a row with identical arguments doesn't teach
# the agent anything new, it just re-pays (in cache-read tokens) for
# information that's already sitting in context. This is the biggest single
# driver of the cache_read_input_tokens growth seen in production (500K+
# tokens/job) - the dedup guard below directly targets it.
REPEATABLE_WITHOUT_PROGRESS = {
    "mcp__browser__browser_snapshot",
    "mcp__browser__browser_network_requests",
    "mcp__browser__browser_take_screenshot",
}

# Anti-bot / block markers to watch for in tool results during recon. If the
# site has already told us "no" (captcha wall, WAF block page, rate limit),
# continuing to spend turns poking at it doesn't produce anything more
# useful than what's already been observed - better to stop immediately,
# report what was found before the block, and let codegen work with that
# (or let the job fail cleanly) rather than burn the rest of the step/budget
# ceiling against a wall. Mirrors the markers the generated scraper's own
# `detect_anti_bot_signals()` is instructed to check for (see
# build_codegen_prompt), so the service and the script it produces agree on
# what "blocked" looks like.
ANTI_BOT_MARKERS = [
    "captcha", "cloudflare", "perimeterx", "datadome", "access denied",
    "unusual traffic", "are you a robot", "verify you are human",
    "403 forbidden", "429 too many requests", "request blocked",
]


def _detect_anti_bot_signal(text: str) -> str | None:
    lowered = (text or "").lower()
    for marker in ANTI_BOT_MARKERS:
        if marker in lowered:
            return marker
    return None


def _tool_result_text(block: ToolResultBlock) -> str:
    """ToolResultBlock.content is either a plain string or a list of content
    blocks (mirroring the Anthropic Messages API tool_result shape) -
    normalize either into plain text for marker scanning."""
    content = getattr(block, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(getattr(item, "text", "")))
        return " ".join(parts)
    return str(content or "")


def build_hooks(
    job: InspectionJob,
    action_log: list[dict],
    call_counter: dict,
    max_steps: int | None = None,
    phase_label: str = "recon",
    on_log_entry=None,
):
    """
    PreToolUse hook: runs BEFORE every tool call the agent makes.
      - Logs the call (audit trail), tagged with which phase it belongs to
        so a merged recon+codegen action_log stays unambiguous.
      - Pushes the same entry to on_log_entry(), if provided, so a caller
        (e.g. the API layer's SSE endpoint) can stream it live rather than
        only seeing the full list after the job finishes.
      - Enforces the per-phase step cap by denying further tool use once hit.
      - Denies an immediate repeat of an expensive read-only tool call with
        identical arguments (see REPEATABLE_WITHOUT_PROGRESS above).
    This is the application intercepting the agent loop, not Claude deciding
    to stop on its own - so the caps are real and can't be reasoned around.
    """
    effective_max_steps = job.max_steps if max_steps is None else max_steps
    last_call = {"key": None, "repeat_count": 0}

    async def audit_and_cap(input_data, tool_use_id, context):
        call_counter["count"] += 1
        tool_name = input_data.get("tool_name")
        tool_input = input_data.get("tool_input")
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "phase": phase_label,
            "tool": tool_name,
            "input": tool_input,
            "call_number": call_counter["count"],
        }
        action_log.append(entry)
        if on_log_entry:
            try:
                await on_log_entry(entry)
            except Exception:
                pass  # streaming is best-effort - never let it break the job itself

        if call_counter["count"] > effective_max_steps:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Step cap ({effective_max_steps}) reached for job "
                        f"{job.job_id} ({phase_label} phase). Denying further "
                        "browser actions - wrap up with what's been gathered so far."
                    ),
                }
            }

        if tool_name in REPEATABLE_WITHOUT_PROGRESS:
            try:
                call_key = (tool_name, json.dumps(tool_input, sort_keys=True, default=str))
            except TypeError:
                call_key = (tool_name, str(tool_input))
            if call_key == last_call["key"]:
                last_call["repeat_count"] += 1
            else:
                last_call["repeat_count"] = 0
            last_call["key"] = call_key
            if last_call["repeat_count"] >= 1:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"{tool_name} was just called with identical arguments - "
                            "that result is already in context. Act on what you "
                            "already observed (click/type/navigate based on the "
                            "last snapshot, or move on to the next step) instead "
                            "of re-fetching unchanged state."
                        ),
                    }
                }
        else:
            last_call["key"] = None
            last_call["repeat_count"] = 0

        return {}

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[audit_and_cap])],
    }


# --------------------------------------------------------------------------
# Prompt construction - translates the NL rules from the Builder page into
# each phase's task
# --------------------------------------------------------------------------

SCRIPT_CONTRACT = """MANDATORY SCRIPT CONTRACT - every scraper you write MUST follow this exact
shape, regardless of the target site. This is not optional or per-job -
every script this service produces must be a drop-in replacement for any
other, so a person running it never has to read the code first to figure
out how to call it:

  - Command-line args via argparse: `--input` (default "input.xlsx"),
    `--output` (default "output.xlsx"), `--max-products` (optional int cap).
  - Input Excel columns (any subset may be populated - handle all three):
    `keyword`, `listing_url`, `detail_url` - same semantics as: keyword
    triggers a search/match flow, listing_url crawls a specific listing
    page, detail_url scrapes one page directly, skipping the crawl.
  - IMPORTANT: implement `detail_url` as a genuinely independent fast path,
    even if the inspection findings only describe navigating there by
    clicking through from a listing page. A row with only `detail_url`
    populated must go straight to extraction on that exact page - it must
    NOT depend on first finding the page via a listing crawl, since a real
    user may supply detail URLs the inspection never saw.
  - Output Excel has exactly two sheets: "Extracted Data" (one row per
    item) and "Flags" (one row per anti-bot signal, low-confidence
    extraction, or error encountered during the run - never leave this
    sheet out even if it's empty).
  - Include a `detect_anti_bot_signals()` function that checks each HTTP
    response for 403/429 status, CAPTCHA/Cloudflare/PerimeterX/DataDome
    markers, and unusually short bodies - it only FLAGS these to the Flags
    sheet, it never attempts to bypass or solve them.
  - Include `get_proxy()` and `solve_captcha()` as unimplemented placeholder
    stubs (return None / raise NotImplementedError) - do not implement
    anti-bot bypass logic under any circumstances.
  - If ANY part of the script uses a browser (Playwright/Selenium), include a
    `dismiss_cookie_banner(page)` helper and call it once immediately after
    every `page.goto(...)`, before any other interaction with that page. It
    must try a short list of common consent-button text matches (e.g.
    "Accept all", "Accept", "I agree", "Allow all", "Agree") in a try/except
    that does nothing if no match is found within a short timeout - a banner
    that isn't there is not an error. Log (don't crash) if dismissal was
    attempted but nothing matched, and add a "Flags" row noting a possible
    layout change for that site. Static requests+BeautifulSoup code paths
    don't need this - they don't render overlays.
  - Print a timestamped log line for each major step (mirroring a
    `log()` helper), so a person running it from the command line can see
    progress."""


def build_recon_prompt(job: InspectionJob) -> str:
    keyword_line = (
        f"Sample keyword to test with: {job.sample_keyword}"
        if job.sample_keyword
        else "No sample keyword given - use a representative sample item during inspection."
    )
    return f"""You are inspecting a live website to gather the facts needed to build a
Python web scraper. YOU DO NOT WRITE ANY CODE IN THIS PHASE - a separate
step handles code generation from the findings you report here. Your only
output is a compact JSON findings object.

TARGET: {job.target_url}
{keyword_line}

The target above may be a listing/search page OR a single item's detail page -
figure out which by looking at what actually renders, don't assume either way.

RULES (as described by the requester, in plain language):
- Search rules: {job.search_rules or 'none specified'}
- Filter rules: {job.filter_rules or 'none specified'}
- Navigation rules: {job.navigation_rules or 'none specified'}
- Extraction rules (data points to capture): {job.extraction_rules or 'capture all visible product/article attributes'}

YOUR TASK:
1. Navigate to the target URL using the browser tool.
2. Take a snapshot to see the real rendered structure (not guessed selectors).
   This is how you determine whether you're looking at a listing page (many
   items) or a detail page (one item) - not something to assume beforehand.
   Do not re-snapshot the same page state more than once - if you need to
   check something specific after an action, take one fresh snapshot, not
   several of the same state.
3. If a cookie-consent banner, GDPR modal, or similar overlay is blocking the
   page, dismiss it before doing anything else (click "Accept"/"Accept all"/
   "I agree", or the closest equivalent) - don't attempt to click through or
   around it, and don't try to read data hidden behind it. Record that a
   consent banner was present and how you dismissed it, so the generated
   script can replicate the same dismissal step.
4. If this involves search/filter interaction, perform it once with the
   sample keyword (if given) to confirm the flow actually works.
5. Check the network requests once - if the page is backed by a clean JSON
   API, prefer noting that API over describing rendered-HTML selectors.
6. If you're on a listing page, identify the navigation path to detail pages
   and inspect one detail page the same way (dismissing any consent banner
   there too, if a fresh one appears). If you're already on a detail page,
   this step is not applicable.
7. For every fact you were NOT able to verify with high confidence, add an
   explicit note explaining what should be manually checked before a
   production run.
8. Do not attempt to solve CAPTCHAs or defeat any anti-bot mechanism you
   encounter - if you hit one, note it and stop inspecting immediately;
   don't keep exploring past a block.

Return your final answer as a single JSON object with exactly these keys
(keep every value CONCISE - a few sentences or short bullet-style strings per
key, not prose paragraphs - this findings object is another step's entire
input, so density matters more than completeness of wording):
  "page_type"              - "listing" | "detail" | "both"
  "cookie_banner"           - what was present (if anything) and how it was dismissed
  "search_flow"             - how search/filter interaction actually works, if tested
  "navigation_path"         - how to get from listing to detail pages
  "listing_selectors"       - concrete selectors/patterns observed for listing items
  "detail_selectors"        - concrete selectors/patterns observed on a detail page
  "network_api"             - any clean JSON API endpoint observed, or "none observed"
  "sample_extracted_fields" - a small sample of real values you actually saw, per
                               the extraction rules above
  "confidence_notes"        - list of strings, anything unverified or uncertain

CRITICAL FORMATTING RULE: your final message must be ONLY that JSON object -
no markdown code fences, no explanation before or after it, no prose of any
kind outside the JSON, and no code. It must parse cleanly with Python's
json.loads().
"""


def build_codegen_prompt(job: InspectionJob, recon_findings: dict) -> str:
    return f"""You are writing a complete Python web scraper based on a prior live-site
inspection. You do NOT have browser access in this step - work only from the
inspection findings below and the requester's rules. Where the findings are
thin or uncertain, say so explicitly in confidence_notes instead of
inventing selectors or behavior you weren't shown.

TARGET: {job.target_url}

INSPECTION FINDINGS (gathered during a live browser session, prior step):
{json.dumps(recon_findings, indent=2)}

RULES (as described by the requester, in plain language):
- Search rules: {job.search_rules or 'none specified'}
- Filter rules: {job.filter_rules or 'none specified'}
- Navigation rules: {job.navigation_rules or 'none specified'}
- Extraction rules (data points to capture): {job.extraction_rules or 'capture all visible product/article attributes'}

Use requests+BeautifulSoup for static/API-backed content per the findings'
"network_api" field when present, and a browser-based approach (noted
clearly in the delivered script) where the findings indicate the page
requires JS rendering.

{SCRIPT_CONTRACT}

Return your final answer as a single JSON object with exactly these keys:
  "generated_code"    - string, the full .py file, following the contract above
  "requirements_txt"  - string, the exact pip-installable dependency list
                         this specific script needs (only what it actually
                         imports - do not copy a generic list)
  "confidence_notes"  - list of strings (carry forward anything uncertain
                         from the inspection findings, plus anything you
                         had to assume while writing the code)

CRITICAL FORMATTING RULE: your final message must be ONLY that JSON object -
no markdown code fences (no ```json or ```), no explanation before or after
it, no prose of any kind outside the JSON. It must parse cleanly with
Python's json.loads(). Put any explanation you want to give INSIDE the JSON,
in confidence_notes - never as text surrounding the JSON object.
"""


RECON_SYSTEM_PROMPT = (
    "You are a careful web-inspection agent for Scraper Studio. You only "
    "observe pages through the browser tool provided - you never attempt to "
    "bypass CAPTCHAs, proxies, or IP blocks. Your ONLY job this phase is to "
    "gather and report real, observed facts about the target site as "
    "compact structured findings. You do not write any code in this phase - "
    "a separate step handles code generation from what you report. Be "
    "concise: every extra sentence you write is context another step has "
    "to pay to read."
)

CODEGEN_SYSTEM_PROMPT = (
    "You are a careful Python code-generation agent for Scraper Studio, "
    "writing a scraper from a prior live-site inspection's findings. You "
    "have no browser or filesystem access in this step - work only from "
    "the findings and rules given to you. Where the findings are "
    "incomplete or ambiguous, say so explicitly in confidence_notes rather "
    "than guessing at page structure you weren't shown."
)

CODEGEN_MAX_TURNS = 6          # codegen is a (near-)single-shot JSON response,
                                # not a multi-step tool loop - no browser tools
                                # are even offered, so this only needs enough
                                # headroom for a self-correction retry or two.
CODEGEN_MAX_SECONDS = 180      # independent, short wall-clock cap for codegen


# --------------------------------------------------------------------------
# Output parsing
# --------------------------------------------------------------------------

def parse_agent_output(final_text: str) -> tuple[str, str, list[str]]:
    """
    Turns the codegen agent's final message into (generated_code,
    requirements_txt, confidence_notes), with layered fallbacks - and a hard
    guarantee: this function never returns raw prose as generated_code. A
    .py file that fails to even parse (e.g. "SyntaxError: invalid character
    '—'") is worse than an empty one with a clear note, because it looks
    like a real file until someone tries to run it.

    Tier 1: strict JSON - the happy path the prompt asks for.
    Tier 2: JSON wrapped in a ```json ... ``` markdown fence (models do this
            even when told not to) - strip the fence, retry.
    Tier 3: no valid JSON at all - look for a ```python ... ``` fenced code
            block anywhere in the text and use that as the code, with a note
            that any surrounding explanation (and requirements_txt) was NOT
            captured.
    Tier 4: nothing structured found - return EMPTY code (never the raw
            text) plus a note, so nothing downstream mistakes explanation
            for executable code.
    """
    text = final_text.strip()

    try:
        parsed = json.loads(text)
        return (
            parsed.get("generated_code", ""),
            parsed.get("requirements_txt", ""),
            parsed.get("confidence_notes", []),
        )
    except (json.JSONDecodeError, TypeError):
        pass

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1))
            notes = parsed.get("confidence_notes", [])
            notes.append("Agent wrapped its JSON in a markdown fence instead of returning it raw - extracted automatically.")
            return parsed.get("generated_code", ""), parsed.get("requirements_txt", ""), notes
        except (json.JSONDecodeError, TypeError):
            pass

    code_match = re.search(r"```python\s*(.*?)\s*```", text, re.DOTALL)
    if code_match:
        return code_match.group(1), "", [
            "Agent did not return valid JSON - extracted code from a ```python "
            "fenced block in its response instead. requirements_txt could not "
            "be recovered this way - check the script's imports manually. "
            "Any explanation the agent gave outside the code block was NOT captured."
        ]

    return "", "", [
        "Agent response could not be parsed as JSON or a fenced code block - "
        "no usable code was extracted. This is a prompt/parsing issue with this "
        "job, not a problem with the target site. Re-running the job often "
        "resolves this."
    ]


def parse_recon_output(final_text: str) -> tuple[dict, list[str]]:
    """
    Same layered-fallback philosophy as parse_agent_output, but for the
    recon phase's findings JSON. Guarantees a dict is always returned (never
    None) so downstream code can check truthiness safely.
    """
    text = (final_text or "").strip()
    if not text:
        return {}, ["Recon phase produced no output text."]

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, []
    except (json.JSONDecodeError, TypeError):
        pass

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1))
            if isinstance(parsed, dict):
                return parsed, ["Recon agent wrapped its JSON in a markdown fence - extracted automatically."]
        except (json.JSONDecodeError, TypeError):
            pass

    # No structured findings - hand the raw (capped) text through rather than
    # nothing, so codegen still has *something* to work from, but flag it
    # clearly since it wasn't produced in the expected shape.
    capped = text[:4000]
    truncated_note = " (truncated)" if len(text) > 4000 else ""
    return (
        {"raw_recon_notes": capped},
        [f"Recon phase did not return structured JSON findings{truncated_note} - "
         "passing its raw text through to codegen as a best effort. Treat the "
         "generated script's confidence_notes with extra scrutiny for this job."],
    )


# --------------------------------------------------------------------------
# Shared single-session runner - both phases use this
# --------------------------------------------------------------------------

@dataclass
class _PhaseRun:
    status: str
    final_text: str
    tool_calls: int
    result_message: Any
    anti_bot_marker: str | None = None


async def _run_agent_session(
    *,
    options: ClaudeAgentOptions,
    task_prompt: str,
    max_seconds: int,
    call_counter: dict,
    max_steps: int,
    action_log: list[dict],
    detect_anti_bot: bool = False,
) -> _PhaseRun:
    start = time.monotonic()
    final_text = ""
    status = "completed"
    result_message = None
    anti_bot_marker = None

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(task_prompt)

            async for message in client.receive_response():
                if time.monotonic() - start > max_seconds:
                    status = "time_limit_hit"
                    break

                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            final_text += block.text
                        elif isinstance(block, ToolUseBlock):
                            pass  # already captured by the PreToolUse hook
                        elif isinstance(block, ToolResultBlock) and detect_anti_bot:
                            marker = _detect_anti_bot_signal(_tool_result_text(block))
                            if marker:
                                anti_bot_marker = marker

                if anti_bot_marker:
                    status = "anti_bot_detected"
                    break

                if isinstance(message, ResultMessage):
                    result_message = message
                    if message.subtype == "error_max_budget_usd":
                        status = "budget_limit_hit"
                    elif message.subtype == "error_max_turns":
                        status = "step_limit_hit"
                    elif message.is_error:
                        status = "failed"

    except Exception as exc:
        action_log.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "error": str(exc),
        })
        status = "failed"

    if call_counter["count"] > max_steps and status == "completed":
        status = "step_limit_hit"

    return _PhaseRun(
        status=status,
        final_text=final_text,
        tool_calls=call_counter["count"],
        result_message=result_message,
        anti_bot_marker=anti_bot_marker,
    )


# --------------------------------------------------------------------------
# Main entry point: run one inspection job (recon phase, then codegen phase)
# --------------------------------------------------------------------------

async def run_inspection_job(
    job: InspectionJob,
    mcp_base_url: str = "http://localhost:8931",
    cost_sink: CostSink | None = None,
    on_log_entry=None,
) -> InspectionResult:
    cost_sink = cost_sink or StdoutCostSink()
    action_log: list[dict] = []
    cost_records: list[CostRecord] = []

    total_budget = resolve_budget_ceiling(job.max_budget_usd)
    recon_budget, codegen_budget = split_budget_ceiling(total_budget)

    # ---- Phase 1: recon (browser tools, findings only) ----
    recon_call_counter = {"count": 0}
    recon_options = ClaudeAgentOptions(
        system_prompt=RECON_SYSTEM_PROMPT,
        mcp_servers=playwright_mcp_config(mcp_base_url),
        allowed_tools=RECON_ALLOWED_TOOLS,
        disallowed_tools=["Bash", "Write", "Edit", "Read"],  # no filesystem/shell access
        permission_mode="default",  # unlisted tools fall through to deny
        hooks=build_hooks(
            job, action_log, recon_call_counter,
            max_steps=job.max_steps, phase_label="recon", on_log_entry=on_log_entry,
        ),
        max_turns=job.max_steps + 10,  # SDK-level ceiling as a second safety net
        max_budget_usd=recon_budget * SDK_BUDGET_SAFETY_MARGIN,
        **({"model": job.recon_model} if job.recon_model else {}),
    )

    recon_run = await _run_agent_session(
        options=recon_options,
        task_prompt=build_recon_prompt(job),
        max_seconds=job.max_seconds,
        call_counter=recon_call_counter,
        max_steps=job.max_steps,
        action_log=action_log,
        detect_anti_bot=True,
    )

    if recon_run.result_message is not None:
        recon_record = build_cost_record(job.job_id, recon_run.result_message, phase="recon")
    else:
        recon_record = build_fallback_cost_record(job.job_id, recon_run.status, phase="recon")
    cost_sink.record(recon_record)
    cost_records.append(recon_record)

    recon_findings, confidence_notes = parse_recon_output(recon_run.final_text)
    confidence_notes = list(confidence_notes)

    if recon_run.anti_bot_marker:
        confidence_notes.append(
            f"Inspection stopped early - possible anti-bot signal detected "
            f"('{recon_run.anti_bot_marker}') during recon. Findings below "
            "reflect only what was gathered before that point."
        )

    # Nothing usable came out of recon - skip codegen entirely rather than
    # spend a second phase's budget writing a script from an empty findings
    # object. This is the "fail fast, don't pay twice for a doomed job" guard.
    if recon_run.status == "failed" or not recon_findings:
        confidence_notes.append(
            "Recon phase did not produce usable findings - codegen phase was "
            "skipped to avoid spending additional budget on a job that can't "
            "succeed. Re-run the job, or check the target site manually, "
            "before retrying."
        )
        combined_cost = combine_cost_records(cost_records)
        return InspectionResult(
            job_id=job.job_id,
            status=recon_run.status if recon_run.status != "completed" else "failed",
            generated_code="",
            requirements_txt="",
            confidence_notes=confidence_notes,
            action_log=action_log,
            total_tool_calls=recon_call_counter["count"],
            total_cost_usd=combined_cost["total_cost_usd"],
            cost_record=combined_cost,
            anti_bot_marker=recon_run.anti_bot_marker,
        )

    # ---- Phase 2: codegen (no browser, fresh/small context) ----
    codegen_call_counter = {"count": 0}
    codegen_options = ClaudeAgentOptions(
        system_prompt=CODEGEN_SYSTEM_PROMPT,
        allowed_tools=[],  # text in, text out - no tools offered at all
        disallowed_tools=["Bash", "Write", "Edit", "Read", *RECON_ALLOWED_TOOLS],
        permission_mode="default",
        hooks=build_hooks(
            job, action_log, codegen_call_counter,
            max_steps=CODEGEN_MAX_TURNS, phase_label="codegen", on_log_entry=on_log_entry,
        ),
        max_turns=CODEGEN_MAX_TURNS,
        max_budget_usd=codegen_budget * SDK_BUDGET_SAFETY_MARGIN,
        **({"model": job.codegen_model} if job.codegen_model else {}),
    )

    codegen_run = await _run_agent_session(
        options=codegen_options,
        task_prompt=build_codegen_prompt(job, recon_findings),
        max_seconds=CODEGEN_MAX_SECONDS,
        call_counter=codegen_call_counter,
        max_steps=CODEGEN_MAX_TURNS,
        action_log=action_log,
        detect_anti_bot=False,
    )

    if codegen_run.result_message is not None:
        codegen_record = build_cost_record(job.job_id, codegen_run.result_message, phase="codegen")
    else:
        codegen_record = build_fallback_cost_record(job.job_id, codegen_run.status, phase="codegen")
    cost_sink.record(codegen_record)
    cost_records.append(codegen_record)

    generated_code, requirements_txt, codegen_notes = parse_agent_output(codegen_run.final_text)
    confidence_notes.extend(codegen_notes)

    # Recon-side issues (anti-bot / time / step / budget) explain why
    # findings might be thin, so they take priority in the reported status;
    # otherwise reflect whatever happened in codegen.
    status = recon_run.status if recon_run.status != "completed" else codegen_run.status

    if status == "budget_limit_hit":
        confidence_notes.append(
            f"Job hit its ${total_budget:.2f} total budget ceiling "
            f"(recon ${recon_budget:.2f} / codegen ${codegen_budget:.2f}) "
            "before finishing - generated_code (if any) may be incomplete."
        )

    combined_cost = combine_cost_records(cost_records)

    return InspectionResult(
        job_id=job.job_id,
        status=status,
        generated_code=generated_code,
        requirements_txt=requirements_txt,
        confidence_notes=confidence_notes,
        action_log=action_log,
        total_tool_calls=recon_call_counter["count"] + codegen_call_counter["count"],
        total_cost_usd=combined_cost["total_cost_usd"],
        cost_record=combined_cost,
        anti_bot_marker=recon_run.anti_bot_marker,
    )


# --------------------------------------------------------------------------
# CLI entry point for local testing against a real Playwright MCP server
# --------------------------------------------------------------------------

async def _demo():
    require_credentials_at_startup()  # fail fast if ANTHROPIC_API_KEY isn't set

    job = InspectionJob(
        job_id="demo-1",
        target_url="https://www.samsung.com/in/smartphones/all-smartphones/",
        sample_keyword="Galaxy F70",
        search_rules="Match products whose title/slug contains the keyword.",
        filter_rules="None.",
        navigation_rules="Listing page -> detail page links -> visit each.",
        extraction_rules="All spec attribute/value pairs, name, model code, price.",
        max_budget_usd=0.50,
    )
    result = await run_inspection_job(job)
    print(f"Status: {result.status}")
    print(f"Tool calls: {result.total_tool_calls}")
    print(f"Cost: ${result.total_cost_usd}")
    if result.cost_record and result.cost_record.get("phases"):
        for phase in result.cost_record["phases"]:
            print(f"  - {phase.get('phase') or 'unknown'}: ${phase.get('total_cost_usd')} "
                  f"({phase.get('num_turns')} turns, "
                  f"cache_read={phase.get('cache_read_input_tokens')})")
    print(f"Confidence notes: {result.confidence_notes}")
    Path(f"{job.job_id}_generated.py").write_text(result.generated_code, encoding="utf-8")
    Path(f"{job.job_id}_action_log.json").write_text(
        json.dumps(result.action_log, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    try:
        asyncio.run(_demo())
    except CredentialError as exc:
        print(f"[inspection-service] Startup aborted: {exc}")
        raise SystemExit(1)
