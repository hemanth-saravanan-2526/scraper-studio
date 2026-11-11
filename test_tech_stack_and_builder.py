"""
test_tech_stack_and_builder.py
==============================
Verification tests for:
1. Multi-module selection & feasibility evaluation (Requests, Playwright, nodriver, Selenium).
2. Backend API collection & endpoint exposure.
3. Generalized prompt propagation.
4. Data model serialization & contract adherence.
"""

import asyncio
import json

from inspection_agent_service import (
    InspectionJob,
    InspectionResult,
    evaluate_tech_stack_feasibility,
    build_recon_prompt,
    build_codegen_prompt,
)
from api_service import CreateJobRequest, JobRecord
from mcp_toolbox.custom_playwright import CustomPlaywrightToolbox


def test_inspection_job_and_request_models():
    """Verify that InspectionJob and CreateJobRequest accept and default new fields cleanly."""
    job = InspectionJob(
        job_id="test-job-1",
        target_url="https://example.com",
        generalized_prompt="Extract top 10 phones and their prices",
        preferred_module="requests",
        target_language="python",
        force_preferred_module=False,
    )
    assert job.generalized_prompt == "Extract top 10 phones and their prices"
    assert job.preferred_module == "requests"
    assert job.target_language == "python"
    assert job.force_preferred_module is False

    req = CreateJobRequest(
        target_url="https://example.com",
        generalized_prompt="Extract real estate listings",
        preferred_module="nodriver",
        target_language="python",
    )
    assert req.preferred_module == "nodriver"
    assert req.generalized_prompt == "Extract real estate listings"

    rec = JobRecord(
        job_id="test-id",
        name="Test Scraper",
        status="completed",
        created_at="2026-11-02T12:00:00",
        target_url="https://example.com",
        generalized_prompt="Unified instructions",
        preferred_module="requests",
        target_language="python",
        tech_stack_decision={"selected_module": "requests", "was_fallback": False},
    )
    dumped = rec.model_dump()
    assert dumped["preferred_module"] == "requests"
    assert dumped["tech_stack_decision"]["selected_module"] == "requests"


def test_feasibility_evaluation_cases():
    """Test feasibility verification across multiple scenarios."""
    base_job = InspectionJob(
        job_id="feas-1",
        target_url="https://shop.example.com",
    )

    # Scenario 1: User requested 'requests' and an internal backend API was found
    base_job.preferred_module = "requests"
    findings_with_api = {
        "network_api": "https://shop.example.com/api/v1/products",
        "page_type": "listing",
        "sample_extracted_fields": {"title": "Sample Item", "price": "$100"},
    }
    net_logs = [{"url": "https://shop.example.com/api/v1/products?cat=phones", "content_type": "application/json"}]
    decision_1 = evaluate_tech_stack_feasibility(base_job, findings_with_api, net_logs)
    assert decision_1["selected_module"] == "requests"
    assert decision_1["was_fallback"] is False
    assert len(decision_1["discovered_apis"]) >= 1

    # Scenario 2: User requested 'requests' on a client-side SPA with 0 APIs
    findings_spa = {
        "network_api": "none observed",
        "navigation_path": "click through dynamic react tabs",
        "sample_extracted_fields": {},
    }
    decision_2 = evaluate_tech_stack_feasibility(base_job, findings_spa, [])
    assert decision_2["selected_module"] == "playwright"
    assert decision_2["was_fallback"] is True
    assert "Switched to 'playwright'" in decision_2["reason"]

    # Scenario 3: User requested 'nodriver'
    base_job.preferred_module = "nodriver"
    decision_3 = evaluate_tech_stack_feasibility(base_job, findings_spa, [])
    assert decision_3["selected_module"] == "nodriver"
    assert decision_3["was_fallback"] is False

    # Scenario 4: User requested 'Auto-Detect' on site with API
    base_job.preferred_module = "auto"
    decision_4 = evaluate_tech_stack_feasibility(base_job, findings_with_api, net_logs)
    assert decision_4["selected_module"] == "requests"
    assert decision_4["was_fallback"] is False

    # Scenario 5: User requested 'Auto-Detect' on bot-protected site
    findings_bot = {"anti_bot_marker": "cloudflare turnstile"}
    decision_5 = evaluate_tech_stack_feasibility(base_job, findings_bot, [])
    assert decision_5["selected_module"] == "nodriver"


def test_prompts_contain_generalized_prompt_and_engine_contract():
    """Verify that recon and codegen prompts incorporate generalized prompt and selected module."""
    job = InspectionJob(
        job_id="p-1",
        target_url="https://books.toscrape.com",
        generalized_prompt="Scrape all fiction books under £20 with high ratings into Excel.",
        preferred_module="requests",
    )
    recon_p = build_recon_prompt(job)
    assert "GENERALIZED USER PROMPT" in recon_p
    assert "Scrape all fiction books under £20" in recon_p
    assert "get_network_endpoints" in recon_p

    tech_decision = {
        "selected_module": "requests",
        "reason": "Discovered REST API",
        "discovered_apis": ["https://books.toscrape.com/api/books.json"],
    }
    codegen_p = build_codegen_prompt(job, {"network_api": "https://books.toscrape.com/api/books.json"}, tech_decision)
    assert "MANDATORY TECH STACK: `requests`" in codegen_p
    assert "Scrape all fiction books under £20" in codegen_p
    assert "requests\nbeautifulsoup4\npandas\nopenpyxl" in codegen_p


async def test_mcp_custom_playwright_network_endpoints_tool():
    """Verify that get_network_endpoints returns structured JSON with captured endpoints."""
    tb = CustomPlaywrightToolbox()
    tb._is_initialized = True
    tb.network_logs = [
        {"url": "https://api.example.com/v1/search?q=test", "content_type": "application/json", "status": 200},
        {"url": "https://static.example.com/logo.png", "content_type": "image/png", "status": 200},
        {"url": "https://api.example.com/graphql", "content_type": "application/graphql", "status": 200},
    ]
    res_str = await tb.get_network_endpoints(filter_type="api_only")
    res = json.loads(res_str)
    assert res["status"] == "success"
    assert res["total_captured"] == 2
    assert any("graphql" in e["url"] for e in res["endpoints"])
    assert any("search" in e["url"] for e in res["endpoints"])

    dispatched_str = await tb.call_tool("get_network_endpoints", {"filter_type": "all"})
    dispatched = json.loads(dispatched_str)
    assert dispatched["total_captured"] == 3


if __name__ == "__main__":
    test_inspection_job_and_request_models()
    test_feasibility_evaluation_cases()
    test_prompts_contain_generalized_prompt_and_engine_contract()
    asyncio.run(test_mcp_custom_playwright_network_endpoints_tool())
    print("[PASS] All unit tests in test_tech_stack_and_builder PASSED successfully!")
