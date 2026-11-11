"""
Unit and Integration tests for Site Analyzer integration.
Tests site_analyzer_client, tech stack evaluation with analyzer signals,
prompt construction, and api_service persistence.
"""

import asyncio
from site_analyzer_client import extract_site_summary, get_analyzer_ssl_verification
from inspection_agent_service import (
    InspectionJob,
    InspectionResult,
    evaluate_tech_stack_feasibility,
    build_recon_prompt,
    build_codegen_prompt,
)
from api_service import JobRecord, JobStore, AuditVaultManager


SAMPLE_SA_RESPONSE_CLOUDFLARE = {
    "target_url": "https://protected-site.com",
    "scraped_at": "2026-11-03T06:20:00Z",
    "result": {
        "accessible": True,
        "working_method": "Playwright Headless",
        "proxy_required": True,
        "blocking_detected": True,
        "blocking_type": "Cloudflare Turnstile",
        "captcha": True,
        "cloudflare": True,
        "anti_bot_provider": "Cloudflare",
        "javascript_required": True,
        "browser_required": True,
        "login_required": False,
        "sitemap": None,
        "automation_feasibility": "Automated scraping possible using stealth browser automation with residential proxies.",
        "recommended_approach": "Use Playwright stealth browser with residential proxies to bypass Turnstile challenge.",
        "confidence": "high",
        "evidence": [
            "Detected cf-ray header and challenge tokens in HTML response",
            "Page renders dynamic React DOM requiring browser engine",
        ],
        "limitations": ["Rate limiting may apply without proxy rotation"],
    },
}

SAMPLE_SA_RESPONSE_STATIC = {
    "target_url": "https://books.toscrape.com",
    "scraped_at": "2026-11-03T06:20:00Z",
    "result": {
        "accessible": True,
        "working_method": "Direct HTTPS",
        "proxy_required": False,
        "blocking_detected": False,
        "blocking_type": "none",
        "captcha": False,
        "cloudflare": False,
        "anti_bot_provider": "None detected",
        "javascript_required": False,
        "browser_required": False,
        "login_required": False,
        "sitemap": "https://books.toscrape.com/sitemap.xml",
        "automation_feasibility": "Full static scraping possible via direct HTTP requests.",
        "recommended_approach": "Direct HTTP requests via requests or httpx with BeautifulSoup.",
        "confidence": "high",
        "evidence": ["HTTP 200 returned static server-rendered HTML markup with all catalog items"],
        "limitations": [],
    },
}


def test_extract_site_summary_complete():
    summary = extract_site_summary(SAMPLE_SA_RESPONSE_CLOUDFLARE)
    assert summary["available"] is True
    assert summary["accessible"] is True
    assert summary["working_method"] == "Playwright Headless"
    assert summary["cloudflare"] is True
    assert summary["browser_required"] is True
    assert summary["javascript_required"] is True
    assert summary["proxy_required"] is True
    assert summary["anti_bot_provider"] == "Cloudflare"
    assert len(summary["evidence"]) == 2
    assert "Playwright" in summary["recommended_approach"]
    print("[PASS] test_extract_site_summary_complete passed")


def test_extract_site_summary_empty_and_error():
    summary_none = extract_site_summary(None)
    assert summary_none["available"] is False
    assert "proceed with live recon" in summary_none["working_method"]

    summary_err = extract_site_summary({"status": "unavailable", "error": "timeout"})
    assert summary_err["available"] is False

    summary_partial = extract_site_summary({"result": {"accessible": False, "cloudflare": True}})
    assert summary_partial["available"] is True
    assert summary_partial["accessible"] is False
    assert summary_partial["cloudflare"] is True
    print("[PASS] test_extract_site_summary_empty_and_error passed")


def test_evaluate_tech_stack_feasibility_with_cloudflare():
    # User requested requests/nodriver, but Site Analyzer detected Cloudflare and browser requirement
    job = InspectionJob(
        job_id="test_cf_job",
        target_url="https://protected-site.com",
        preferred_module="requests",
        target_language="python",
        force_preferred_module=False,
    )
    decision = evaluate_tech_stack_feasibility(
        job=job,
        recon_findings={"navigation_path": "direct"},
        network_logs=[],
        site_analysis=SAMPLE_SA_RESPONSE_CLOUDFLARE,
    )
    # Should fall back from requests to a stealth browser engine (nodriver or playwright) due to Cloudflare / browser required
    assert decision["selected_module"] in ("nodriver", "playwright")
    assert decision["was_fallback"] is True
    assert "Cloudflare" in decision["reason"] or "anti-bot" in decision["reason"].lower()
    print("[PASS] test_evaluate_tech_stack_feasibility_with_cloudflare passed")


def test_evaluate_tech_stack_feasibility_with_static():
    # User selected auto, and Site Analyzer indicates static HTML / direct HTTP
    job = InspectionJob(
        job_id="test_static_job",
        target_url="https://books.toscrape.com",
        preferred_module="auto",
        target_language="python",
    )
    decision = evaluate_tech_stack_feasibility(
        job=job,
        recon_findings={"navigation_path": "direct"},
        network_logs=[],
        site_analysis=SAMPLE_SA_RESPONSE_STATIC,
    )
    # Since direct HTTP is viable and no JS is required:
    assert decision["selected_module"] == "requests"
    assert decision["was_fallback"] is False
    assert "Site Analyzer verified direct HTTP access" in decision["reason"]
    print("[PASS] test_evaluate_tech_stack_feasibility_with_static passed")


def test_evaluate_tech_stack_force_override():
    # User explicitly forces nodriver despite site analyzer signals
    job = InspectionJob(
        job_id="test_force_job",
        target_url="https://books.toscrape.com",
        preferred_module="nodriver",
        target_language="python",
        force_preferred_module=True,
    )
    decision = evaluate_tech_stack_feasibility(
        job=job,
        recon_findings={},
        network_logs=[],
        site_analysis=SAMPLE_SA_RESPONSE_STATIC,
    )
    assert decision["selected_module"] == "nodriver"
    assert decision["was_fallback"] is False
    assert "User explicitly forced engine" in decision["reason"]
    print("[PASS] test_evaluate_tech_stack_force_override passed")


def test_build_recon_prompt_with_site_analysis():
    job = InspectionJob(
        job_id="test_prompt_job",
        target_url="https://protected-site.com",
    )
    prompt = build_recon_prompt(job, site_analysis=SAMPLE_SA_RESPONSE_CLOUDFLARE)
    assert "PRE-FLIGHT SITE ANALYZER INTELLIGENCE" in prompt
    assert "Playwright Headless" in prompt
    assert "Cloudflare" in prompt
    print("[PASS] test_build_recon_prompt_with_site_analysis passed")


def test_build_codegen_prompt_with_site_analysis():
    job = InspectionJob(
        job_id="test_codegen_job",
        target_url="https://protected-site.com",
    )
    tech_decision = {
        "requested_module": "requests",
        "selected_module": "playwright",
        "was_fallback": True,
        "reason": "Cloudflare Turnstile requires browser automation",
        "discovered_apis": [],
        "language": "python",
    }
    prompt = build_codegen_prompt(
        job=job,
        recon_findings={},
        tech_decision=tech_decision,
        site_analysis=SAMPLE_SA_RESPONSE_CLOUDFLARE,
    )
    assert "SITE ANALYZER PRE-FLIGHT GUIDANCE" in prompt
    assert "Use Playwright stealth browser" in prompt
    print("[PASS] test_build_codegen_prompt_with_site_analysis passed")


def test_api_service_persistence(tmp_path=None):
    import tempfile
    from pathlib import Path
    temp_dir = Path(tempfile.mkdtemp())
    record = JobRecord(
        job_id="test_sa_store_job",
        name="Test SA Store Job",
        target_url="https://protected-site.com",
        status="pending",
        created_at="2026-11-03T00:00:00Z",
        site_analysis=SAMPLE_SA_RESPONSE_CLOUDFLARE,
    )
    assert record.site_analysis is not None
    assert record.site_analysis["result"]["cloudflare"] is True

    # Audit vault attempt recording
    vault = AuditVaultManager()
    summary = vault.record_attempt(
        folder=temp_dir,
        attempt_number=1,
        phase="recon",
        agent_decisions={
            "selected_module": "playwright",
            "site_analysis": SAMPLE_SA_RESPONSE_CLOUDFLARE,
        },
    )
    assert summary["attempt"] == 1
    assert summary["agent_decisions"]["site_analysis"]["result"]["cloudflare"] is True
    print("[PASS] test_api_service_persistence passed")


def test_ssl_verification_resolution(monkeypatch=None):
    import os
    import tempfile

    # Case 1: Explicit env verify set to false
    os.environ["SITE_ANALYZER_VERIFY_SSL"] = "false"
    assert get_analyzer_ssl_verification("https://ss-api-1.mobiusservices.co.in/analyze") is False
    assert get_analyzer_ssl_verification("https://example.com/analyze") is False

    # Case 2: Explicit env verify set to true
    os.environ["SITE_ANALYZER_VERIFY_SSL"] = "true"
    assert get_analyzer_ssl_verification("https://ss-api-1.mobiusservices.co.in/analyze") is True

    # Case 3: Env unset, auto-defaults
    del os.environ["SITE_ANALYZER_VERIFY_SSL"]
    # Internal domains default to False
    assert get_analyzer_ssl_verification("https://ss-api-1.mobiusservices.co.in/analyze") is False
    assert get_analyzer_ssl_verification("http://localhost:8000/analyze") is False
    # External domains default to True
    assert get_analyzer_ssl_verification("https://api.external-crawler.com/analyze") is True

    # Case 4: CA Bundle path
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        ca_path = tf.name
    try:
        os.environ["SITE_ANALYZER_CA_BUNDLE"] = ca_path
        assert get_analyzer_ssl_verification("https://ss-api-1.mobiusservices.co.in/analyze") == ca_path
    finally:
        if "SITE_ANALYZER_CA_BUNDLE" in os.environ:
            del os.environ["SITE_ANALYZER_CA_BUNDLE"]
        if os.path.exists(ca_path):
            os.remove(ca_path)

    print("[PASS] test_ssl_verification_resolution passed")


if __name__ == "__main__":
    test_ssl_verification_resolution()
    test_extract_site_summary_complete()
    test_extract_site_summary_empty_and_error()
    test_evaluate_tech_stack_feasibility_with_cloudflare()
    test_evaluate_tech_stack_feasibility_with_static()
    test_evaluate_tech_stack_force_override()
    test_build_recon_prompt_with_site_analysis()
    test_build_codegen_prompt_with_site_analysis()
    test_api_service_persistence()
    print("\nALL 9 SITE ANALYZER INTEGRATION TESTS PASSED SUCCESSFULLY!")
