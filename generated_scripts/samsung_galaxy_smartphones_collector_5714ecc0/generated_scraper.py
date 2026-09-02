import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://www.samsung.com/in/smartphones/all-smartphones/"
USER_AGENT = "Mozilla/5.0 (compatible; ScraperStudio/1.0)"


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
    raise NotImplementedError("CAPTCHA solving and anti-bot bypass are not implemented")


def detect_anti_bot_signals(response):
    signals = []
    body = response.text or ""
    lowered = body.lower()
    if response.status_code in {403, 429}:
        signals.append(f"HTTP status {response.status_code}")
    markers = {
        "captcha": "CAPTCHA marker",
        "cloudflare": "Cloudflare marker",
        "perimeterx": "PerimeterX marker",
        "datadome": "DataDome marker",
    }
    for marker, description in markers.items():
        if marker in lowered:
            signals.append(description)
    if len(body.strip()) < 500:
        signals.append("Unusually short HTTP response body")
    return signals


def request_page(session, url, flags):
    log(f"Requesting {url}")
    try:
        response = session.get(url, timeout=30)
        for signal in detect_anti_bot_signals(response):
            flags.append({
                "Signal": signal,
                "URL": url,
                "Message": "Anti-bot signal detected; no bypass attempted.",
            })
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        flags.append({"Signal": "Request error", "URL": url, "Message": str(exc)})
        log(f"Request failed for {url}: {exc}")
        return None


def jsonld_objects(soup):
    objects = []
    for node in soup.find_all("script", type="application/ld+json"):
        raw = node.string or node.get_text("\n", strip=True)
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, list):
            objects.extend(parsed)
        else:
            objects.append(parsed)
    return objects


def listing_links(soup, base_url):
    links = []
    for obj in jsonld_objects(soup):
        if not isinstance(obj, dict):
            continue
        if obj.get("@type") == "ItemList" or "itemListElement" in obj:
            for entry in obj.get("itemListElement", []):
                item = entry.get("item", entry) if isinstance(entry, dict) else entry
                href = ""
                if isinstance(item, str):
                    href = item
                elif isinstance(item, dict):
                    href = item.get("url") or item.get("@id") or ""
                if href:
                    links.append(urljoin(base_url, href))
    unique = []
    seen = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            unique.append(link)
    return unique


def recursive_products(value):
    found = []
    if isinstance(value, dict):
        kind = value.get("@type", "")
        types = kind if isinstance(kind, list) else [kind]
        if any(str(item) in {"Product", "ProductGroup"} for item in types):
            found.append(value)
        for child in value.values():
            found.extend(recursive_products(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(recursive_products(child))
    return found


def first_product_jsonld(soup):
    products = []
    for obj in jsonld_objects(soup):
        products.extend(recursive_products(obj))
    return products[0] if products else {}


def text_lines(soup):
    text = soup.get_text("\n", strip=True).replace("\xa0", " ")
    return [re.sub(r"\s+", " ", line).strip() for line in text.split("\n") if line.strip()]


def value_after_label(lines, labels):
    wanted = {label.lower() for label in labels}
    for index, line in enumerate(lines):
        normalized = line.strip().lower().rstrip(":")
        if normalized in wanted and index + 1 < len(lines):
            return lines[index + 1]
    return ""


def property_values(product, terms):
    values = []
    for prop in product.get("additionalProperty", []) if isinstance(product, dict) else []:
        if not isinstance(prop, dict):
            continue
        name = clean(prop.get("name")).lower()
        if any(term in name for term in terms):
            value = clean(prop.get("value"))
            if value:
                values.append(value)
    return values


def variant_values(product, terms):
    values = []
    variants = product.get("hasVariant", []) if isinstance(product, dict) else []
    if isinstance(variants, dict):
        variants = [variants]
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        for key, value in variant.items():
            if any(term in str(key).lower() for term in terms):
                if isinstance(value, list):
                    values.extend(clean(item) for item in value)
                else:
                    values.append(clean(value))
        name = clean(variant.get("name"))
        if name:
            values.append(name)
    return values


def unique_join(values):
    result = []
    seen = set()
    for value in values:
        value = clean(value)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return ", ".join(result)


def extract_price(product, lines):
    offers = product.get("offers", {}) if isinstance(product, dict) else {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if isinstance(offers, dict):
        price = clean(offers.get("price"))
        currency = clean(offers.get("priceCurrency"))
        if price:
            return f"{currency} {price}".strip()
    for line in lines:
        if re.search(r"(?:₹|INR)\s*[\d,]+", line, re.I):
            return line
    return ""


def excluded_item(name, url):
    combined = f"{name} {url}".lower()
    terms = ["accessory", "tablet cover", "charger", "refurbished"]
    return any(term in combined for term in terms)


def extract_detail(session, url, flags):
    response = request_page(session, url, flags)
    if response is None:
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    product = first_product_jsonld(soup)
    lines = text_lines(soup)
    name = clean(product.get("name")) or clean(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
    model_code = clean(product.get("model")) or clean(product.get("mpn")) or clean(product.get("sku"))
    colors = unique_join(property_values(product, ["color", "colour"]) + variant_values(product, ["color", "colour"]))
    storage = unique_join(property_values(product, ["storage", "capacity"]) + variant_values(product, ["storage", "capacity"]))
    colors = colors or value_after_label(lines, ["Colour", "Color", "Available Colors"])
    storage = storage or value_after_label(lines, ["Storage", "Storage Variants"])
    display = value_after_label(lines, ["Display", "Display Specs", "Screen"])
    camera = value_after_label(lines, ["Camera", "Camera Specs"])
    battery = value_after_label(lines, ["Battery", "Battery Capacity"])
    fields = {
        "Product Name": name,
        "Model Code": model_code,
        "Price in INR": extract_price(product, lines),
        "Available Colors": colors,
        "Storage Variants": storage,
        "Display Specs": display,
        "Camera Specs": camera,
        "Battery Capacity": battery,
        "Source URL": url,
    }
    required = list(fields)[:-1]
    for field in required:
        if not fields[field]:
            flags.append({
                "Signal": "Low-confidence extraction",
                "URL": url,
                "Message": f"Field not verified or not exposed in the inspected HTML: {field}",
            })
    if not name:
        flags.append({"Signal": "Low-confidence extraction", "URL": url, "Message": "Product name not found"})
    return fields


def main():
    parser = argparse.ArgumentParser(description="Samsung India smartphone scraper")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    args = parser.parse_args()

    log("Starting scraper")
    input_df = pd.read_excel(args.input).fillna("")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})
    flags = []
    extracted = []
    processed = set()

    for _, row in input_df.iterrows():
        if args.max_products is not None and len(extracted) >= args.max_products:
            break
        keyword = value_from_row(row, "keyword")
        listing_url = value_from_row(row, "listing_url")
        detail_url = value_from_row(row, "detail_url")
        if detail_url:
            urls = [detail_url]
        else:
            listing_url = listing_url or TARGET_URL
            response = request_page(session, listing_url, flags)
            if response is None:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            urls = listing_links(soup, listing_url)
            log(f"Found {len(urls)} JSON-LD listing links")

        for url in urls:
            if args.max_products is not None and len(extracted) >= args.max_products:
                break
            if url in processed:
                continue
            processed.add(url)
            if keyword and keyword.lower() not in url.lower() and not detail_url:
                pass
            item = extract_detail(session, url, flags)
            if not item:
                continue
            if excluded_item(item["Product Name"], url):
                flags.append({"Signal": "Excluded item", "URL": url, "Message": "Matched accessory, tablet cover, charger, or refurbished exclusion"})
                continue
            if keyword and keyword.lower() not in item["Product Name"].lower():
                continue
            extracted.append(item)
            log(f"Extracted {item['Product Name'] or url}")

    output_path = Path(args.output)
    log(f"Writing {len(extracted)} extracted rows and {len(flags)} flags")
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        data_columns = ["Product Name", "Model Code", "Price in INR", "Available Colors", "Storage Variants", "Display Specs", "Camera Specs", "Battery Capacity", "Source URL"]
        pd.DataFrame(extracted, columns=data_columns).to_excel(writer, sheet_name="Extracted Data", index=False)
        pd.DataFrame(flags, columns=["Signal", "URL", "Message"]).to_excel(writer, sheet_name="Flags", index=False)
    log("Scraper completed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"Fatal error: {exc}")
        sys.exit(1)