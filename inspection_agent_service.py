"""
Live Inspection & Codegen Agent Service (OpenAI + MCP Powered)
==============================================================
Backend service that drives an autonomous browser-inspection + code-generation
session using OpenAI (default: GPT-5.6 Luna for both Recon and Codegen) and the
Model Context Protocol (MCP) for Playwright browser automation.

Architectural highlights:
  - OpenAI Client (openai.AsyncOpenAI) for model completions & tool calling.
  - Playwright MCP server over HTTP/SSE (or Stdio) for browser tools.
  - Two-phase architecture:
      1. RECON phase: Browser tools enabled. Inspects live target page, navigates,
         extracts selectors and sample data into a compact structured JSON findings object.
      2. CODEGEN phase: Fresh context, zero browser tools. Writes the standalone Python
         scraper strictly adhering to the universal SCRIPT_CONTRACT.
  - Independent model configurability (OPENAI_RECON_MODEL, OPENAI_CODEGEN_MODEL, default: gpt-5.6-luna).
  - Default temperature: 1.0 (optimized for Luna).
  - PreToolUse hooks for audit logging, SSE live streaming, step capping, and duplicate-call dedup.
  - Real-time anti-bot detection for fast failover.
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageToolCall

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from auth_config import resolve_credentials, require_credentials_at_startup, CredentialError
from cost_metering import (
    CostRecord,
    CostSink,
    StdoutCostSink,
    build_cost_record,
    build_fallback_cost_record,
    combine_cost_records,
    resolve_budget_ceiling,
    split_budget_ceiling,
    calculate_cost_usd,
    SDK_BUDGET_SAFETY_MARGIN,
)

# --------------------------------------------------------------------------
# Model & Temperature Configuration (OpenAI)
# --------------------------------------------------------------------------

def get_default_model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-5.6-luna")

def get_default_recon_model() -> str:
    return os.getenv("OPENAI_RECON_MODEL", get_default_model())

def get_default_codegen_model() -> str:
    return os.getenv("OPENAI_CODEGEN_MODEL", get_default_model())

def get_default_temperature() -> float:
    try:
        return float(os.getenv("OPENAI_TEMPERATURE", "1.0"))
    except ValueError:
        return 1.0

DEFAULT_MODEL = get_default_model()
DEFAULT_RECON_MODEL = get_default_recon_model()
DEFAULT_CODEGEN_MODEL = get_default_codegen_model()
DEFAULT_TEMPERATURE = get_default_temperature()


# --------------------------------------------------------------------------
# Job contract
# --------------------------------------------------------------------------

@dataclass
class InspectionJob:
    """Mirrors the fields submitted from the Builder page."""
    job_id: str
    target_url: str
    sample_keyword: str | None = None
    search_rules: str = ""
    filter_rules: str = ""
    navigation_rules: str = ""
    extraction_rules: str = ""
    max_steps: int = 40          # hard cap on RECON phase tool calls
    max_seconds: int = 600       # hard cap on wall-clock time for the RECON phase
    max_budget_usd: float | None = None  # hard cap on TOTAL spend across both phases
    recon_model: str | None = None    # optional model override for Phase 1 (Recon)
    codegen_model: str | None = None  # optional model override for Phase 2 (Codegen)
    temperature: float | None = None  # optional temperature override (default: 1.0)


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
    cost_record: dict | None = None
    anti_bot_marker: str | None = None


# --------------------------------------------------------------------------
# Playwright MCP Server Configuration & Allowed Tools
# --------------------------------------------------------------------------

RECON_ALLOWED_TOOLS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_wait_for",
    "browser_network_requests",
    "browser_take_screenshot",
    # Prefixed aliases for compatibility
    "mcp__browser__browser_navigate",
    "mcp__browser__browser_snapshot",
    "mcp__browser__browser_click",
    "mcp__browser__browser_type",
    "mcp__browser__browser_press_key",
    "mcp__browser__browser_wait_for",
    "mcp__browser__browser_network_requests",
    "mcp__browser__browser_take_screenshot",
]

REPEATABLE_WITHOUT_PROGRESS = {
    "browser_snapshot",
    "browser_network_requests",
    "browser_take_screenshot",
    "mcp__browser__browser_snapshot",
    "mcp__browser__browser_network_requests",
    "mcp__browser__browser_take_screenshot",
}

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


# --------------------------------------------------------------------------
# Prompt Construction
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
key, not prose paragraphs):
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

CODEGEN_MAX_TURNS = 6
CODEGEN_MAX_SECONDS = 180


# --------------------------------------------------------------------------
# Output Parsing
# --------------------------------------------------------------------------

def parse_agent_output(final_text: str) -> tuple[str, str, list[str]]:
    """Extracts (generated_code, requirements_txt, confidence_notes) with fallbacks."""
    text = (final_text or "").strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return (
                parsed.get("generated_code", ""),
                parsed.get("requirements_txt", ""),
                parsed.get("confidence_notes", []) if isinstance(parsed.get("confidence_notes"), list) else [],
            )
    except (json.JSONDecodeError, TypeError):
        pass

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1))
            notes = parsed.get("confidence_notes", []) if isinstance(parsed.get("confidence_notes"), list) else []
            notes.append("Agent wrapped its JSON in a markdown fence instead of returning it raw - extracted automatically.")
            return parsed.get("generated_code", ""), parsed.get("requirements_txt", ""), notes
        except (json.JSONDecodeError, TypeError):
            pass

    code_match = re.search(r"```python\s*(.*?)\s*```", text, re.DOTALL)
    if code_match:
        return code_match.group(1), "", [
            "Agent did not return valid JSON - extracted code from a ```python "
            "fenced block in its response instead. requirements_txt could not "
            "be recovered this way - check the script's imports manually."
        ]

    return "", "", [
        "Agent response could not be parsed as JSON or a fenced code block - "
        "no usable code was extracted. Re-running the job often resolves this."
    ]


def parse_recon_output(final_text: str) -> tuple[dict, list[str]]:
    """Extracts structured JSON findings from recon phase."""
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

    capped = text[:4000]
    truncated_note = " (truncated)" if len(text) > 4000 else ""
    return (
        {"raw_recon_notes": capped},
        [f"Recon phase did not return structured JSON findings{truncated_note} - "
         "passing raw text to codegen as best effort."],
    )


# --------------------------------------------------------------------------
# MCP Client & Tool Execution
# --------------------------------------------------------------------------

class MCPClientManager:
    """Manages connection to Playwright MCP Server (HTTP/SSE or Stdio)."""

    def __init__(self, base_url: str = "http://localhost:8931"):
        self.base_url = base_url.rstrip("/")
        self.session: ClientSession | None = None
        self._exit_stack = None

    async def connect(self):
        if not MCP_AVAILABLE:
            print("[inspection-service] Warning: mcp library not available in environment.")
            return None

        # 1. Try SSE HTTP transport
        endpoints_to_try = [
            f"{self.base_url}/sse",
            f"{self.base_url}/mcp",
            f"{self.base_url}",
        ]
        for url in endpoints_to_try:
            try:
                self._exit_stack = asyncio.LifoQueue()
                # Create sse client context
                sse_ctx = sse_client(url)
                read, write = await sse_ctx.__aenter__()
                session = ClientSession(read, write)
                await session.__aenter__()
                await session.initialize()
                self.session = session
                print(f"[inspection-service] Connected to Playwright MCP via {url}")
                return self.session
            except Exception:
                continue

        # 2. Try Stdio fallback
        try:
            params = StdioServerParameters(
                command="npx",
                args=["-y", "@playwright/mcp@latest", "--isolated"],
            )
            stdio_ctx = stdio_client(params)
            read, write = await stdio_ctx.__aenter__()
            session = ClientSession(read, write)
            await session.__aenter__()
            await session.initialize()
            self.session = session
            print("[inspection-service] Connected to Playwright MCP via stdio process")
            return self.session
        except Exception:
            return None

    async def close(self):
        if self.session:
            try:
                await self.session.__aexit__(None, None, None)
            except Exception:
                pass
            self.session = None

NATIVE_BROWSER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Navigate the browser to a target URL and inspect the rendered page.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "The URL to navigate to"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": "Capture a structured text snapshot of the current page including headings, listing elements, detail attributes, links, and forms.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element or modal consent button on the page.",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string", "description": "CSS selector or element description to click"}},
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into a search bar or input field.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the input element"},
                    "text": {"type": "string", "description": "Text to type into the element"},
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_press_key",
            "description": "Press a keyboard key such as 'Enter' or 'Escape'.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Key name to press"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait_for",
            "description": "Wait for a specific selector or dynamic element to render.",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string", "description": "CSS selector to wait for"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_network_requests",
            "description": "List all recorded network requests, JSON API endpoints, and XHR requests captured during navigation.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_take_screenshot",
            "description": "Capture a screenshot of the currently visible viewport.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class NativePythonBrowserSession:
    """
    Pure Python browser inspection session (fallback when external Playwright MCP server is not running).
    Uses httpx + BeautifulSoup to parse HTML, detect JSON-LD, analyze DOM hierarchy, and discover API endpoints.
    """
    def __init__(self):
        self.current_url: str = ""
        self.current_html: str = ""
        self.network_logs: list[dict] = []
        self.client = None

    async def initialize(self):
        import httpx
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
            timeout=25.0,
        )

    async def close(self):
        if self.client:
            await self.client.aclose()
            self.client = None

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        clean_name = tool_name.replace("mcp__browser__", "")
        
        if clean_name == "browser_navigate":
            url = arguments.get("url", "")
            if not url:
                return "Error: No URL provided."
            self.current_url = url
            try:
                if not self.client:
                    await self.initialize()
                resp = await self.client.get(url)
                self.current_html = resp.text
                self.network_logs.append({
                    "url": url,
                    "status": resp.status_code,
                    "content_type": resp.headers.get("content-type", ""),
                    "size_bytes": len(resp.content),
                })
                
                # Check for anti-bot
                if resp.status_code in (403, 429):
                    return f"Navigated to {url}. Received HTTP Status {resp.status_code} (Possible Access Denied / Cloudflare / 403 Forbidden)."
                
                return f"Navigated to {url}. HTTP Status {resp.status_code}. Page length: {len(self.current_html)} characters."
            except Exception as exc:
                return f"Navigation failed for {url}: {exc}"

        elif clean_name == "browser_snapshot":
            if not self.current_html:
                return "No page loaded. Call browser_navigate first."
            
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(self.current_html, "html.parser")
                
                title = soup.title.string.strip() if soup.title and soup.title.string else "No title"
                
                # Extract meta JSON-LD or API state
                scripts_json = []
                for s in soup.find_all("script", type="application/ld+json"):
                    if s.string:
                        scripts_json.append(s.string.strip()[:300])
                
                # Headings
                headings = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])[:10] if h.get_text(strip=True)]
                
                # Products / Listing containers
                sample_items = []
                for card in soup.select("[class*='product'], [class*='item'], [class*='card'], article, li")[:8]:
                    text = card.get_text(" ", strip=True)
                    if len(text) > 20 and len(text) < 300:
                        sample_items.append(text)
                
                # Links & Selectors
                sample_links = [a.get("href") for a in soup.find_all("a", href=True)[:8]]
                
                snapshot = {
                    "url": self.current_url,
                    "title": title,
                    "headings": headings[:6],
                    "sample_product_elements": sample_items[:5],
                    "sample_links": sample_links[:6],
                    "has_json_ld": len(scripts_json) > 0,
                    "json_ld_sample": scripts_json[:1] if scripts_json else [],
                }
                return json.dumps(snapshot, indent=2)
            except Exception as exc:
                return f"Snapshot parsing error: {exc}. Raw snippet: {self.current_html[:1500]}"

        elif clean_name in ("browser_click", "browser_type", "browser_press_key", "browser_wait_for"):
            return f"Action '{clean_name}' simulated on {arguments}. Page state preserved."

        elif clean_name == "browser_network_requests":
            return json.dumps(self.network_logs, indent=2) if self.network_logs else "No network logs recorded."

        elif clean_name == "browser_take_screenshot":
            return f"Screenshot captured for {self.current_url} (DOM rendered)."

        return f"Unknown tool: {tool_name}"


# --------------------------------------------------------------------------
# PreToolUse Hook & Interceptor
# --------------------------------------------------------------------------

class ToolUseInterceptor:
    def __init__(
        self,
        job: InspectionJob,
        action_log: list[dict],
        call_counter: dict,
        max_steps: int,
        phase_label: str = "recon",
        on_log_entry: Callable[[dict], Coroutine[Any, Any, None]] | None = None,
    ):
        self.job = job
        self.action_log = action_log
        self.call_counter = call_counter
        self.max_steps = max_steps
        self.phase_label = phase_label
        self.on_log_entry = on_log_entry
        self.last_call = {"key": None, "repeat_count": 0}

    async def before_tool_call(self, tool_name: str, tool_input: dict) -> tuple[bool, str | None]:
        self.call_counter["count"] += 1
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "phase": self.phase_label,
            "tool": tool_name,
            "input": tool_input,
            "call_number": self.call_counter["count"],
        }
        self.action_log.append(entry)
        if self.on_log_entry:
            try:
                await self.on_log_entry(entry)
            except Exception:
                pass

        if self.call_counter["count"] > self.max_steps:
            reason = (
                f"Step cap ({self.max_steps}) reached for job {self.job.job_id} "
                f"({self.phase_label} phase). Denying further actions - wrap up findings."
            )
            return False, reason

        if tool_name in REPEATABLE_WITHOUT_PROGRESS:
            try:
                call_key = (tool_name, json.dumps(tool_input, sort_keys=True, default=str))
            except TypeError:
                call_key = (tool_name, str(tool_input))

            if call_key == self.last_call["key"]:
                self.last_call["repeat_count"] += 1
            else:
                self.last_call["repeat_count"] = 0
            self.last_call["key"] = call_key

            if self.last_call["repeat_count"] >= 1:
                reason = (
                    f"{tool_name} was just called with identical arguments - "
                    "that result is already in context. Act on observed data rather than re-fetching."
                )
                return False, reason
        else:
            self.last_call["key"] = None
            self.last_call["repeat_count"] = 0

        return True, None


# --------------------------------------------------------------------------
# Single-Phase Runner (OpenAI Execution Loop)
# --------------------------------------------------------------------------

@dataclass
class _PhaseRun:
    status: str
    final_text: str
    tool_calls: int
    usage_info: Any
    anti_bot_marker: str | None = None


async def _run_openai_recon_session(
    *,
    client: AsyncOpenAI,
    model: str,
    temperature: float,
    system_prompt: str,
    task_prompt: str,
    max_seconds: int,
    max_steps: int,
    interceptor: ToolUseInterceptor,
    mcp_base_url: str = "http://localhost:8931",
) -> _PhaseRun:
    start_time = time.monotonic()
    status = "completed"
    final_text = ""
    anti_bot_marker = None
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cached_tokens = 0
    turns_count = 0

    mcp_manager = MCPClientManager(base_url=mcp_base_url)
    mcp_session = await mcp_manager.connect()
    native_session = None

    openai_tools = []
    if mcp_session:
        try:
            tools_list = await mcp_session.list_tools()
            filtered_tools = [
                t for t in tools_list.tools
                if t.name in RECON_ALLOWED_TOOLS or any(t.name.endswith(a) for a in RECON_ALLOWED_TOOLS)
            ]
            openai_tools = _convert_mcp_tools_to_openai(filtered_tools)
        except Exception as exc:
            print(f"[inspection-service] Failed listing MCP tools: {exc}")

    if not openai_tools:
        # Fallback to pure Python browser inspection tools
        native_session = NativePythonBrowserSession()
        await native_session.initialize()
        openai_tools = NATIVE_BROWSER_TOOLS
        print("[inspection-service] Using Native Python Browser Tools for Recon phase")

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
    ]

    try:
        while True:
            turns_count += 1
            if time.monotonic() - start_time > max_seconds:
                status = "time_limit_hit"
                break

            if interceptor.call_counter["count"] >= max_steps:
                status = "step_limit_hit"
                break

            create_kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
            }
            if openai_tools:
                create_kwargs["tools"] = openai_tools
                create_kwargs["reasoning_effort"] = "none"

            try:
                response = await client.chat.completions.create(**create_kwargs)
            except Exception as api_exc:
                err_text = str(api_exc)
                if "reasoning_effort" in err_text and "tools" in create_kwargs:
                    create_kwargs["reasoning_effort"] = "none"
                    response = await client.chat.completions.create(**create_kwargs)
                elif "temperature" in err_text:
                    create_kwargs.pop("temperature", None)
                    response = await client.chat.completions.create(**create_kwargs)
                else:
                    raise api_exc

            # Accumulate usage
            if response.usage:
                total_prompt_tokens += response.usage.prompt_tokens or 0
                total_completion_tokens += response.usage.completion_tokens or 0
                prompt_details = getattr(response.usage, "prompt_tokens_details", None)
                if prompt_details and hasattr(prompt_details, "cached_tokens"):
                    total_cached_tokens += getattr(prompt_details, "cached_tokens", 0) or 0

            choice = response.choices[0]
            msg = choice.message

            if msg.tool_calls:
                # Assistant message with tool calls
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                })

                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        fn_args = {}

                    allowed, deny_reason = await interceptor.before_tool_call(fn_name, fn_args)
                    if not allowed:
                        tool_result_str = f"DENIED: {deny_reason}"
                    elif mcp_session is not None:
                        try:
                            mcp_res = await mcp_session.call_tool(fn_name, fn_args)
                            parts = []
                            for c in getattr(mcp_res, "content", []):
                                if hasattr(c, "text"):
                                    parts.append(str(c.text))
                                elif isinstance(c, dict):
                                    parts.append(str(c.get("text", "")))
                                else:
                                    parts.append(str(c))
                            tool_result_str = "\n".join(parts) or "Tool execution completed."
                        except Exception as tool_exc:
                            tool_result_str = f"Tool execution error: {tool_exc}"
                    elif native_session is not None:
                        try:
                            tool_result_str = await native_session.call_tool(fn_name, fn_args)
                        except Exception as native_exc:
                            tool_result_str = f"Native browser action error: {native_exc}"
                    else:
                        tool_result_str = "No browser session available."


                    # Anti-bot detection
                    marker = _detect_anti_bot_signal(tool_result_str)
                    if marker:
                        anti_bot_marker = marker

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result_str,
                    })

                if anti_bot_marker:
                    status = "anti_bot_detected"
                    break
            else:
                # Final text output
                final_text = msg.content or ""
                break

    except Exception as exc:
        interceptor.action_log.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "error": str(exc),
        })
        status = "failed"
    finally:
        await mcp_manager.close()

    usage_summary = {
        "input_tokens": total_prompt_tokens,
        "output_tokens": total_completion_tokens,
        "cached_tokens": total_cached_tokens,
        "num_turns": turns_count,
        "duration_ms": int((time.monotonic() - start_time) * 1000),
        "model": model,
        "subtype": status,
        "is_error": (status == "failed"),
    }

    return _PhaseRun(
        status=status,
        final_text=final_text,
        tool_calls=interceptor.call_counter["count"],
        usage_info=usage_summary,
        anti_bot_marker=anti_bot_marker,
    )


async def _run_openai_codegen_session(
    *,
    client: AsyncOpenAI,
    model: str,
    temperature: float,
    system_prompt: str,
    task_prompt: str,
    max_seconds: int = CODEGEN_MAX_SECONDS,
) -> _PhaseRun:
    start_time = time.monotonic()
    status = "completed"
    final_text = ""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
    ]

    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cached_tokens = 0

    try:
        create_kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(**create_kwargs),
                timeout=max_seconds,
            )
        except Exception as api_exc:
            if "temperature" in str(api_exc):
                create_kwargs.pop("temperature", None)
                response = await asyncio.wait_for(
                    client.chat.completions.create(**create_kwargs),
                    timeout=max_seconds,
                )
            else:
                raise api_exc

        if response.usage:
            total_prompt_tokens = response.usage.prompt_tokens or 0
            total_completion_tokens = response.usage.completion_tokens or 0
            prompt_details = getattr(response.usage, "prompt_tokens_details", None)
            if prompt_details and hasattr(prompt_details, "cached_tokens"):
                total_cached_tokens = getattr(prompt_details, "cached_tokens", 0) or 0

        final_text = response.choices[0].message.content or ""

    except asyncio.TimeoutError:
        status = "time_limit_hit"
    except Exception as exc:
        status = "failed"
        final_text = f"Codegen error: {exc}"

    usage_summary = {
        "input_tokens": total_prompt_tokens,
        "output_tokens": total_completion_tokens,
        "cached_tokens": total_cached_tokens,
        "num_turns": 1,
        "duration_ms": int((time.monotonic() - start_time) * 1000),
        "model": model,
        "subtype": status,
        "is_error": (status == "failed"),
    }

    return _PhaseRun(
        status=status,
        final_text=final_text,
        tool_calls=0,
        usage_info=usage_summary,
        anti_bot_marker=None,
    )


# --------------------------------------------------------------------------
# Main Entry Point: run_inspection_job
# --------------------------------------------------------------------------

async def run_inspection_job(
    job: InspectionJob,
    mcp_base_url: str = "http://localhost:8931",
    cost_sink: CostSink | None = None,
    on_log_entry: Callable[[dict], Coroutine[Any, Any, None]] | None = None,
) -> InspectionResult:
    cost_sink = cost_sink or StdoutCostSink()
    action_log: list[dict] = []
    cost_records: list[CostRecord] = []

    # Resolve OpenAI credentials & Client
    creds = resolve_credentials()
    client = AsyncOpenAI(
        api_key=creds.api_key,
        base_url=creds.base_url,
        organization=creds.org_id,
        project=creds.project_id,
    )

    # Resolve Models and Temperature
    recon_model = job.recon_model or get_default_recon_model()
    codegen_model = job.codegen_model or get_default_codegen_model()
    temperature = job.temperature if job.temperature is not None else get_default_temperature()

    total_budget = resolve_budget_ceiling(job.max_budget_usd)
    recon_budget, codegen_budget = split_budget_ceiling(total_budget)

    # ---- Phase 1: Recon (Browser Tools) ----
    recon_call_counter = {"count": 0}
    recon_interceptor = ToolUseInterceptor(
        job=job,
        action_log=action_log,
        call_counter=recon_call_counter,
        max_steps=job.max_steps,
        phase_label="recon",
        on_log_entry=on_log_entry,
    )

    recon_run = await _run_openai_recon_session(
        client=client,
        model=recon_model,
        temperature=temperature,
        system_prompt=RECON_SYSTEM_PROMPT,
        task_prompt=build_recon_prompt(job),
        max_seconds=job.max_seconds,
        max_steps=job.max_steps,
        interceptor=recon_interceptor,
        mcp_base_url=mcp_base_url,
    )

    recon_record = build_cost_record(
        job_id=job.job_id,
        usage_info=recon_run.usage_info,
        phase="recon",
        model=recon_model,
    )
    cost_sink.record(recon_record)
    cost_records.append(recon_record)

    recon_findings, confidence_notes = parse_recon_output(recon_run.final_text)
    confidence_notes = list(confidence_notes)

    if recon_run.anti_bot_marker:
        confidence_notes.append(
            f"Inspection stopped early - possible anti-bot signal detected "
            f"('{recon_run.anti_bot_marker}') during recon. Findings reflect "
            "only data gathered prior to block."
        )

    # Fail fast guard if recon produced zero findings or failed
    if recon_run.status == "failed" or not recon_findings:
        confidence_notes.append(
            "Recon phase did not produce usable findings - codegen phase was "
            "skipped to avoid spending additional budget."
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

    # ---- Phase 2: Codegen (Fresh Context) ----
    codegen_run = await _run_openai_codegen_session(
        client=client,
        model=codegen_model,
        temperature=temperature,
        system_prompt=CODEGEN_SYSTEM_PROMPT,
        task_prompt=build_codegen_prompt(job, recon_findings),
        max_seconds=CODEGEN_MAX_SECONDS,
    )

    codegen_record = build_cost_record(
        job_id=job.job_id,
        usage_info=codegen_run.usage_info,
        phase="codegen",
        model=codegen_model,
    )
    cost_sink.record(codegen_record)
    cost_records.append(codegen_record)

    generated_code, requirements_txt, codegen_notes = parse_agent_output(codegen_run.final_text)
    confidence_notes.extend(codegen_notes)

    status = recon_run.status if recon_run.status != "completed" else codegen_run.status
    combined_cost = combine_cost_records(cost_records)

    return InspectionResult(
        job_id=job.job_id,
        status=status,
        generated_code=generated_code,
        requirements_txt=requirements_txt,
        confidence_notes=confidence_notes,
        action_log=action_log,
        total_tool_calls=recon_call_counter["count"],
        total_cost_usd=combined_cost["total_cost_usd"],
        cost_record=combined_cost,
        anti_bot_marker=recon_run.anti_bot_marker,
    )


# --------------------------------------------------------------------------
# CLI Demo
# --------------------------------------------------------------------------

async def _demo():
    require_credentials_at_startup()

    job = InspectionJob(
        job_id="demo-openai-1",
        target_url="https://www.samsung.com/in/smartphones/all-smartphones/",
        sample_keyword="Galaxy S24",
        search_rules="Match products whose title/slug contains the keyword.",
        filter_rules="None.",
        navigation_rules="Listing page -> detail page links -> visit each.",
        extraction_rules="All spec attribute/value pairs, name, model code, price.",
        max_budget_usd=0.50,
        recon_model="gpt-5.6-luna",
        codegen_model="gpt-5.6-luna",
        temperature=1.0,
    )
    result = await run_inspection_job(job)
    print(f"Status: {result.status}")
    print(f"Tool calls: {result.total_tool_calls}")
    print(f"Cost: ${result.total_cost_usd}")
    print(f"Confidence notes: {result.confidence_notes}")


if __name__ == "__main__":
    try:
        asyncio.run(_demo())
    except CredentialError as exc:
        print(f"[inspection-service] Startup aborted: {exc}")
        raise SystemExit(1)
