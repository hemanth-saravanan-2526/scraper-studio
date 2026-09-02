import argparse
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

DEFAULT_LISTING_URL = "https://www.samsung.com/in/smartphones/all-smartphones/"
USER_AGENT = "Mozilla/5.0 (compatible; ScraperStudio/1.0; +https://www.samsung.com/)"


def log(message):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def get_proxy():
    """Placeholder for an externally supplied proxy integration."""
    return None


def solve_captcha(*args, **kwargs):
    """CAPTCHA solving and anti-bot bypass are intentionally not implemented."""
    raise NotImplementedError("CAPTCHA solving is not implemented")


def detect_anti_bot_signals(response):
    """Return descriptive anti-bot signals without attempting to bypass them."""
    signals = []
    body = response.text or ""
    lowered = body.lower()
    if response.status_code in (403, 429):
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
        signals.append(f"unusually short response body ({len(body.strip())} characters)")
    return signals


def add_flag(flags, flag_type, url, message):
    flags.append({
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "type": flag_type,
        "url": url or "",
        "message": message,
    })


def fetch(session, url, flags):
    log(f"Fetching {url}")
    try:
        response = session.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"},
            timeout=30,
            proxies=get_proxy(),
        )
    except requests.RequestException as exc:
        add_flag(flags, "error", url, f"HTTP request failed: {exc}")
        return None
    for signal in detect_anti_bot_signals(response):
        add_flag(flags, "anti-bot", url, signal)
    if response.status_code >= 400:
        add_flag(flags, "error", url, f"HTTP status {response.status_code}")
        return None
    return response


def jsonld_objects(soup):
    objects = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, list):
            objects.extend(value)
        else:
            objects.append(value)
    return objects


def object_types(obj):
    value = obj.get("@type", []) if isinstance(obj, dict) else []
    return value if isinstance(value, list) else [value]


def listing_items(soup, listing_url):
    items = []
    for obj in jsonld_objects(soup):
        candidates = []
        if isinstance(obj, dict) and ("ItemList" in object_types(obj) or "itemListElement" in obj):
            candidates = obj.get("itemListElement", [])
        for entry in candidates:
            item = entry.get("item", entry) if isinstance(entry, dict) else entry
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("@id")
            name = item.get("name") or item.get("headline") or ""
            if url:
                items.append({"url": urljoin(listing_url, url), "name": str(name)})
    deduped = []
    seen = set()
    for item in items:
        if item["url"] not in seen:
            seen.add(item["url"])
            deduped.append(item)
    return deduped


def excluded_product(name, url):
    value = f"{name} {url}".lower()
    return any(word in value for word in ("accessor", "case", "refurbished"))


def find_first(obj, keys):
    if isinstance(obj, dict):
        for key in keys:
            if obj.get(key) not in (None, "", []):
                return obj.get(key)
        for value in obj.values():
            result = find_first(value, keys)
            if result not in (None, "", []):
                return result
    elif isinstance(obj, list):
        for value in obj:
            result = find_first(value, keys)
            if result not in (None, "", []):
                return result
    return None


def all_product_objects(objects):
    result = []
    for obj in objects:
        if isinstance(obj, dict):
            types = [str(x).lower() for x in object_types(obj)]
            if any(x in ("product", "productgroup", "productmodel") for x in types):
                result.append(obj)
            graph = obj.get("@graph")
            if isinstance(graph, list):
                result.extend(all_product_objects(graph))
    return result


def regex_value(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            return re.sub(r"\\s+", " ", match.group(1)).strip(" :;-\n")
    return ""


def extract_detail(response, url, flags):
    soup = BeautifulSoup(response.text, "html.parser")
    text = re.sub(r"\\s+", " ", soup.get_text(" ", strip=True))
    objects = jsonld_objects(soup)
    products = all_product_objects(objects)
    product = products[0] if products else {}

    title = product.get("name", "") if isinstance(product, dict) else ""
    if not title:
        heading = soup.find(["h1", "h2"])
        title = heading.get_text(" ", strip=True) if heading else ""
    if not title:
        title = regex_value(text, [r"(Galaxy\\s+[A-Za-z0-9 +\\-]+)"]) or ""

    model_code = find_first(product, ["sku", "mpn", "model", "modelCode"]) or ""
    if isinstance(model_code, dict):
        model_code = model_code.get("value", "") or model_code.get("name", "")
    offers = product.get("offers", {}) if isinstance(product, dict) else {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = offers.get("price", "") if isinstance(offers, dict) else ""
    currency = offers.get("priceCurrency", "INR") if isinstance(offers, dict) else "INR"
    price_value = f"{price} {currency}" if price != "" else ""

    colors = []
    color = product.get("color", "") if isinstance(product, dict) else ""
    if color:
        colors.extend(color if isinstance(color, list) else [color])
    storage = regex_value(text, [r"Storage\\s*[:\\-]?\\s*([0-9]+\\s*(?:GB|TB))", r"([0-9]+\\s*(?:GB|TB))\\s*(?:storage|ROM)"])
    display = regex_value(text, [r"Display(?:\\s+size)?\\s*[:\\-]?\\s*(.{0,180}?)(?:Camera|Processor|Battery|Memory|Storage)"])
    camera = regex_value(text, [r"Camera\\s*[:\\-]?\\s*(.{0,220}?)(?:Display|Processor|Battery|Memory|Storage)"])
    availability = ""
    if isinstance(offers, dict):
        availability = offers.get("availability", "")
    if availability:
        availability = str(availability).rsplit("/", 1)[-1]
    if not availability:
        availability = regex_value(text, [r"(In stock|Out of stock|Available|Sold out|Currently unavailable)"])

    fields = {
        "Product Title": title,
        "Model Code": str(model_code),
        "Price (INR)": price_value,
        "Color options": ", ".join(dict.fromkeys(str(x) for x in colors)),
        "Storage capacity": storage,
        "Display specs": display,
        "Camera specs": camera,
        "Availability status": availability,
        "Detail URL": url,
    }
    uncertain = [name for name, value in fields.items() if name != "Detail URL" and not value]
    if uncertain:
        add_flag(flags, "low-confidence", url, "Fields not exposed or not confidently located: " + ", ".join(uncertain))
    if not products:
        add_flag(flags, "low-confidence", url, "No Product/ProductGroup JSON-LD object was found")
    return fields


def read_input(path):
    try:
        frame = pd.read_excel(path)
    except Exception as exc:
        raise RuntimeError(f"Could not read input workbook: {exc}") from exc
    for column in ("keyword", "listing_url", "detail_url"):
        if column not in frame.columns:
            frame[column] = ""
    return frame.fillna("")


def main():
    parser = argparse.ArgumentParser(description="Samsung smartphone scraper")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    args = parser.parse_args()

    log("Starting scraper")
    try:
        input_frame = read_input(args.input)
    except RuntimeError as exc:
        log(str(exc))
        sys.exit(1)
    session = requests.Session()
    flags = []
    extracted = []
    processed = 0

    for _, row in input_frame.iterrows():
        keyword = str(row.get("keyword", "")).strip()
        detail_url = str(row.get("detail_url", "")).strip()
        listing_url = str(row.get("listing_url", "")).strip() or DEFAULT_LISTING_URL

        if detail_url:
            log(f"Using independent detail URL path: {detail_url}")
            response = fetch(session, detail_url, flags)
            if response is not None:
                extracted.append(extract_detail(response, detail_url, flags))
                processed += 1
            if args.max_products is not None and processed >= args.max_products:
                break
            continue

        log(f"Crawling listing page: {listing_url}")
        response = fetch(session, listing_url, flags)
        if response is None:
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        items = listing_items(soup, listing_url)
        if not items:
            add_flag(flags, "low-confidence", listing_url, "No ItemList JSON-LD products found; rendered card selectors were not known")
        keyword_lower = keyword.lower()
        if keyword:
            items = [item for item in items if keyword_lower in (item["name"] + " " + item["url"]).lower()]
        items = [item for item in items if not excluded_product(item["name"], item["url"])]
        log(f"Found {len(items)} matching listing products")
        for item in items:
            if args.max_products is not None and processed >= args.max_products:
                break
            product_response = fetch(session, item["url"], flags)
            if product_response is None:
                continue
            extracted.append(extract_detail(product_response, item["url"], flags))
            processed += 1
        if args.max_products is not None and processed >= args.max_products:
            break

    log(f"Writing {len(extracted)} extracted rows and {len(flags)} flags")
    data_columns = ["Product Title", "Model Code", "Price (INR)", "Color options", "Storage capacity", "Display specs", "Camera specs", "Availability status", "Detail URL"]
    flag_columns = ["timestamp", "type", "url", "message"]
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        pd.DataFrame(extracted, columns=data_columns).to_excel(writer, sheet_name="Extracted Data", index=False)
        pd.DataFrame(flags, columns=flag_columns).to_excel(writer, sheet_name="Flags", index=False)
    log("Scraper finished")


if __name__ == "__main__":
    main()
