import argparse
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup


TARGET = "https://www.amazon.com/"
USER_AGENT = "Mozilla/5.0 (compatible; ScraperStudio/1.0)"
DATA_COLUMNS = [
    "Product Title",
    "Model Code",
    "Price (INR)",
    "Color options",
    "Storage capacity",
    "Display specs",
    "Camera specs",
    "Availability status",
    "Source URL",
]
FLAG_COLUMNS = ["timestamp", "url", "kind", "message"]


def log(message):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def get_proxy():
    """Placeholder for a caller-provided proxy integration."""
    return None


def solve_captcha(*args, **kwargs):
    """CAPTCHA solving and anti-bot bypass are intentionally unsupported."""
    raise NotImplementedError("CAPTCHA solving is not implemented")


def detect_anti_bot_signals(response):
    """Return observed anti-bot indicators without attempting to bypass them."""
    signals = []
    body = response.text or ""
    lower = body.lower()
    if response.status_code == 403:
        signals.append("HTTP 403 Forbidden")
    if response.status_code == 429:
        signals.append("HTTP 429 Too Many Requests")
    markers = {
        "captcha": "CAPTCHA marker",
        "cloudflare": "Cloudflare marker",
        "perimeterx": "PerimeterX marker",
        "datadome": "DataDome marker",
    }
    for marker, description in markers.items():
        if marker in lower:
            signals.append(description)
    if len(body.strip()) < 3000:
        signals.append(f"Unusually short response body ({len(body.strip())} bytes)")
    return signals


def add_flag(flags, url, kind, message):
    flags.append({
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": url or "",
        "kind": kind,
        "message": message,
    })


def fetch(session, url, flags):
    log(f"Fetching {url}")
    try:
        response = session.get(url, timeout=30, allow_redirects=True)
    except requests.RequestException as exc:
        add_flag(flags, url, "error", f"HTTP request failed: {exc}")
        return None
    for signal in detect_anti_bot_signals(response):
        add_flag(flags, response.url or url, "anti-bot", signal)
    if response.status_code >= 400:
        add_flag(flags, response.url or url, "error", f"HTTP status {response.status_code}")
        return None
    return response


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def text_after_label(text, labels):
    compact = clean_text(text)
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{label_pattern})\s*[:\-]?\s*(.{{1,220}})", compact, re.I)
    return clean_text(match.group(1)) if match else ""


def extract_detail(response, source_url, flags):
    soup = BeautifulSoup(response.text, "html.parser")
    page_text = clean_text(soup.get_text(" ", strip=True))
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = clean_text(h1.get_text(" ", strip=True))
    if not title and soup.title:
        title = clean_text(soup.title.get_text(" ", strip=True))
        title = re.sub(r"\s*[:\-|].*Amazon.*$", "", title, flags=re.I).strip()

    price = ""
    price_match = re.search(r"(?:₹|INR|Rs\.?)[\s]*[\d,]+(?:\.\d{1,2})?", page_text, re.I)
    if price_match:
        price = price_match.group(0)

    model = text_after_label(page_text, ["Model", "Model Number", "Model Name", "Item model number"])
    color = text_after_label(page_text, ["Color", "Colour", "Color options"])
    storage = text_after_label(page_text, ["Storage", "Storage capacity", "Hard Disk Size", "Memory Storage Capacity"])
    display = text_after_label(page_text, ["Display", "Display size", "Screen Size", "Display Technology"])
    camera = text_after_label(page_text, ["Camera", "Camera specs", "Rear Camera", "Front Camera"])

    availability = ""
    availability_match = re.search(
        r"(In Stock|Currently unavailable|Temporarily out of stock|Available|Out of stock|Usually ships[^.]{0,100})",
        page_text,
        re.I,
    )
    if availability_match:
        availability = clean_text(availability_match.group(1))

    if not title:
        add_flag(flags, source_url, "low-confidence", "No product title found using generic document selectors")
    if not any([model, price, color, storage, display, camera, availability]):
        add_flag(flags, source_url, "low-confidence", "No requested product fields were confidently found")

    return {
        "Product Title": title,
        "Model Code": model,
        "Price (INR)": price,
        "Color options": color,
        "Storage capacity": storage,
        "Display specs": display,
        "Camera specs": camera,
        "Availability status": availability,
        "Source URL": source_url,
    }


def product_links(response, base_url):
    soup = BeautifulSoup(response.text, "html.parser")
    links = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"])
        parsed = urlparse(href)
        path = parsed.path.lower()
        if parsed.netloc.lower().endswith("amazon.com") and ("/dp/" in path or "/gp/product/" in path):
            normalized = href.split("?")[0]
            if normalized not in seen:
                seen.add(normalized)
                links.append((normalized, clean_text(anchor.get_text(" ", strip=True))))
    return links


def keyword_matches(url, anchor_text, keyword):
    needle = (keyword or "").casefold().strip()
    if not needle:
        return True
    haystack = f"{url} {anchor_text}".casefold()
    return needle in haystack


def excluded_product(url, anchor_text):
    haystack = f"{url} {anchor_text}".casefold()
    return any(term in haystack for term in ["accessory", "accessories", "case", "refurbished"])


def process_row(row, session, flags, data, max_products, count):
    keyword = clean_text(row.get("keyword", ""))
    listing_url = clean_text(row.get("listing_url", ""))
    detail_url = clean_text(row.get("detail_url", ""))

    # This is deliberately independent of listing discovery.
    if detail_url:
        if max_products is not None and count[0] >= max_products:
            return
        response = fetch(session, detail_url, flags)
        if response is not None:
            log(f"Extracting direct detail page {detail_url}")
            data.append(extract_detail(response, detail_url, flags))
            count[0] += 1
        return

    if keyword and not listing_url:
        listing_url = TARGET.rstrip("/") + "/s?k=" + quote_plus(keyword)
        log(f"Constructed Amazon search URL for keyword {keyword!r}")
    if not listing_url:
        add_flag(flags, "", "error", "Input row has no keyword, listing_url, or detail_url")
        return

    response = fetch(session, listing_url, flags)
    if response is None:
        return
    links = product_links(response, response.url or listing_url)
    if not links:
        add_flag(flags, listing_url, "low-confidence", "No product detail links found with generic /dp/ or /gp/product/ patterns")
    for detail, anchor_text in links:
        if max_products is not None and count[0] >= max_products:
            break
        if keyword and not keyword_matches(detail, anchor_text, keyword):
            continue
        if excluded_product(detail, anchor_text):
            continue
        detail_response = fetch(session, detail, flags)
        if detail_response is None:
            continue
        log(f"Extracting listing product detail {detail}")
        data.append(extract_detail(detail_response, detail, flags))
        count[0] += 1


def main():
    parser = argparse.ArgumentParser(description="Static Amazon product scraper")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    args = parser.parse_args()

    log("Starting scraper; using requests + BeautifulSoup only (no browser rendering)")
    try:
        input_df = pd.read_excel(args.input)
    except Exception as exc:
        log(f"Could not read input workbook: {exc}")
        raise

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    flags = []
    data = []
    count = [0]
    for index, row in input_df.iterrows():
        log(f"Processing input row {index + 1}")
        process_row(row.to_dict(), session, flags, data, args.max_products, count)
        if args.max_products is not None and count[0] >= args.max_products:
            log("Reached --max-products cap")
            break

    log(f"Writing {len(data)} extracted rows and {len(flags)} flag rows")
    output_data = pd.DataFrame(data, columns=DATA_COLUMNS)
    output_flags = pd.DataFrame(flags, columns=FLAG_COLUMNS)
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        output_data.to_excel(writer, sheet_name="Extracted Data", index=False)
        output_flags.to_excel(writer, sheet_name="Flags", index=False)
    log(f"Finished; output saved to {args.output}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"Fatal error: {exc}")
        sys.exit(1)
