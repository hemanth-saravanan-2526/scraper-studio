"""
Scraper Studio - Streamlit Application
======================================
Full-featured, modern web UI for Scraper Studio powered by OpenAI and Python.
Recreates all Builder, Live Agent Detail, Dashboard, Cost Ops, and Settings features.
"""

import asyncio
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# Load .env variables immediately
load_dotenv(override=False)

# Ensure local imports work cleanly
sys.path.insert(0, os.getcwd())

import auth_config
from auth_config import require_credentials_at_startup, resolve_credentials, CredentialError
import cost_metering
from cost_metering import JSONLCostSink, DEFAULT_MAX_BUDGET_USD, OPENAI_MODEL_PRICING
import inspection_agent_service
from inspection_agent_service import (
    InspectionJob,
    InspectionResult,
    run_inspection_job,
    get_default_model,
    get_default_recon_model,
    get_default_codegen_model,
    get_default_temperature,
)
import api_service
from api_service import store, write_generated_files_to_disk, _build_sample_input_xlsx, TEST_RUN_MAX_PRODUCTS, CreateJobRequest, _run_test_run

# -----------------------------------------------------------------------------
# Streamlit Page Config & Custom Styling
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Scraper Studio — Autonomous AI Web Scraping",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for modern clean white & orange theme
st.markdown("""
<style>
    /* Global Styles */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
    
    .stApp {
        background-color: #f8f9fc;
        font-family: 'Inter', sans-serif;
        color: #1e293b;
    }
    
    /* Sidebar Styling */
    section[data-testid="stSidebar"] {
        background-color: #f4f6f9;
        border-right: 1px solid #e2e8f0;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 10px 16px;
        margin-bottom: 8px;
        transition: all 0.2s ease;
        color: #334155;
        font-weight: 500;
        cursor: pointer;
        display: flex;
        align-items: center;
        box-shadow: 0 1px 3px rgba(0,0,0,0.02);
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label > div:first-child {
        display: none !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label div[data-testid="stMarkdownContainer"] p {
        margin: 0;
        font-size: 14px;
        font-weight: 500;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {
        border-color: #ff8a65;
        background: #fffaf5;
        color: #ff5722;
        transform: translateY(-1px);
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"],
    section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) {
        background: linear-gradient(135deg, #ff5722 0%, #ea580c 100%) !important;
        border-color: #ea580c !important;
        color: #ffffff !important;
        font-weight: 600 !important;
        box-shadow: 0 4px 14px rgba(255, 87, 34, 0.35) !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) * {
        color: #ffffff !important;
        font-weight: 600 !important;
    }
    
    /* Header Styling */
    .app-header {
        display: flex;
        align-items: flex-start;
        gap: 16px;
        padding: 22px 26px;
        background: #ffffff;
        border-radius: 14px;
        border: 1px solid #e2e8f0;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.03);
        margin-bottom: 24px;
    }
    .app-title {
        font-size: 26px;
        font-weight: 700;
        color: #0f172a;
        margin: 0;
        letter-spacing: -0.02em;
    }
    .app-subtitle {
        font-size: 13.5px;
        color: #64748b;
        margin-top: 5px;
        line-height: 1.5;
    }
    
    /* Stat Cards */
    .stat-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 18px 20px;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.02);
        transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
    }
    .stat-card:hover {
        border-color: #ff5722;
        transform: translateY(-2px);
        box-shadow: 0 6px 16px rgba(255, 87, 34, 0.12);
    }
    .stat-val {
        font-size: 26px;
        font-weight: 700;
        color: #ff5722;
        font-family: 'JetBrains Mono', monospace;
    }
    .stat-lbl {
        font-size: 11.5px;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: #64748b;
        font-weight: 600;
        margin-top: 4px;
    }
    
    /* Status Badges & Pills */
    .badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 9999px;
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .badge-completed { background: #dcfce7; color: #16a34a; border: 1px solid #bbf7d0; }
    .badge-running { background: #ffedd5; color: #ea580c; border: 1px solid #fed7aa; }
    .badge-failed { background: #fee2e2; color: #dc2626; border: 1px solid #fecaca; }
    .badge-anti_bot { background: #fef3c7; color: #d97706; border: 1px solid #fde68a; }
    
    /* Tag Pills */
    .pill-blue { background: #e0f2fe; color: #0284c7; border: 1px solid #bae6fd; }
    .pill-purple { background: #f3e8ff; color: #9333ea; border: 1px solid #e9d5ff; }
    .pill-green { background: #dcfce7; color: #16a34a; border: 1px solid #bbf7d0; }
    .pill-orange { background: #ffedd5; color: #ea580c; border: 1px solid #fed7aa; }
    
    /* Banner Panels */
    .banner-panel {
        background: linear-gradient(135deg, #fffaf5 0%, #ffffff 100%);
        border: 1px solid #ffedd5;
        border-left: 4px solid #ff5722;
        border-radius: 12px;
        padding: 22px 24px;
        margin-bottom: 24px;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.02);
    }
    
    /* Log Box */
    .log-container {
        background: #0f172a;
        border: 1px solid #cbd5e1;
        border-radius: 8px;
        padding: 14px 16px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
        color: #e2e8f0;
        max-height: 380px;
        overflow-y: auto;
    }
    .log-entry {
        margin-bottom: 6px;
        padding: 4px 0;
        border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    }
    .log-ts { color: #94a3b8; margin-right: 8px; }
    .log-phase { color: #fb923c; font-weight: 600; margin-right: 8px; }
    .log-tool { color: #38bdf8; font-weight: 600; margin-right: 8px; }
    
    /* Custom Buttons */
    div.stButton > button:first-child, div.stDownloadButton > button:first-child {
        background: linear-gradient(135deg, #ff5722 0%, #ea580c 100%) !important;
        color: white !important;
        font-weight: 600 !important;
        border: none !important;
        border-radius: 8px !important;
        padding: 10px 20px !important;
        transition: all 0.2s ease !important;
        box-shadow: 0 2px 8px rgba(255, 87, 34, 0.25) !important;
    }
    div.stButton > button:first-child:hover, div.stDownloadButton > button:first-child:hover {
        background: linear-gradient(135deg, #f4511e 0%, #c2410c 100%) !important;
        box-shadow: 0 4px 14px rgba(255, 87, 34, 0.4) !important;
        transform: translateY(-1px);
    }
    
    /* Metrics */
    div[data-testid="stMetric"] {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 16px 20px;
        box-shadow: 0 2px 6px rgba(0,0,0,0.02);
    }
    div[data-testid="stMetricValue"] > div {
        color: #ff5722 !important;
        font-weight: 700 !important;
        font-size: 24px !important;
        line-height: 1.2 !important;
    }
    div[data-testid="stMetricLabel"] {
        color: #64748b !important;
        font-weight: 600 !important;
        text-transform: uppercase;
        font-size: 11px !important;
        letter-spacing: 0.04em !important;
    }
    
    /* Form Inputs */
    div[data-testid="stForm"] {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 14px;
        padding: 24px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.02);
    }
    
    /* Tabs */
    button[data-baseweb="tab"][aria-selected="true"] {
        color: #ff5722 !important;
        border-bottom-color: #ff5722 !important;
    }
    div[data-baseweb="tab-highlight"] {
        background-color: #ff5722 !important;
    }
</style>
""", unsafe_allow_html=True)

# Ensure jobs store is loaded
store.load_from_disk()

# -----------------------------------------------------------------------------
# Sidebar Navigation
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown("""
    <div style="display:flex; align-items:center; gap:12px; padding:4px 0 16px 0; margin-bottom:12px; border-bottom:1px solid #e2e8f0;">
        <div style="background:linear-gradient(135deg, #ff5722 0%, #ea580c 100%); width:38px; height:38px; border-radius:10px; display:flex; align-items:center; justify-content:center; box-shadow:0 4px 12px rgba(255,87,34,0.3); font-size:20px; color:#ffffff;">⚡</div>
        <div>
            <div style="font-size:17px; font-weight:700; color:#0f172a; letter-spacing:-0.01em;">Scraper Studio</div>
            <div style="font-size:11.5px; color:#64748b; font-weight:500;">Autonomous AI Scraper</div>
        </div>
    </div>
    <div style="font-size:10.5px; font-weight:700; text-transform:uppercase; letter-spacing:0.08em; color:#94a3b8; margin:12px 4px 8px 4px;">Navigation</div>
    """, unsafe_allow_html=True)
    
    nav_selection = st.radio(
        "Navigation",
        ["⚡ Builder", "📊 Dashboard"],
        label_visibility="collapsed",
    )
    
    st.markdown("<div style='height: 24px;'></div>", unsafe_allow_html=True)
    
    st.markdown("""
    <div style="padding:14px; background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; box-shadow:0 1px 3px rgba(0,0,0,0.02); margin-top:16px;">
        <div style="display:flex; align-items:center; gap:8px; font-size:12px; font-weight:600; color:#0f172a;">
            <span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#22c55e; box-shadow:0 0 6px #22c55e;"></span>
            <span>Agent Engine Ready</span>
        </div>
        <div style="font-size:11px; color:#64748b; margin-top:5px; line-height:1.4;">OpenAI Playwright agent ready for live inspection & synthesis.</div>
    </div>
    """, unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# 1. BUILDER (HOME) VIEW
# -----------------------------------------------------------------------------
if nav_selection == "⚡ Builder":
    st.markdown("""
    <div class="app-header">
        <span style="font-size:36px; line-height:1;">⚡</span>
        <div>
            <h1 class="app-title">Scraper Studio</h1>
            <div class="app-subtitle">Describe what to collect in plain English. OpenAI (GPT-5.6 Luna) inspects the live site with Playwright and generates production-ready Python scrapers.</div>
            <div style="display:flex; gap:8px; margin-top:12px; flex-wrap:wrap;">
                <span class="badge pill-blue">REPAIR: GPT-5.6-LUNA</span>
                <span class="badge pill-purple">INSPECT: GPT-4O-SEARCH-PREVIEW</span>
                <span class="badge pill-green">SELF-HEALING</span>
                <span class="badge pill-orange">ZERO-DOWNTIME</span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    with st.form("builder_form"):
        col1, col2 = st.columns([2, 1])
        with col1:
            job_name = st.text_input("Scraper Name", value="Samsung Smartphones Data Collector", placeholder="e.g. Samsung India Smartphone Collector")
            target_url = st.text_input("Target URL (Listing or Detail Page)", value="https://www.samsung.com/in/smartphones/all-smartphones/")
        with col2:
            sample_keyword = st.text_input("Sample Keyword (Optional)", value="Galaxy S24", placeholder="e.g. Galaxy S24")
            uploaded_excel = st.file_uploader("Sample Input Excel (.xlsx)", type=["xlsx", "xls"], help="Upload an Excel file with keyword/listing_url columns to test with.")
        
        st.markdown("### Natural Language Rules")
        tab_search, tab_filter, tab_nav, tab_extract = st.tabs(["🔎 Search Rules", "🎯 Filter Rules", "🧭 Navigation Rules", "📦 Extraction Rules"])
        
        with tab_search:
            search_rules = st.text_area("Search Rules", value="Match products whose title or slug contains the search keyword.", height=90)
        with tab_filter:
            filter_rules = st.text_area("Filter Rules", value="Ignore accessories, cases, and refurbished products.", height=90)
        with tab_nav:
            navigation_rules = st.text_area("Navigation Rules", value="Listing page -> click through to each product detail page to inspect specs.", height=90)
        with tab_extract:
            extraction_rules = st.text_area("Extraction Rules (Fields to capture)", value="Product Title, Model Code, Price (INR), Color options, Storage capacity, Display specs, Camera specs, and Availability status.", height=90)
        
        with st.expander("⚙️ Advanced Settings (Models, Limits, & Budget)"):
            c1, c2, c3 = st.columns(3)
            with c1:
                recon_model = st.selectbox("Phase 1 (Recon) Model", ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-4o", "gpt-4o-mini"], index=0)
                max_steps = st.number_input("Max Inspection Steps", min_value=5, max_value=100, value=40)
            with c2:
                codegen_model = st.selectbox("Phase 2 (Codegen) Model", ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-4o", "gpt-4o-mini"], index=0)
                max_seconds = st.number_input("Max Wall-Clock Time (Seconds)", min_value=30, max_value=1200, value=600)
            with c3:
                temperature = st.slider("Temperature", min_value=0.0, max_value=2.0, value=float(get_default_temperature()), step=0.1)
                max_budget = st.number_input("Max Budget ($USD)", min_value=0.10, max_value=10.0, value=1.50, step=0.25)
        
        submit_btn = st.form_submit_button("🚀 Generate Scraper")

    if submit_btn:
        if not target_url:
            st.error("Please provide a valid Target URL.")
        else:
            job_req = InspectionJob(
                job_id=str(time.time()).replace(".", "-"),
                target_url=target_url,
                sample_keyword=sample_keyword or None,
                search_rules=search_rules,
                filter_rules=filter_rules,
                navigation_rules=navigation_rules,
                extraction_rules=extraction_rules,
                max_steps=max_steps,
                max_seconds=max_seconds,
                max_budget_usd=max_budget,
                recon_model=recon_model,
                codegen_model=codegen_model,
                temperature=temperature,
            )
            
            # Create Job Record
            async def _run_job_flow():
                record = await store.create(api_service.CreateJobRequest(
                    name=job_name,
                    target_url=target_url,
                    sample_keyword=sample_keyword or None,
                    search_rules=search_rules,
                    filter_rules=filter_rules,
                    navigation_rules=navigation_rules,
                    extraction_rules=extraction_rules,
                    max_steps=max_steps,
                    max_seconds=max_seconds,
                    max_budget_usd=max_budget,
                    recon_model=recon_model,
                    codegen_model=codegen_model,
                    temperature=temperature,
                ))
                
                status_placeholder = st.empty()
                log_placeholder = st.empty()
                
                live_logs = []
                async def _on_log(entry):
                    live_logs.append(entry)
                    log_html = "".join(
                        f"<div class='log-entry'><span class='log-ts'>[{e.get('ts','')}]</span> <span class='log-phase'>[{e.get('phase','').upper()}]</span> <span class='log-tool'>{e.get('tool','')}</span> #{e.get('call_number','')}</div>"
                        for e in live_logs[-12:]
                    )
                    log_placeholder.markdown(f"<div class='log-container'>{log_html}</div>", unsafe_allow_html=True)
                
                status_placeholder.info(f"⏳ Running autonomous agent inspection for **{job_name}**...")
                
                job_req.job_id = record.job_id
                res = await run_inspection_job(job_req, on_log_entry=_on_log)
                await store.update_status(record.job_id, res.status, res)
                
                if res.status == "completed":
                    status_placeholder.success(f"✅ Scraper successfully generated! Total spend: **${res.total_cost_usd:.4f}** • Tool calls: **{res.total_tool_calls}**")
                    if res.generated_code:
                        st.markdown("### 📄 Generated Script Preview")
                        st.code(res.generated_code[:1200] + ("\n\n# ... [Open Dashboard tab to view full script] ..." if len(res.generated_code) > 1200 else ""), language="python")
                        st.info("💡 Switch to the **📊 Dashboard** tab in the sidebar to download files, run isolated test runs, or view the complete audit log.")
                else:
                    status_placeholder.warning(f"⚠️ Inspection ended with status: **{res.status}**")
                    if res.confidence_notes:
                        for note in res.confidence_notes:
                            st.warning(note)
                
                st.session_state["selected_job_id"] = record.job_id
            
            # Execute async in streamlit
            with st.spinner("Executing autonomous agent inspection and code synthesis..."):
                asyncio.run(_run_job_flow())


# -----------------------------------------------------------------------------
# 2. DASHBOARD (JOB DETAIL & TRACE) VIEW
# -----------------------------------------------------------------------------
elif nav_selection == "📊 Dashboard":
    st.markdown("""
    <div class="app-header">
        <span style="font-size:36px; line-height:1;">📊</span>
        <div>
            <h1 class="app-title">Scraper Dashboard</h1>
            <div class="app-subtitle">Inspect generated Python code, audit browser actions, analyze token spend breakdown, download assets, or run smoke tests.</div>
            <div style="display:flex; gap:8px; margin-top:12px; flex-wrap:wrap;">
                <span class="badge pill-blue">LIVE TRACE</span>
                <span class="badge pill-orange">COST BREAKDOWN</span>
                <span class="badge pill-green">CODE DELIVERABLES</span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    all_jobs = list(store.jobs.values())
    if not all_jobs:
        st.warning("No jobs found. Create a scraper in the Builder tab first.")
    else:
        job_options = {j.job_id: f"{j.name} ({j.job_id[:8]}) — {j.status.upper()}" for j in reversed(all_jobs)}
        default_index = 0
        if "selected_job_id" in st.session_state and st.session_state["selected_job_id"] in job_options:
            default_index = list(job_options.keys()).index(st.session_state["selected_job_id"])
        
        selected_id = st.selectbox("Select Scraper Job", options=list(job_options.keys()), format_func=lambda x: job_options[x], index=default_index)
        job = store.jobs[selected_id]
        
        # Header Stats & Badges
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        with c1:
            st.markdown(f"### {job.name}")
            st.caption(f"🎯 Target: `{job.target_url}`")
        with c2:
            status_class = f"badge-{job.status}"
            st.markdown(f"<span class='badge {status_class}'>{job.status}</span>", unsafe_allow_html=True)
            st.caption(f"Created: {job.created_at}")
        with c3:
            total_cost = (job.cost or {}).get("total_cost_usd")
            st.metric("Total Spend", f"${total_cost:.4f}" if total_cost is not None else "—")
        with c4:
            tool_calls = (job.result or {}).get("total_tool_calls", 0)
            st.metric("Tool Calls", tool_calls)
        
        # Anti-Bot or Confidence Flags
        notes = (job.result or {}).get("confidence_notes", [])
        if job.anti_bot_marker or notes:
            st.markdown("<div style='background:#fffbeb; border:1px solid #fef3c7; border-left:4px solid #f59e0b; padding:14px 18px; border-radius:8px; margin:16px 0;'>", unsafe_allow_html=True)
            st.markdown("<div style='font-size:12px; font-weight:700; color:#b45309; letter-spacing:0.04em;'>⚠️ OBSERVATIONS & CONFIDENCE FLAGS</div>", unsafe_allow_html=True)
            for n in notes:
                st.markdown(f"- {n}")
            st.markdown("</div>", unsafe_allow_html=True)
        
        # Phase Breakdown Spend
        phases = (job.cost or {}).get("phases", [])
        if phases:
            st.markdown("#### Phase Spend Breakdown")
            p_cols = st.columns(len(phases))
            for i, p in enumerate(phases):
                with p_cols[i]:
                    st.metric(
                        label=f"Phase: {p.get('phase','').upper()}",
                        value=f"${p.get('total_cost_usd',0) or 0:.4f}",
                        delta=f"{p.get('num_turns',0)} turns • {p.get('cache_read_input_tokens',0)} cached tokens",
                    )
        
        # Code & Deliverables Tabs
        st.markdown("#### Generated Deliverables")
        code_tabs = st.tabs(["📄 generated_scraper.py", "📦 requirements.txt", "🔍 Action Log", "📊 Findings JSON"])
        
        res = job.result or {}
        code_str = res.get("generated_code", "")
        reqs_str = res.get("requirements_txt", "")
        action_log = res.get("action_log", [])
        
        with code_tabs[0]:
            if code_str:
                st.code(code_str, language="python", line_numbers=True)
                st.download_button(
                    label="📥 Download generated_scraper.py",
                    data=code_str,
                    file_name=f"{job.job_id}_generated_scraper.py",
                    mime="text/x-python",
                )
            else:
                st.info("No code was generated for this job.")
        
        with code_tabs[1]:
            if reqs_str:
                st.code(reqs_str, language="text")
                st.download_button(
                    label="📥 Download requirements.txt",
                    data=reqs_str,
                    file_name="requirements.txt",
                    mime="text/plain",
                )
            else:
                st.info("No custom requirements specified.")
        
        with code_tabs[2]:
            if action_log:
                log_html = "".join(
                    f"<div class='log-entry'><span class='log-ts'>[{e.get('ts','')}]</span> <span class='log-phase'>[{e.get('phase','').upper()}]</span> <span class='log-tool'>{e.get('tool','')}</span> #{e.get('call_number','')} <span style='color:#94a3b8;'>({json.dumps(e.get('input',{}))})</span></div>"
                    for e in action_log
                )
                st.markdown(f"<div class='log-container'>{log_html}</div>", unsafe_allow_html=True)
            else:
                st.info("No action log recorded.")
        
        with code_tabs[3]:
            st.json(res)
        
        st.divider()
        
        # Test Run Section
        st.markdown("#### 🧪 Test Run Against Sample Site")
        col_test1, col_test2 = st.columns([1, 2])
        with col_test1:
            test_disabled = not bool(code_str)
            if st.button("Run Test Run Script", disabled=test_disabled, help="Runs the scraper in an isolated per-job venv against a 3-product sample."):
                with st.spinner("Creating venv, installing dependencies, and running smoke test..."):
                    asyncio.run(api_service._run_test_run(job.job_id))
                    st.rerun()
        
        with col_test2:
            test_run_info = job.test_run
            if test_run_info:
                st.markdown(f"**Status:** `{test_run_info.get('status','').upper()}`")
                if test_run_info.get("status") == "passed":
                    st.success("Test run passed! Sample data was successfully collected.")
                    if job.saved_to:
                        sample_output_path = Path(job.saved_to) / "sample_output.xlsx"
                        if sample_output_path.exists():
                            with open(sample_output_path, "rb") as f:
                                st.download_button(
                                    label="📊 Download sample_output.xlsx",
                                    data=f.read(),
                                    file_name=f"{job.name}_sample_output.xlsx",
                                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                )
                elif test_run_info.get("status") == "failed":
                    st.error(f"Test run failed: {test_run_info.get('error', 'Check stderr output below')}")
                    if test_run_info.get("stderr_tail"):
                        st.code(test_run_info["stderr_tail"], language="text")
