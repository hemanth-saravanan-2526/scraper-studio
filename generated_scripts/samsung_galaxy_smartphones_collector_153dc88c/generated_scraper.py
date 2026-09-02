import argparse
import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

DEFAULT_LISTING_URL = "https://www.samsung.com/in/smartphones/all-smartphones/"


def log(message):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{timestamp}] {message}", flush=True)


def clean(value):
    if value is None or pd.isna(value) or str(value).strip().lower() in ("", "nan", "none", "null"):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def value_from_row(row, column):
    return clean(row[column]) if column in row else ""


def get_proxy():
    return None


def solve_captcha(*args, **kwargs):
    raise NotImplementedError("CAPTCHA solving is intentionally not implemented")


def detect_anti_bot_signals(response):
    signals = []
    try:
        status = response.status
        url = response.url
    except Exception:
        return ["Unable to inspect an HTTP response"]

    if status == 403:
        signals.append("HTTP 403 Forbidden")
    elif status == 429:
        signals.append("HTTP 429 Too Many Requests")

    body = ""
    try:
        body = response.body().decode("utf-8", errors="ignore")
    except Exception:
        body = ""

    combined = f"{url}\n{body}".lower()
    markers = {
        "captcha": "CAPTCHA marker",
        "cloudflare": "Cloudflare marker",
        "perimeterx": "PerimeterX marker",
        "datadome": "DataDome marker",
    }
    for marker, description in markers.items():
        if marker in combined:
            signals.append(description)

    try:
        content_type = response.headers.get("content-type", "").lower()
    except Exception:
        content_type = ""
    if status == 200 and ("text/html" in content_type or not content_type) and body and len(body) < 500:
        signals.append("Unusually short HTML response body")
    return signals


def dismiss_cookie_banner(page):
    consent_texts = ["Accept all", "Accept", "I agree", "Allow all", "Agree"]
    attempted = False
    for text in consent_texts:
        try:
            locator = page.get_by_text(text, exact=True).first
            if locator.count() > 0:
                attempted = True
                locator.click(timeout=700)
                log(f"Cookie consent dismissal clicked: {text}")
                return True
        except Exception:
            continue
    log("Cookie consent dismissal attempted, but no matching consent button was found")
    return False


def add_flag(flags, url, message, severity="warning"):
    flags.append({
        "Timestamp": datetime.now(timezone.utc).isoformat(),
        "URL": clean(url),
        "Severity": severity,
        "Flag": message,
    })


def text_of(soup):
    return soup.get_text("\n", strip=True).replace("\xa0", " ")


def first_nonempty(values):
    for value in values:
        value = clean(value)
        if value:
            return value
    return ""


def jsonld_objects(soup):
    objects = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            parsed = json.loads(node.string or node.get_text())
            if isinstance(parsed, list):
                objects.extend(parsed)
            else:
                objects.append(parsed)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return objects


def product_jsonld(soup):
    for obj in jsonld_objects(soup):
        if isinstance(obj, dict):
            if obj.get("@type") == "Product" or "offers" in obj or "sku" in obj:
                return obj
            graph = obj.get("@graph")
            if isinstance(graph, list):
                for child in graph:
                    if isinstance(child, dict) and (child.get("@type") == "Product" or "offers" in child):
                        return child
    return {}


def extract_near_label(text, labels):
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(rf"(?:{label_pattern})\s*[:\-]?\s*([^\n]{{1,240}})", re.IGNORECASE)
    match = pattern.search(text)
    return clean(match.group(1)) if match else ""


def extract_detail(html, url):
    soup = BeautifulSoup(html, "html.parser")
    text = text_of(soup)
    data = product_jsonld(soup)
    offers = data.get("offers", {}) if isinstance(data, dict) else {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}

    title = first_nonempty([
        data.get("name") if isinstance(data, dict) else "",
        soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "",
        soup.title.get_text(" ", strip=True) if soup.title else "",
    ])
    price = first_nonempty([
        offers.get("price"),
        extract_near_label(text, ["Price", "MRP", "₹", "INR"]),
    ])
    if price:
        price = re.sub(r"\s+", " ", str(price)).strip()
        if re.fullmatch(r"[\d,]+(?:\.\d+)?", price):
            price = f"INR {price}"

    model_code = first_nonempty([
        data.get("sku") if isinstance(data, dict) else "",
        extract_near_label(text, ["Model Code", "Model number", "Model Number", "SKU"]),
    ])
    colors = first_nonempty([
        data.get("color") if isinstance(data, dict) else "",
        extract_near_label(text, ["Available Colors", "Colors", "Colour", "Color"]),
    ])
    storage = extract_near_label(text, ["Storage Variants", "Storage", "Internal Storage", "Memory"])
    display = extract_near_label(text, ["Display Specs", "Display", "Screen size", "Screen"])
    camera = extract_near_label(text, ["Camera Specs", "Camera", "Rear Camera", "Front Camera"])
    battery = extract_near_label(text, ["Battery Capacity", "Battery", "Battery capacity"])

    return {
        "Product Name": title,
        "Model Code": model_code,
        "Price in INR": price,
        "Available Colors": colors,
        "Storage Variants": storage,
        "Display Specs": display,
        "Camera Specs": camera,
        "Battery Capacity": battery,
        "Source URL": url,
    }


def listing_links(html, listing_url, keyword):
    soup = BeautifulSoup(html, "html.parser")
    keyword_lower = clean(keyword).lower()
    candidates = []
    for obj in jsonld_objects(soup):
        if not isinstance(obj, dict) or obj.get("@type") != "ItemList":
            continue
        for item in obj.get("itemListElement", []):
            if not isinstance(item, dict):
                continue
            item_url = item.get("url")
            item_data = item.get("item") if isinstance(item.get("item"), dict) else {}
            item_url = item_url or item_data.get("url")
            item_name = item.get("name") or item_data.get("name") or ""
            if item_url and (not keyword_lower or keyword_lower in clean(item_name).lower()):
                candidates.append((urljoin(listing_url, item_url), item_name))

    for anchor in soup.find_all("a", href=True):
        href = urljoin(listing_url, anchor["href"])
        name = anchor.get_text(" ", strip=True)
        if not name:
            continue
        if keyword_lower and keyword_lower not in name.lower():
            continue
        if any(token in href.lower() for token in ["smartphone", "galaxy", "/in/"]):
            candidates.append((href, name))

    result = []
    seen = set()
    for href, name in candidates:
        if href not in seen:
            seen.add(href)
            result.append((href, name))
    return result


def main():
    parser = argparse.ArgumentParser(description="Samsung India smartphone scraper")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    args = parser.parse_args()

    log("Reading input workbook")
    input_df = pd.read_excel(args.input).fillna("")
    extracted = []
    flags = []
    proxy = get_proxy()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, proxy=proxy)
        context = browser.new_context()
        page = context.new_page()

        def on_response(response):
            for signal in detect_anti_bot_signals(response):
                add_flag(flags, response.url, signal)

        page.on("response", on_response)

        for row_number, row in input_df.iterrows():
            keyword = value_from_row(row, "keyword")
            listing_url = value_from_row(row, "listing_url")
            detail_url = value_from_row(row, "detail_url")
            if detail_url:
                targets = [(detail_url, "direct detail URL")]
            else:
                listing_url = listing_url or DEFAULT_LISTING_URL
                log(f"Row {row_number + 1}: loading listing {listing_url}")
                try:
                    page.goto(listing_url, wait_until="domcontentloaded", timeout=45000)
                    dismissed = dismiss_cookie_banner(page)
                    if not dismissed:
                        add_flag(flags, listing_url, "Possible cookie-banner layout change; no consent button matched")
                    page.wait_for_timeout(1500)
                    targets = listing_links(page.content(), listing_url, keyword)
                    if not targets:
                        add_flag(flags, listing_url, "No high-confidence product links found in listing HTML", "low-confidence")
                except Exception as exc:
                    add_flag(flags, listing_url, f"Listing error: {exc}", "error")
                    targets = []

            for target_url, target_name in targets:
                if args.max_products is not None and len(extracted) >= args.max_products:
                    break
                log(f"Extracting product {len(extracted) + 1}: {target_url}")
                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                    dismissed = dismiss_cookie_banner(page)
                    if not dismissed:
                        add_flag(flags, target_url, "Possible cookie-banner layout change; no consent button matched")
                    page.wait_for_timeout(1200)
                    item = extract_detail(page.content(), target_url)
                    if keyword and keyword.lower() not in item["Product Name"].lower() and not detail_url:
                        continue
                    missing = [key for key in ["Product Name", "Model Code", "Price in INR", "Available Colors", "Storage Variants", "Display Specs", "Camera Specs", "Battery Capacity"] if not item.get(key)]
                    if missing:
                        add_flag(flags, target_url, "Low-confidence or missing fields: " + ", ".join(missing), "low-confidence")
                    extracted.append(item)
                except Exception as exc:
                    add_flag(flags, target_url, f"Detail extraction error: {exc}", "error")
            if args.max_products is not None and len(extracted) >= args.max_products:
                break
        browser.close()

    log(f"Writing {len(extracted)} extracted rows and {len(flags)} flags")
    data_columns = ["Product Name", "Model Code", "Price in INR", "Available Colors", "Storage Variants", "Display Specs", "Camera Specs", "Battery Capacity", "Source URL"]
    flag_columns = ["Timestamp", "URL", "Severity", "Flag"]
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        pd.DataFrame(extracted, columns=data_columns).to_excel(writer, sheet_name="Extracted Data", index=False)
        pd.DataFrame(flags, columns=flag_columns).to_excel(writer, sheet_name="Flags", index=False)
    log("Scrape complete")


if __name__ == "__main__":
    main()