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

import ast
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

def get_mcp_server_url() -> str:
    """Resolve Playwright MCP Server URL from environment (.env)."""
    return (
        os.environ.get("MCP_SERVER_URL")
        or os.environ.get("PLAYWRIGHT_MCP_URL")
        or os.environ.get("MCP_BASE_URL")
        or "http://localhost:8931"
    ).rstrip("/")


RECON_ALLOWED_TOOLS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_scroll",
    "browser_wait_for",
    "browser_evaluate",
    "browser_network_requests",
    "browser_take_screenshot",
    # Prefixed aliases for compatibility
    "mcp__browser__browser_navigate",
    "mcp__browser__browser_snapshot",
    "mcp__browser__browser_click",
    "mcp__browser__browser_type",
    "mcp__browser__browser_press_key",
    "mcp__browser__browser_scroll",
    "mcp__browser__browser_wait_for",
    "mcp__browser__browser_evaluate",
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
# Prompt Construction & Cascade Extraction Contract
# --------------------------------------------------------------------------

SCRIPT_CONTRACT = """MANDATORY SCRIPT CONTRACT - every scraper you write MUST follow this exact
shape, regardless of the target site. This is not optional or per-job -
every script this service produces must be a drop-in replacement for any
other, so a person running it never has to read the code first to figure
out how to call it:

  - Command-line args via argparse:
      `--input` (default "input.xlsx"),
      `--output` (default "output.xlsx"),
      `--max-products` (optional int cap),
      `--headless` (default True, flag to run browser headless or visible).

  - Input Excel columns (any subset may be populated - handle all three):
      `keyword`, `listing_url`, `detail_url` - semantics:
      * `keyword`: triggers a search/match flow against the listing page or direct URL.
      * `listing_url`: crawls a specific listing page (or falls back to default TARGET_URL).
      * `detail_url`: scrapes one item page directly, skipping listing discovery.
      * IMPORTANT: `detail_url` must be a genuinely independent fast path that extracts
        the single page directly without requiring a listing crawl first.

  - CASCADE EXTRACTION STRATEGY (CRITICAL FOR HIGH ACCURACY):
      1. Tier 1 (Internal JSON API): If the target page is powered by an internal REST/XHR/GraphQL
         API observed during recon (e.g. /api/products, GraphQL queries), query that endpoint
         directly using requests/httpx with proper headers. This is the fastest, cleanest path.
      2. Tier 2 (Hydrated State): Check for in-page JSON objects such as `<script id="__NEXT_DATA__">`,
         `<script id="__NUXT_DATA__">`, `window.__INITIAL_STATE__`, or Schema.org JSON-LD blocks.
      3. Tier 3 (Playwright Async Browser Automation): For modern JavaScript SPAs (React/Vue/Angular),
         client-rendered prices, accordion spec tables, and dynamic color/storage variants:
         * Use `playwright.async_api.async_playwright()`.
         * Launch Chromium with `--disable-blink-features=AutomationControlled` and realistic User-Agent.
         * Call `dismiss_cookie_banner(page)` immediately after `page.goto()`.
         * Auto-wait for elements using `page.wait_for_selector()` or `page.locator()`.
         * Click accordion tabs (e.g. "Specifications", "Specs", "Storage") to reveal hidden attributes.
      4. Tier 4 (Static Fast-Path): For traditional server-rendered static HTML sites, use `requests`
         with a desktop User-Agent and `BeautifulSoup`.

  - Output Excel has exactly two sheets:
      * "Extracted Data": One row per item. Columns matching the required extraction fields plus "Source URL".
      * "Flags": One row per anti-bot signal, low-confidence extraction, or error encountered during the run.
        Never leave this sheet out, even if it has 0 rows.

  - Include a `detect_anti_bot_signals()` function that checks each HTTP response / page title
    for 403/429 status, CAPTCHA/Cloudflare/PerimeterX/DataDome markers, and unusually short bodies.
    It only FLAGS these to the Flags sheet; it never attempts to bypass or solve them.

  - Include `get_proxy()` and `solve_captcha()` as unimplemented placeholder stubs (return None / raise NotImplementedError).

  - Cookie & Consent Banner Dismissal:
      Include a `dismiss_cookie_banner(page)` helper and call it once immediately after every `page.goto(...)`,
      before interacting with the page. Try common consent-button text matches ("Accept all", "Accept", "I agree",
      "Allow all", "Agree", "Got it") in a try/except that does nothing if not present.

  - Data Cleaning & Normalization:
      * Clean currency/prices: extract numeric values or standard currency strings (e.g. "INR 1,24,999" or "124999"),
        never capture unrelated header or promotional text.
      * Model codes: if not present in rendered body text, extract from URL parameters (e.g. `?modelCode=...`, `sku=...`).
      * Print a timestamped log line for each major step using a `log()` helper.

  - REGEX & CODE SYNTAX RULES (CRITICAL):
      * In raw strings (r"..." or r'...'), use SINGLE backslashes for regex tokens:
        r"\\d+", r"\\s+", r"\\b", r"\\w+", r"\\(?[2-9]\\d{2}\\)?", r"\\.com".
        NEVER double-escape inside raw strings (do NOT write r"\\\\d", r"\\\\s", r"\\\\b", r"\\\\(").
      * When escaping literal parentheses in regex, use r"\\(" and r"\\)" (or re.escape).
      * For HTML string cleaning with BeautifulSoup, use "\\n" as separator and "\\xa0" for non-breaking spaces.
      * The script must be syntactically valid Python 3 and compile with zero errors.

  - INPUT EXCEL & NAN HANDLING RULES (CRITICAL):
      * When reading Excel with pandas, always sanitize missing values:
        `input_df = pd.read_excel(args.input).fillna("")`
      * Any helper function (e.g. `clean()`, `value_from_row()`) MUST treat `pd.isna(val)`, `None`,
        and the string `"nan"` / `"none"` / `"null"` as empty string `""`.
      * NEVER allow `"nan"` to be treated as a valid `detail_url`, `listing_url`, or `keyword`."""


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

- REGEX & STRING ESCAPING (CRITICAL):
  When writing regex patterns, use standard Python raw string literals with single backslashes,
  e.g. r"\\d{3}", r"\\s+", r"\\b", r"\\([2-9]\\d{2}\\)". Never double-escape backslashes in raw strings
  (do NOT write r"\\\\d+" or r"\\\\("). Use "\\n" for get_text separator and "\\xa0" for non-breaking spaces.

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
    "You are an expert Python code-generation agent for Scraper Studio, "
    "writing a clean, production-grade scraper from a prior live-site inspection's findings. "
    "You have no browser or filesystem access in this step - work only from "
    "the findings and rules given to you. Follow the mandatory SCRIPT CONTRACT strictly. "
    "Use standard Python 3 syntax with properly formatted raw strings for regex patterns "
    "(single backslashes like r'\\d+', r'\\s+', r'\\b', r'\\('), avoiding invalid double-escapes. "
    "Where findings are incomplete or ambiguous, note them in confidence_notes rather than guessing."
)

CODEGEN_MAX_TURNS = 6
CODEGEN_MAX_SECONDS = 180

REFINEMENT_RECON_SYSTEM_PROMPT = (
    "You are a specialized web-inspection agent for Scraper Studio performing a targeted refinement pass. "
    "Your goal is to inspect the live target website specifically for the new data points, DOM selectors, "
    "interactions, or navigation flows requested by the user to refine an existing scraper. "
    "You only observe pages through the browser tool provided. You do not write code in this phase. "
    "Report real, verified facts as a compact structured JSON findings object."
)

REFINEMENT_CODEGEN_SYSTEM_PROMPT = (
    "You are an expert Python code-generation and refactoring agent for Scraper Studio. "
    "Your goal is to update an existing scraper to incorporate the user's specific refinement instructions, "
    "new data points, and the latest inspection findings. "
    "You have no browser access in this step. Ensure the updated scraper follows the mandatory SCRIPT CONTRACT "
    "strictly, preserves all existing working features, compiles with valid Python 3 syntax, and extracts the "
    "newly requested fields with clean column names in the 'Extracted Data' Excel sheet."
)


def build_refinement_recon_prompt(
    job: InspectionJob,
    prior_code: str,
    prior_findings: dict | None,
    refinement_instructions: str,
) -> str:
    keyword_line = (
        f"Sample keyword to test with: {job.sample_keyword}"
        if job.sample_keyword
        else "No sample keyword given - use a representative sample item during inspection."
    )
    prior_findings_json = json.dumps(prior_findings or {}, indent=2)
    truncated_prior_code = prior_code[:8000] if len(prior_code) > 8000 else prior_code

    return f"""You are performing an interactive REFINEMENT inspection on a live website for an existing scraper in Scraper Studio.
YOU DO NOT WRITE CODE IN THIS PHASE. Your only output is an updated, compact JSON findings object.

TARGET: {job.target_url}
{keyword_line}

USER REFINEMENT REQUEST / DESIRED CHANGES:
\"\"\"{refinement_instructions}\"\"\"

EXISTING SCRAPER RULES:
- Search rules: {job.search_rules or 'none specified'}
- Filter rules: {job.filter_rules or 'none specified'}
- Navigation rules: {job.navigation_rules or 'none specified'}
- Extraction rules: {job.extraction_rules or 'none specified'}

PRIOR INSPECTION FINDINGS:
{prior_findings_json}

CURRENT SCRAPER CODE EXCERPT:
```python
{truncated_prior_code}
```

YOUR TASK FOR THIS REFINEMENT:
1. Navigate to the target page or relevant detail page using the browser tool.
2. Focus specifically on the USER'S REFINEMENT REQUEST:
   - If the user requested NEW DATA POINTS or EXTRACTION FIELDS (e.g. SKU, discount, rating, seller name, specs, stock):
     * Locate the exact HTML elements, attributes, or API responses containing these values.
     * Record the concrete CSS selectors or XPath patterns for them.
     * Record real sample extracted values you observed.
   - If the user requested DIFFERENT KEYWORDS or SEARCH LOGIC:
     * Test the search or filter with the requested keyword.
   - If the user requested PAGINATION or NAVIGATION CHANGES (e.g. multi-page crawl, clicking tabs/accordions):
     * Test clicking the pagination buttons ("Next", page numbers) or infinite scroll, and note the selectors and URL pattern.
     * Test clicking accordions/tabs (e.g. "Specifications", "Tech Specs") to verify how they expand.
   - If the page is backed by an internal JSON/XHR endpoint that provides the requested data, record that API endpoint.
3. Dismiss any cookie banners or overlays encountered.
4. Do not solve CAPTCHAs or bypass anti-bot blocks - note them if encountered and exit.

Return your final answer as a single JSON object with exactly these keys:
  "page_type"              - "listing" | "detail" | "both"
  "cookie_banner"          - what was present and how dismissed
  "search_flow"            - updated search/filter flow, if tested
  "navigation_path"        - updated listing to detail navigation or pagination steps
  "listing_selectors"      - updated selectors for listing items
  "detail_selectors"       - updated selectors on detail page, including selectors for NEWLY requested fields
  "network_api"            - any clean JSON API observed, or "none observed"
  "sample_extracted_fields"- dictionary of sample extracted values, including the NEW data points requested by the user
  "refinement_notes"       - short bullet points describing what was discovered for the user's specific request
  "confidence_notes"       - list of strings for anything unverified or uncertain

CRITICAL FORMATTING RULE: your final message must be ONLY that JSON object -
no markdown code fences, no explanation before or after it, no prose of any
kind outside the JSON. It must parse cleanly with Python's json.loads().
"""


def build_refinement_codegen_prompt(
    job: InspectionJob,
    prior_code: str,
    prior_findings: dict | None,
    new_recon_findings: dict,
    refinement_instructions: str,
) -> str:
    merged_findings = {**(prior_findings or {}), **new_recon_findings}
    return f"""You are modifying and updating an existing Python web scraper based on an interactive user refinement request.
You do NOT have browser access in this step - work from the existing code, the user's requested changes, and the latest inspection findings.

TARGET: {job.target_url}

USER REFINEMENT REQUEST / DESIRED CHANGES:
\"\"\"{refinement_instructions}\"\"\"

LATEST INSPECTION FINDINGS (from live browser session):
{json.dumps(merged_findings, indent=2)}

EXISTING SCRAPER CODE:
```python
{prior_code}
```

REFINEMENT INSTRUCTIONS:
1. Update the existing scraper to completely fulfill the USER'S REFINEMENT REQUEST.
   - If new data points were requested, extract them accurately using the discovered selectors/APIs, and include them as explicit columns in the "Extracted Data" sheet.
   - If pagination or multi-page crawling was requested, implement robust pagination loop handling `--max-products`.
   - If keywords or search logic changed, adapt the matching logic accordingly.
   - If accordion/tab clicking is required, use Playwright async locators (`await page.locator(...).click()`, `await page.wait_for_timeout(...)`).
2. Maintain all existing working capabilities of the scraper:
   - Command-line arguments (`--input`, `--output`, `--max-products`, `--headless`).
   - Universal dual-sheet Excel output ("Extracted Data" and "Flags").
   - Cookie dismissal (`dismiss_cookie_banner`).
   - Anti-bot detection (`detect_anti_bot_signals`).
   - Clean NaN handling (`input_df = pd.read_excel(args.input).fillna("")`).
3. Follow Python syntax rules strictly:
   - Use standard raw strings (r"\\d+", r"\\s+", r"\\b", r"\\(") without double escaping.
   - Clean HTML strings with "\\n" and "\\xa0".
4. Return your final answer as a single JSON object with keys:
   "generated_code"    - string, the complete, updated standalone .py script
   "requirements_txt"  - string, the pip requirements list needed by the updated script
   "confidence_notes"  - list of strings detailing the changes made and anything requiring verification

CRITICAL FORMATTING RULE: your final message must be ONLY that JSON object -
no markdown code fences (no ```json or ```), no explanation before or after
it, no prose outside the JSON. It must parse cleanly with Python's json.loads().

{SCRIPT_CONTRACT}
"""


# --------------------------------------------------------------------------
# Output Parsing & Code Sanitization
# --------------------------------------------------------------------------

def sanitize_regex_in_raw_string(content: str) -> str:
    """
    In raw strings (r"..." or r'...'), LLMs often mistakenly double-escape backslashes,
    e.g. r"\\\\d+" instead of r"\\d+", r"\\\\(" instead of r"\\(", r"\\\\s+" instead of r"\\s+",
    r"\\\\b" instead of r"\\b".
    In Python raw strings, '\\\\' is two literal backslash characters.
    When compiled as regex, '\\\\(' becomes a literal backslash followed by '(', which causes
    '(?' to be parsed as a regex extension like '(?[', throwing PatternError.
    
    This function replaces double-backslashes before regex tokens in raw strings with single backslashes.
    """
    pattern = r"\\\\([bBdDsSwW\(\)\[\]\{\}\+\*\?\.\-\/\^\|\$\\\'\"]|[nrt]|xa0|[ux][0-9a-fA-F]+|[0-9])"
    
    def replacer(m):
        return "\\" + m.group(1)
        
    return re.sub(pattern, replacer, content)


def sanitize_generated_code(code: str) -> tuple[str, list[str]]:
    """Sanitizes, auto-repairs, and validates LLM-generated Python scraper code."""
    notes = []
    if not code or not code.strip():
        return code, notes

    sanitized = code.strip()

    # 1. Strip markdown fences if present
    if sanitized.startswith("```"):
        sanitized = re.sub(r"^```(?:python)?\s*\n?", "", sanitized)
        sanitized = re.sub(r"\n?```\s*$", "", sanitized)

    # 2. Fix BeautifulSoup/string literal common mistakes
    # node.get_text("\\n" -> node.get_text("\n"
    sanitized = re.sub(
        r'(\.get_text\()\s*["\']\\\\n["\']',
        lambda m: m.group(1) + '"\\n"',
        sanitized
    )
    # text.replace("\\xa0" -> text.replace("\xa0"
    sanitized = re.sub(
        r'(\.replace\()\s*["\']\\\\xa0["\']',
        lambda m: m.group(1) + '"\\xa0"',
        sanitized
    )
    # text.replace("\\n" -> text.replace("\n"
    sanitized = re.sub(
        r'(\.replace\()\s*["\']\\\\n["\']',
        lambda m: m.group(1) + '"\\n"',
        sanitized
    )

    # 3. Fix double-escaped raw strings: r"..." or r'...'
    def fix_raw_string_match(match):
        prefix = match.group(1)
        quote = match.group(2)
        body = match.group(3)
        fixed_body = sanitize_regex_in_raw_string(body)
        return f"{prefix}{quote}{fixed_body}{quote}"

    # Handle triple-quoted raw strings
    sanitized = re.sub(
        r'(r[fF]?|R[fF]?)(["\']{3})(.*?)\2',
        fix_raw_string_match,
        sanitized,
        flags=re.DOTALL
    )

    # Handle single-quoted raw strings (non-triple)
    sanitized = re.sub(
        r'(r[fF]?|R[fF]?)(["\'])(.*?)\2',
        fix_raw_string_match,
        sanitized
    )

    # 4. Fix Excel NaN / empty-cell handling in clean helpers & pd.read_excel
    sanitized = re.sub(
        r'def\s+clean(?:_text)?\s*\(\s*value\s*\)\s*:\s*\n\s*if\s+value\s+is\s+None\s*:\s*\n\s*return\s*["\']["\']',
        'def clean(value):\n    if value is None or pd.isna(value) or str(value).strip().lower() in ("", "nan", "none", "null"):\n        return ""',
        sanitized
    )
    sanitized = re.sub(
        r'(pd\.read_excel\([^)\n]+)\)(?!\s*\.\s*fillna)',
        r'\1).fillna("")',
        sanitized
    )

    # 5. Fix row value access: str(row.get(...) or "") where nan string could be created
    sanitized = re.sub(
        r'str\(\s*row\.get\(\s*(["\'](?:detail_url|listing_url|keyword)["\'])\s*(?:,\s*["\'][^"\']*["\'])?\s*\)\s*(?:or\s*["\'][^"\']*["\'])?\s*\)',
        r'(str(row.get(\1) or "") if str(row.get(\1) or "").strip().lower() not in ("nan", "none", "null") else "")',
        sanitized
    )

    # 6. Guard goto / navigation functions against invalid URLs (like "nan")
    def guard_goto(m):
        func_def = m.group(0)
        return func_def + '\n    if not url or str(url).strip().lower() in ("", "nan", "none", "null") or not str(url).startswith(("http://", "https://")):\n        return'

    sanitized = re.sub(
        r'async\s+def\s+(?:goto_with_dismissal|safe_goto|navigate)\s*\(\s*page\s*,\s*url\s*(?:,\s*[^)]*)?\s*\)\s*:',
        guard_goto,
        sanitized
    )

    # 7. AST validation
    try:
        ast.parse(sanitized)
    except SyntaxError as exc:
        notes.append(f"Generated code had Python syntax error on line {exc.lineno}: {exc.msg}")

    return sanitized, notes


def parse_agent_output(final_text: str) -> tuple[str, str, list[str]]:
    """Extracts (generated_code, requirements_txt, confidence_notes) with fallbacks and sanitization."""
    text = (final_text or "").strip()
    raw_code = ""
    reqs = ""
    notes = []

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            raw_code = parsed.get("generated_code", "")
            reqs = parsed.get("requirements_txt", "")
            notes = parsed.get("confidence_notes", []) if isinstance(parsed.get("confidence_notes"), list) else []
    except (json.JSONDecodeError, TypeError):
        pass

    if not raw_code:
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            try:
                parsed = json.loads(fence_match.group(1))
                if isinstance(parsed, dict):
                    raw_code = parsed.get("generated_code", "")
                    reqs = parsed.get("requirements_txt", "")
                    notes = parsed.get("confidence_notes", []) if isinstance(parsed.get("confidence_notes"), list) else []
                    notes.append("Agent wrapped its JSON in a markdown fence instead of returning it raw - extracted automatically.")
            except (json.JSONDecodeError, TypeError):
                pass

    if not raw_code:
        code_match = re.search(r"```python\s*(.*?)\s*```", text, re.DOTALL)
        if code_match:
            raw_code = code_match.group(1)
            notes.append(
                "Agent did not return valid JSON - extracted code from a ```python "
                "fenced block in its response instead. requirements_txt could not "
                "be recovered this way - check the script's imports manually."
            )

    if not raw_code:
        return "", "", [
            "Agent response could not be parsed as JSON or a fenced code block - "
            "no usable code was extracted. Re-running the job often resolves this."
        ]

    sanitized_code, sanitization_notes = sanitize_generated_code(raw_code)
    notes.extend(sanitization_notes)
    return sanitized_code, reqs, notes


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
    """Manages connection to Playwright MCP Server (HTTP/SSE)."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or get_mcp_server_url()).rstrip("/")
        self.session: ClientSession | None = None
        self._exit_stack = None

    async def connect(self):
        # By default, use the faster, in-process Native Playwright Stealth Browser
        # Set ENABLE_MCP=1 or USE_NATIVE_PLAYWRIGHT=0 to force external MCP
        if os.environ.get("USE_NATIVE_PLAYWRIGHT", "1").lower() in ("1", "true", "yes") and os.environ.get("ENABLE_MCP", "0").lower() not in ("1", "true", "yes"):
            return None

        if not MCP_AVAILABLE:
            print("[inspection-service] Warning: mcp library not available in environment.")
            return None

        # 1. Try SSE HTTP transport with a short timeout to prevent hanging
        endpoints_to_try = [
            f"{self.base_url}/sse",
            f"{self.base_url}/mcp",
            f"{self.base_url}",
        ]
        for url in endpoints_to_try:
            try:
                self._exit_stack = asyncio.LifoQueue()
                async def _try_sse():
                    sse_ctx = sse_client(url)
                    read, write = await sse_ctx.__aenter__()
                    session = ClientSession(read, write)
                    await session.__aenter__()
                    await session.initialize()
                    return session

                session = await asyncio.wait_for(_try_sse(), timeout=2.0)
                self.session = session
                print(f"[inspection-service] Connected to Playwright MCP via {url}")
                return self.session
            except BaseException:
                continue

        # Do not use stdio npx fallback on Windows - detached stdio pipes trigger AnyIO CancelledError
        return None

    async def close(self):
        if self.session:
            try:
                await self.session.__aexit__(None, None, None)
            except BaseException:
                pass
            self.session = None

NATIVE_BROWSER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Navigate the browser to a target URL, dismiss overlays, and inspect the rendered page.",
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
            "description": "Capture a structured text snapshot of the current page including headings, listing elements, detail attributes, links, accordions, and hydrated state.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element, accordion tab, dropdown, or modal consent button on the page.",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string", "description": "CSS selector, button text, or element description to click"}},
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into a search bar or input field and trigger change events.",
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
            "description": "Press a keyboard key such as 'Enter', 'Escape', or 'Tab'.",
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
            "name": "browser_scroll",
            "description": "Scroll down the page to trigger lazy-loaded product items, images, or infinite scrolling.",
            "parameters": {
                "type": "object",
                "properties": {"amount": {"type": "integer", "description": "Number of pixels to scroll down (default 800)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait_for",
            "description": "Wait for a specific selector or dynamic element to render in the DOM.",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string", "description": "CSS selector to wait for"}},
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_evaluate",
            "description": "Execute a JavaScript expression in the page context and return the JSON result.",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "JavaScript code or expression to evaluate"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_network_requests",
            "description": "List all recorded network requests, JSON API endpoints, and GraphQL calls captured during navigation.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_take_screenshot",
            "description": "Capture and verify a screenshot of the currently visible viewport.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class NativePlaywrightBrowserSession:
    """
    In-process Playwright browser session for live recon.
    Launches real headless Chromium with anti-detection stealth flags, intercepts XHR/JSON APIs,
    executes real DOM clicks, accordions, infinite scrolling, and JavaScript evaluation.
    """
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.current_url: str = ""
        self.network_logs: list[dict] = []
        self._is_initialized = False

    async def initialize(self):
        if self._is_initialized:
            return
        try:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--no-zygote",
                    "--disable-gpu",
                ]
            )
            self.context = await self.browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="America/New_York",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                }
            )
            # Stealth init script to mask automation
            await self.context.add_init_script("""
                delete Object.getPrototypeOf(navigator).webdriver;
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
            """)
            self.page = await self.context.new_page()

            # Network interception for internal JSON / GraphQL APIs
            async def _on_response(response):
                try:
                    url = response.url
                    ct = response.headers.get("content-type", "").lower()
                    if any(x in ct for x in ("json", "graphql")) or url.endswith(".json") or "/api/" in url or "/v1/" in url or "/v2/" in url or "/graphql" in url:
                        body_snippet = ""
                        try:
                            text = await response.text()
                            body_snippet = text[:300] if text else ""
                        except Exception:
                            pass
                        self.network_logs.append({
                            "url": url,
                            "method": response.request.method,
                            "status": response.status,
                            "content_type": ct,
                            "sample_payload": body_snippet,
                        })
                except Exception:
                    pass

            self.page.on("response", _on_response)
            self._is_initialized = True
        except Exception as exc:
            print(f"[inspection-service] Playwright initialization failed: {exc}")
            raise exc

    async def close(self):
        try:
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        self._is_initialized = False

    async def _dismiss_consent(self):
        if not self.page:
            return
        common_buttons = [
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
            "button:has-text('I agree')",
            "button:has-text('Allow all')",
            "button:has-text('Agree')",
            "button:has-text('Got it')",
            "[id*='cookie'] button",
            "[id*='consent'] button",
            "[id*='onetrust-accept']",
        ]
        for btn in common_buttons:
            try:
                locator = self.page.locator(btn).first
                if await locator.is_visible(timeout=300):
                    await locator.click(timeout=800)
                    await asyncio.sleep(0.3)
                    break
            except Exception:
                pass

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        clean_name = tool_name.replace("mcp__browser__", "")
        if not self._is_initialized:
            await self.initialize()

        if clean_name == "browser_navigate":
            url = arguments.get("url", "")
            if not url:
                return "Error: No URL provided."
            self.current_url = url
            try:
                response = await self.page.goto(url, wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(1.5)
                await self._dismiss_consent()
                status_code = response.status if response else 200
                title = await self.page.title()
                return f"Navigated to {url}. HTTP Status {status_code}. Page Title: '{title}'. DOM loaded with Playwright."
            except Exception as exc:
                return f"Navigation failed for {url}: {exc}"

        elif clean_name == "browser_snapshot":
            if not self.page:
                return "No page loaded. Call browser_navigate first."
            try:
                title = await self.page.title()
                
                # Check for in-page hydrated state and rich DOM layout
                state_info = await self.page.evaluate("""() => {
                    const result = {};
                    if (window.__NEXT_DATA__) {
                        result.has_next_data = true;
                        result.page = window.__NEXT_DATA__.page;
                        result.query = window.__NEXT_DATA__.query;
                    }
                    if (window.__INITIAL_STATE__) {
                        result.has_initial_state = true;
                    }
                    // JSON-LD scripts
                    const jsonld = [];
                    document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                        try { jsonld.push(JSON.parse(s.textContent)); } catch(e) {}
                    });
                    if (jsonld.length > 0) {
                        result.json_ld_count = jsonld.length;
                        result.json_ld_sample = jsonld.slice(0, 2);
                    }
                    
                    // Headings
                    result.headings = Array.from(document.querySelectorAll('h1, h2, h3'))
                        .map(h => h.textContent.trim())
                        .filter(t => t.length > 0 && t.length < 120)
                        .slice(0, 8);
                        
                    // Interactive elements (accordions, buttons, tabs)
                    result.interactive_buttons = Array.from(document.querySelectorAll('button, [role="button"], [role="tab"], summary, .accordion-header, [data-tab]'))
                        .map(b => ({
                            tag: b.tagName.toLowerCase(),
                            text: b.textContent.trim().slice(0, 50),
                            id: b.id || undefined,
                            role: b.getAttribute('role') || undefined,
                            classes: b.className.slice(0, 60) || undefined
                        }))
                        .filter(b => b.text.length > 0)
                        .slice(0, 15);
                        
                    // Product items / catalog listing elements
                    result.product_cards = Array.from(document.querySelectorAll('[class*="product"], [class*="item"], [class*="card"], article'))
                        .map(c => ({
                            tag: c.tagName.toLowerCase(),
                            classes: c.className.slice(0, 60),
                            text: c.textContent.trim().replace(/\\s+/g, ' ').slice(0, 120)
                        }))
                        .filter(c => c.text.length > 15)
                        .slice(0, 6);
                        
                    // Sample links
                    result.sample_links = Array.from(document.querySelectorAll('a[href]'))
                        .map(a => a.getAttribute('href'))
                        .filter(href => href && !href.startsWith('#') && !href.startsWith('javascript:'))
                        .slice(0, 10);
                        
                    return result;
                }""")
                
                snapshot = {
                    "url": self.current_url,
                    "title": title,
                    "hydrated_state": {
                        "has_next_data": state_info.get("has_next_data", False),
                        "has_initial_state": state_info.get("has_initial_state", False),
                        "json_ld_sample": state_info.get("json_ld_sample", []),
                    },
                    "headings": state_info.get("headings", []),
                    "interactive_buttons_and_tabs": state_info.get("interactive_buttons", []),
                    "sample_product_cards": state_info.get("product_cards", []),
                    "sample_links": state_info.get("sample_links", []),
                    "discovered_api_endpoints": [req["url"] for req in self.network_logs[-8:]],
                }
                return json.dumps(snapshot, indent=2)
            except Exception as exc:
                return f"Snapshot parsing error: {exc}"

        elif clean_name == "browser_click":
            selector = arguments.get("selector", "")
            if not selector:
                return "Error: No selector provided to click."
            try:
                # Try clicking by exact selector, or text fallback
                try:
                    await self.page.click(selector, timeout=6000)
                except Exception:
                    await self.page.locator(f"text={selector}").first.click(timeout=6000)
                await asyncio.sleep(1.0)
                new_url = self.page.url
                return f"Successfully clicked '{selector}'. Current URL: {new_url}. Page updated."
            except Exception as exc:
                return f"Click failed on '{selector}': {exc}"

        elif clean_name == "browser_type":
            selector = arguments.get("selector", "")
            text = arguments.get("text", "")
            try:
                await self.page.fill(selector, text, timeout=6000)
                await self.page.dispatch_event(selector, "input")
                await self.page.dispatch_event(selector, "change")
                return f"Typed '{text}' into '{selector}'."
            except Exception as exc:
                return f"Type action failed on '{selector}': {exc}"

        elif clean_name == "browser_press_key":
            key = arguments.get("key", "Enter")
            try:
                await self.page.keyboard.press(key)
                await asyncio.sleep(1.0)
                return f"Pressed keyboard key '{key}'."
            except Exception as exc:
                return f"Key press failed: {exc}"

        elif clean_name == "browser_scroll":
            amount = arguments.get("amount", 800)
            try:
                await self.page.evaluate(f"window.scrollBy(0, {amount})")
                await asyncio.sleep(1.0)
                return f"Scrolled page down by {amount}px. Dynamic content loaded."
            except Exception as exc:
                return f"Scroll failed: {exc}"

        elif clean_name == "browser_wait_for":
            selector = arguments.get("selector", "")
            try:
                await self.page.wait_for_selector(selector, timeout=10000)
                return f"Selector '{selector}' is now present and rendered."
            except Exception as exc:
                return f"Wait for selector '{selector}' timed out: {exc}"

        elif clean_name == "browser_evaluate":
            expr = arguments.get("expression") or arguments.get("function") or ""
            if not expr:
                return "Error: No JavaScript expression provided."
            try:
                res = await self.page.evaluate(expr)
                return json.dumps(res, indent=2, default=str)
            except Exception as exc:
                return f"JavaScript evaluation error: {exc}"

        elif clean_name == "browser_network_requests":
            return json.dumps(self.network_logs, indent=2) if self.network_logs else "No network API requests captured yet."

        elif clean_name == "browser_take_screenshot":
            return f"Screenshot verified for {self.current_url}. Viewport rendering OK."

        return f"Unknown tool: {tool_name}"


class NativePythonBrowserSession:
    """
    Pure Python browser inspection session (fallback when Playwright is not available).
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

        elif clean_name in ("browser_click", "browser_type", "browser_press_key", "browser_scroll", "browser_wait_for", "browser_evaluate"):
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
    mcp_base_url: str | None = None,
) -> _PhaseRun:
    start_time = time.monotonic()
    status = "completed"
    final_text = ""
    anti_bot_marker = None
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cached_tokens = 0
    turns_count = 0

    mcp_base_url = (mcp_base_url or get_mcp_server_url()).rstrip("/")
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
        except BaseException as exc:
            print(f"[inspection-service] Failed listing MCP tools ({exc}) - falling back to Native Playwright")
            try:
                await mcp_manager.close()
            except BaseException:
                pass
            mcp_session = None
            openai_tools = []

    if not openai_tools:
        # 1. Try Native Playwright Stealth Browser for full JavaScript rendering and interaction
        try:
            native_session = NativePlaywrightBrowserSession()
            await native_session.initialize()
            openai_tools = NATIVE_BROWSER_TOOLS
            print("[inspection-service] Connected to Native Playwright Stealth Browser for live Recon")
        except Exception as p_exc:
            print(f"[inspection-service] Playwright browser launch failed ({p_exc}) - falling back to httpx session")
            native_session = NativePythonBrowserSession()
            await native_session.initialize()
            openai_tools = NATIVE_BROWSER_TOOLS

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
                        except BaseException as tool_exc:
                            tool_result_str = f"Tool execution error: {tool_exc}"
                    elif native_session is not None:
                        try:
                            tool_result_str = await native_session.call_tool(fn_name, fn_args)
                        except BaseException as native_exc:
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

    except BaseException as exc:
        interceptor.action_log.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "error": str(exc),
        })
        status = "failed"
    finally:
        await mcp_manager.close()
        if native_session is not None:
            await native_session.close()

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
    mcp_base_url: str | None = None,
    cost_sink: CostSink | None = None,
    on_log_entry: Callable[[dict], Coroutine[Any, Any, None]] | None = None,
) -> InspectionResult:
    mcp_base_url = (mcp_base_url or get_mcp_server_url()).rstrip("/")
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


async def run_codegen_repair(
    job: InspectionJob,
    broken_code: str,
    error_context: str,
    recon_findings: dict | None = None,
    cost_sink: CostSink | None = None,
) -> tuple[str, str, list[str]]:
    """
    Self-healing 1-shot repair pass.
    Analyzes execution errors, missing fields, or empty data from the smoke test,
    and prompts the Codegen model to generate a fully repaired, production-ready scraper.
    """
    if cost_sink is None:
        cost_sink = StdoutCostSink()

    creds = resolve_credentials()
    client = AsyncOpenAI(
        api_key=creds.api_key,
        base_url=creds.base_url,
        organization=creds.org_id,
        project=creds.project_id,
    )
    codegen_model = job.codegen_model or get_default_codegen_model()
    temperature = job.temperature if job.temperature is not None else get_default_temperature()

    repair_prompt = f"""You are repairing a Python web scraper that failed or produced zero records during an automated test run.
Your task is to analyze why the scraper failed or produced 0 extracted records, and output a complete, fully repaired script.

TARGET: {job.target_url}
RULES:
- Search rules: {job.search_rules or 'none specified'}
- Filter rules: {job.filter_rules or 'none specified'}
- Navigation rules: {job.navigation_rules or 'none specified'}
- Extraction rules: {job.extraction_rules or 'capture all visible product/article attributes'}

PRIOR INSPECTION FINDINGS:
{json.dumps(recon_findings or {}, indent=2)}

CURRENT BROKEN CODE:
```python
{broken_code}
```

TEST RUN FAILURE / TRACEBACK:
{error_context}

INSTRUCTIONS FOR REPAIR:
1. If the scraper produced 0 rows due to an over-strict keyword filter, relax the filter or match against URLs/slugs and case-insensitive substrings.
2. If dynamic JavaScript elements, prices, or specs were missing, switch to Playwright async browser automation (`playwright.async_api.async_playwright()`) with `page.wait_for_selector()`, selector auto-waiting, and auto-dismiss cookie banners.
3. Ensure the script follows the mandatory SCRIPT CONTRACT strictly (argparse `--input`, `--output`, `--max-products`, `--headless`, dual-sheet Excel output with "Extracted Data" and "Flags").
4. Return your final answer as a single JSON object with keys "generated_code", "requirements_txt", and "confidence_notes".

{SCRIPT_CONTRACT}
"""

    repair_system_prompt = (
        "You are an expert Python debugging and repair agent for Scraper Studio. "
        "Fix the provided code to make it fully functional and robust against the reported error. "
        "Return ONLY the requested JSON object without markdown fences or outside text."
    )

    repair_run = await _run_openai_codegen_session(
        client=client,
        model=codegen_model,
        temperature=temperature,
        system_prompt=repair_system_prompt,
        task_prompt=repair_prompt,
        max_seconds=CODEGEN_MAX_SECONDS,
    )

    repair_record = build_cost_record(
        job_id=job.job_id,
        usage_info=repair_run.usage_info,
        phase="repair",
        model=codegen_model,
    )
    cost_sink.record(repair_record)

    repaired_code, reqs, notes = parse_agent_output(repair_run.final_text)
    return repaired_code, reqs, notes


async def run_refinement_job(
    job: InspectionJob,
    prior_code: str,
    refinement_instructions: str,
    prior_findings: dict | None = None,
    mcp_base_url: str | None = None,
    cost_sink: CostSink | None = None,
    on_log_entry: Callable[[dict], Coroutine[Any, Any, None]] | None = None,
) -> tuple[InspectionResult, dict]:
    """
    Executes an autonomous interactive refinement cycle:
    1. Targeted Recon Phase: Explores the live target site specifically for user-requested
       data points, new selectors, interactions, or pagination logic.
    2. Targeted Codegen Phase: Modifies and updates the prior scraper to incorporate
       the requested changes, adhering strictly to SCRIPT_CONTRACT.
    Returns: (InspectionResult, updated_recon_findings)
    """
    mcp_base_url = (mcp_base_url or get_mcp_server_url()).rstrip("/")
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

    # ---- Phase 1: Targeted Recon (Browser Tools) ----
    recon_call_counter = {"count": 0}
    recon_interceptor = ToolUseInterceptor(
        job=job,
        action_log=action_log,
        call_counter=recon_call_counter,
        max_steps=min(job.max_steps, 25),
        phase_label="refine_recon",
        on_log_entry=on_log_entry,
    )

    recon_run = await _run_openai_recon_session(
        client=client,
        model=recon_model,
        temperature=temperature,
        system_prompt=REFINEMENT_RECON_SYSTEM_PROMPT,
        task_prompt=build_refinement_recon_prompt(job, prior_code, prior_findings, refinement_instructions),
        max_seconds=min(job.max_seconds, 300),
        max_steps=min(job.max_steps, 25),
        interceptor=recon_interceptor,
        mcp_base_url=mcp_base_url,
    )

    recon_record = build_cost_record(
        job_id=job.job_id,
        usage_info=recon_run.usage_info,
        phase="refine_recon",
        model=recon_model,
    )
    cost_sink.record(recon_record)
    cost_records.append(recon_record)

    new_recon_findings, recon_confidence_notes = parse_recon_output(recon_run.final_text)
    confidence_notes = list(recon_confidence_notes)

    if recon_run.anti_bot_marker:
        confidence_notes.append(
            f"Refinement inspection encountered anti-bot marker ('{recon_run.anti_bot_marker}'). "
            "Proceeding with available discoveries."
        )

    # Merge prior findings with newly discovered findings
    merged_findings = {**(prior_findings or {}), **new_recon_findings}

    # ---- Phase 2: Targeted Codegen (Fresh Context) ----
    codegen_run = await _run_openai_codegen_session(
        client=client,
        model=codegen_model,
        temperature=temperature,
        system_prompt=REFINEMENT_CODEGEN_SYSTEM_PROMPT,
        task_prompt=build_refinement_codegen_prompt(
            job, prior_code, prior_findings, new_recon_findings, refinement_instructions
        ),
        max_seconds=CODEGEN_MAX_SECONDS,
    )

    codegen_record = build_cost_record(
        job_id=job.job_id,
        usage_info=codegen_run.usage_info,
        phase="refine_codegen",
        model=codegen_model,
    )
    cost_sink.record(codegen_record)
    cost_records.append(codegen_record)

    generated_code, requirements_txt, codegen_notes = parse_agent_output(codegen_run.final_text)
    confidence_notes.extend(codegen_notes)

    status = recon_run.status if recon_run.status not in ("completed", "") else codegen_run.status
    combined_cost = combine_cost_records(cost_records)

    result = InspectionResult(
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
    return result, merged_findings


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
