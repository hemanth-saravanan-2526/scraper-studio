"""
test_codegen_guardrails.py
===========================
Unit tests for Codegen Guardrails and URL Hygiene:
1. URL scheme preservation in prefix-stripping regexes
2. URL early-return guard in clean()
3. pd.read_excel(.astype(object).fillna("")) avoiding Pandas4Warning
4. take_diagnostic_screenshot protocol normalization
5. _build_sample_input_xlsx URL scheme normalization
"""

import ast
import re
import tempfile
import warnings
from pathlib import Path
import pandas as pd
import openpyxl

from inspection_agent_service import sanitize_generated_code
from api_service import _build_sample_input_xlsx, JobRecord


SAMPLE_SCRAPER_WITH_NAIVE_REGEX = '''
import argparse
import logging
import re
import unicodedata
import pandas as pd
import requests

def clean(value):
    if value is None:
        return ""
    text = str(value).replace("\\xa0", " ").strip()
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"^\\s*[^:]{1,40}:\\s*", "", text)
    return text

def take_diagnostic_screenshot(url):
    print(f"Navigating to {url}")

def request_page(session, url, flags):
    try:
        resp = session.get(url, timeout=30)
        return resp
    except requests.RequestException as exc:
        flags.append({"Type": "Error", "URL": url, "Message": str(exc)})
        return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="input.xlsx")
    args = parser.parse_args()
    input_df = pd.read_excel(args.input).fillna("")
'''


def test_prefix_stripping_regex_preserves_urls():
    sanitized, _ = sanitize_generated_code(SAMPLE_SCRAPER_WITH_NAIVE_REGEX)
    
    # 1. Check that AST compiles cleanly
    ast.parse(sanitized)
    
    # 2. Extract and execute the sanitized clean() function
    exec_scope = {"re": re, "unicodedata": __import__("unicodedata"), "pd": pd}
    clean_code_match = re.search(r'(def clean\(value\):[\s\S]+?)(?=\ndef |\Z)', sanitized)
    assert clean_code_match is not None, "clean function not found in sanitized code"
    exec(clean_code_match.group(1), exec_scope)
    clean = exec_scope["clean"]

    # Test URLs are completely preserved (scheme never stripped!)
    test_urls = [
        "https://huggingface.co/models?sort=trending",
        "http://example.com/api/v1/products",
        "https://news.ycombinator.com/",
        "//cdn.example.com/image.png",
    ]
    for url in test_urls:
        res = clean(url)
        assert res == url, f"URL was modified! Expected {url}, got {res}"

    # Test label prefixes are still properly stripped on non-URL text
    assert clean("Title: NVIDIA Nemotron") == "NVIDIA Nemotron"
    assert clean("Nome: CASA DO PADRE") == "CASA DO PADRE"
    assert clean("Price: $1,299.00") == "$1,299.00"
    assert clean("Data: 2026-11-05") == "2026-11-05"
    assert clean("Plain Product Name") == "Plain Product Name"

    print("[PASS] test_prefix_stripping_regex_preserves_urls passed")


def test_pandas4warning_elimination():
    sanitized, _ = sanitize_generated_code(SAMPLE_SCRAPER_WITH_NAIVE_REGEX)
    
    # Verify .astype(object).fillna("") is present instead of raw .fillna("")
    assert '.astype(object).fillna("")' in sanitized
    assert 'pd.read_excel(args.input).fillna("")' not in sanitized

    # Test runtime execution with mixed float/string DataFrame:
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
        temp_excel = tf.name
    try:
        # Create a DataFrame with float and string columns containing NaNs
        df = pd.DataFrame({
            "numeric_id": [1.0, float("nan"), 3.0],
            "price": [19.99, 29.99, float("nan")],
            "title": ["Item A", None, "Item C"],
        })
        df.to_excel(temp_excel, index=False)

        # Catch any Pandas4Warning / FutureWarning
        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter("always")
            read_df = pd.read_excel(temp_excel).astype(object).fillna("")
            assert read_df.iloc[1]["numeric_id"] == ""
            assert read_df.iloc[2]["price"] == ""

            pandas_warnings = [
                w for w in recorded_warnings
                if "Pandas4Warning" in str(w.category.__name__) or "not supported as a fill value" in str(w.message)
            ]
            assert len(pandas_warnings) == 0, f"Encountered unexpected Pandas warnings: {pandas_warnings}"
    finally:
        if Path(temp_excel).exists():
            Path(temp_excel).unlink()

    print("[PASS] test_pandas4warning_elimination passed")


def test_diagnostic_screenshot_protocol_guard():
    sanitized, _ = sanitize_generated_code(SAMPLE_SCRAPER_WITH_NAIVE_REGEX)
    
    # Check that protocol guard was injected into take_diagnostic_screenshot
    assert 'startswith("//")' in sanitized
    assert 'url = "https:" + str(url)' in sanitized
    print("[PASS] test_diagnostic_screenshot_protocol_guard passed")


def test_request_page_logs_exceptions():
    sanitized, _ = sanitize_generated_code(SAMPLE_SCRAPER_WITH_NAIVE_REGEX)
    
    # Check that logging.error is called on RequestException
    assert "logging.error" in sanitized
    assert 'Request failed for' in sanitized
    print("[PASS] test_request_page_logs_exceptions passed")


def test_build_sample_input_xlsx_url_normalization():
    temp_dir = Path(tempfile.mkdtemp())
    try:
        # Case 1: JobRecord with URL missing scheme
        record = JobRecord(
            job_id="test_job_url",
            name="Test URL Job",
            status="pending",
            target_url="huggingface.co/models?sort=trending",
            created_at="2026-11-05T00:00:00",
        )
        xlsx_path = _build_sample_input_xlsx(temp_dir, record)
        assert xlsx_path.exists()
        
        wb = openpyxl.load_workbook(xlsx_path)
        ws = wb.active
        rows = list(ws.rows)
        listing_url = rows[1][1].value
        assert listing_url == "https://huggingface.co/models?sort=trending"

        # Case 2: Protocol-relative URL
        temp_dir_2 = Path(tempfile.mkdtemp())
        record2 = JobRecord(
            job_id="test_job_url_2",
            name="Test URL Job 2",
            status="pending",
            target_url="//news.ycombinator.com",
            created_at="2026-11-05T00:00:00",
        )
        xlsx_path2 = _build_sample_input_xlsx(temp_dir_2, record2)
        wb2 = openpyxl.load_workbook(xlsx_path2)
        rows2 = list(wb2.active.rows)
        assert rows2[1][1].value == "https://news.ycombinator.com"
    finally:
        import shutil
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

    print("[PASS] test_build_sample_input_xlsx_url_normalization passed")


if __name__ == "__main__":
    test_prefix_stripping_regex_preserves_urls()
    test_pandas4warning_elimination()
    test_diagnostic_screenshot_protocol_guard()
    test_request_page_logs_exceptions()
    test_build_sample_input_xlsx_url_normalization()
    print("\nALL 5 CODEGEN GUARDRAIL & URL HYGIENE TESTS PASSED SUCCESSFULLY!")
