"""
site_analyzer_client.py
=======================
Asynchronous client for the external Site Analyzer API:
https://ss-api-1.mobiusservices.co.in/analyze

Performs pre-flight website access and anti-bot probing before autonomous
reconnaissance, determining:
- Accessibility (HTTP status)
- Direct HTTP vs Browser Automation requirements
- Cloudflare, CAPTCHA, and WAF defense markers
- JavaScript rendering requirements
- Automation feasibility and recommended approach

Designed with robust timeouts and graceful degradation: if the external analyzer
service is unreachable or times out, it returns a safe diagnostic fallback dictionary
without failing the scraper synthesis pipeline.
"""

import asyncio
import json
import os
import ssl
import urllib.parse
import urllib.request
import warnings
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

# Suppress unverified HTTPS warnings when connecting to internal endpoints
warnings.filterwarnings("ignore", message=".*Unverified HTTPS request.*")
warnings.filterwarnings("ignore", message=".*InsecureRequestWarning.*")

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    httpx = None
    _HAS_HTTPX = False

DEFAULT_ANALYZER_URL = os.getenv(
    "SITE_ANALYZER_URL", "https://ss-api-1.mobiusservices.co.in/analyze"
)
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("SITE_ANALYZER_TIMEOUT", "35.0"))


def get_analyzer_ssl_verification(url: str | None = None) -> bool | str:
    """
    Resolves SSL verification configuration for the Site Analyzer service:
    - If SITE_ANALYZER_CA_BUNDLE is set and points to an existing file, returns that path.
    - If SITE_ANALYZER_VERIFY_SSL is set ('false'/'0'/'no' -> False, 'true'/'1'/'yes' -> True).
    - Defaults to False for internal mobiusservices and localhost domains to handle missing
      intermediate CA chains on private corporate endpoints.
    - Defaults to True for any other public external endpoints.
    """
    ca_bundle = os.getenv("SITE_ANALYZER_CA_BUNDLE")
    if ca_bundle and os.path.isfile(ca_bundle):
        return ca_bundle

    env_verify = os.getenv("SITE_ANALYZER_VERIFY_SSL")
    if env_verify is not None:
        return env_verify.strip().lower() in ("true", "1", "yes")

    target_url = (url or os.getenv("SITE_ANALYZER_URL", DEFAULT_ANALYZER_URL)).lower()
    if "mobiusservices." in target_url or "localhost" in target_url or "127.0.0.1" in target_url:
        return False

    return True


async def analyze_target_site(
    url: str,
    max_urls: int = 3,
    max_depth: int = 1,
    proxy: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    verify_ssl: bool | str | None = None,
) -> dict[str, Any]:
    """
    Calls the external Site Analyzer service to gather pre-flight access and WAF intelligence.

    Args:
        url: The website URL to analyze.
        max_urls: Number of sample URLs to probe (keep small for fast pre-flight ~10s).
        max_depth: Crawl traversal depth for probing.
        proxy: Optional proxy string.
        timeout_seconds: Hard cutoff for the HTTP request.
        verify_ssl: Explicit SSL verification override (True/False/CA path).

    Returns:
        Structured dictionary matching the analyzer schema, or a safe error fallback.
    """
    clean_url = (url or "").strip()
    if not clean_url:
        return {
            "status": "error",
            "error": "No URL provided for site analysis",
            "result": {},
        }

    params: dict[str, Any] = {
        "url": clean_url,
        "max_urls": max_urls,
        "max_depth": max_depth,
    }
    if proxy:
        params["proxy"] = proxy

    headers = {
        "User-Agent": "ScraperStudio-Engine/2.5 (+https://scraper-studio.internal)",
        "Accept": "application/json",
    }

    target_analyzer_url = os.getenv("SITE_ANALYZER_URL", DEFAULT_ANALYZER_URL)
    effective_verify = verify_ssl if verify_ssl is not None else get_analyzer_ssl_verification(target_analyzer_url)

    try:
        if _HAS_HTTPX:
            async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True, verify=effective_verify) as client:
                resp = await client.get(target_analyzer_url, params=params, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if "result" in data:
                        return data
                    return {"status": "success", "url": clean_url, "result": data}
                else:
                    return {
                        "status": "http_error",
                        "http_status": resp.status_code,
                        "error": f"Site analyzer returned HTTP {resp.status_code}: {resp.text[:300]}",
                        "result": {},
                    }
        else:
            # Fallback to standard library urllib via asyncio.to_thread
            def _urllib_fetch():
                query = urllib.parse.urlencode(params)
                target_url = f"{target_analyzer_url}?{query}"
                parsed_scheme = urllib.parse.urlparse(target_url).scheme.lower()
                if parsed_scheme not in ("http", "https"):
                    raise ValueError(f"Unsupported URL scheme: {parsed_scheme}")
                req = urllib.request.Request(target_url, headers=headers)
                if effective_verify is False:
                    ssl_ctx = ssl._create_unverified_context()
                elif isinstance(effective_verify, str):
                    ssl_ctx = ssl.create_default_context(cafile=effective_verify)
                else:
                    ssl_ctx = ssl.create_default_context()
                with urllib.request.urlopen(req, timeout=timeout_seconds, context=ssl_ctx) as resp:  # nosec B310
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw)

            data = await asyncio.to_thread(_urllib_fetch)
            if "result" in data:
                return data
            return {"status": "success", "url": clean_url, "result": data}
    except TimeoutError:
        return {
            "status": "timeout",
            "error": f"Site analyzer timed out after {timeout_seconds}s for {clean_url}. Proceeding with standard MCP recon.",
            "result": {},
        }
    except Exception as exc:
        is_timeout = "timeout" in str(exc).lower() or (
            _HAS_HTTPX and isinstance(exc, getattr(httpx, "TimeoutException", type(None)))
        )
        if is_timeout:
            return {
                "status": "timeout",
                "error": f"Site analyzer timed out after {timeout_seconds}s for {clean_url}. Proceeding with standard MCP recon.",
                "result": {},
            }
        return {
            "status": "unavailable",
            "error": f"Site analyzer connection failed: {exc}. Proceeding with standard MCP recon.",
            "result": {},
        }


def extract_site_summary(analysis: dict[str, Any] | None) -> dict[str, Any]:
    """
    Extracts key decision-making signals from the site analyzer report into a compact dict.
    """
    if not analysis or analysis.get("status") in ("error", "unavailable", "timeout", "http_error"):
        return {
            "available": False,
            "error": (analysis or {}).get("error", "Not performed"),
            "accessible": True,
            "browser_required": False,
            "javascript_required": False,
            "cloudflare": False,
            "blocking_detected": False,
            "working_method": "Unknown (proceed with live recon)",
            "recommended_approach": "",
        }

    res = analysis.get("result", {})
    return {
        "available": True,
        "accessible": res.get("accessible", True),
        "working_method": res.get("working_method", "Direct HTTPS requests"),
        "proxy_required": res.get("proxy_required", False),
        "blocking_detected": res.get("blocking_detected", False),
        "blocking_type": res.get("blocking_type", []),
        "captcha": res.get("captcha", False),
        "cloudflare": res.get("cloudflare", False),
        "anti_bot_provider": res.get("anti_bot_provider", "None detected"),
        "javascript_required": res.get("javascript_required", False),
        "browser_required": res.get("browser_required", False),
        "login_required": res.get("login_required", False),
        "automation_feasibility": res.get("automation_feasibility", ""),
        "recommended_approach": res.get("recommended_approach", ""),
        "evidence": res.get("evidence", []),
    }
