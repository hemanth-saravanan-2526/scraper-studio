import argparse
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook, load_workbook

DEFAULT_LISTING_URL = "https://www.samsung.com/in/smartphones/all-smartphones/"
REQUEST_TIMEOUT = 30

DATA_HEADERS = [
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
FLAG_HEADERS = ["Timestamp", "Type", "URL", "Message"]


def log(message):
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    print(f"[{timestamp}] {message}", flush=True)


def get_proxy():
    """Placeholder for proxy configuration; anti-bot bypass is not implemented."""
    return None


def solve_captcha(*args, **kwargs):
    """Captcha solving is deliberately not implemented."""
    raise NotImplementedError("Captcha solving and anti-bot bypass are not implemented")


def detect_anti_bot_signals(response):
    """Return anti-bot observations for a response without attempting any bypass."""
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
    if len(body) < 500:
        signals.append(f"Unusually short response body ({len(body)} bytes)")
    return signals


def clean_text(value):
    if value is None:
        return None
    text = str(value).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def fetch(url, session, flags):
    log(f"Fetching {url}")
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, proxies=get_proxy())
        for signal in detect_anti_bot_signals(response):
            flags.append({
                "Timestamp": datetime.now(timezone.utc).isoformat(),
                "Type": "anti-bot",
                "URL": url,
                "Message": signal,
            })
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        flags.append({
            "Timestamp": datetime.now(timezone.utc).isoformat(),
            "Type": "error",
            "URL": url,
            "Message": f"Request failed: {exc}",
        })
        log(f"Request failed for {url}: {exc}")
        return None


def load_json_ld(soup):
    objects = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text("\n", strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                objects.extend(parsed)
            else:
                objects.append(parsed)
        except (json.JSONDecodeError, TypeError):
            continue
    return objects


def walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def first_value(data, keys):
    for obj in walk_json(data):
        for key in keys:
            value = obj.get(key)
            if value not in (None, "", []):
                if isinstance(value, dict):
                    value = value.get("name") or value.get("value") or value.get("@id")
                return clean_text(value)
    return None


def product_objects(json_ld):
    products = []
    for obj in walk_json(json_ld):
        obj_type = obj.get("@type")
        types = obj_type if isinstance(obj_type, list) else [obj_type]
        if "Product" in types:
            products.append(obj)
    return products


def is_excluded(title, url):
    combined = f"{title or ''} {url or ''}".casefold()
    return any(term in combined for term in ("accessor", "case", "refurbished"))


def listing_items(soup, base_url):
    items = []
    seen = set()
    json_ld = load_json_ld(soup)
    for obj in walk_json(json_ld):
        if obj.get("@type") == "ListItem" or "ListItem" in (obj.get("@type") or [] if isinstance(obj.get("@type"), list) else []):
            item = obj.get("item")
            if isinstance(item, dict):
                item_url = item.get("url") or item.get("@id")
                title = item.get("name")
            else:
                item_url = item
                title = obj.get("name")
            if item_url:
                absolute = urljoin(base_url, str(item_url))
                key = (absolute, clean_text(title))
                if absolute not in seen:
                    seen.add(absolute)
                    items.append({"url": absolute, "title": clean_text(title)})
    if items:
        return items

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        title = clean_text(anchor.get_text("\n", strip=True))
        absolute = urljoin(base_url, href)
        haystack = f"{title or ''} {absolute}".casefold()
        if absolute in seen or not title:
            continue
        if any(term in haystack for term in ("galaxy", "smartphone", "phone")):
            seen.add(absolute)
            items.append({"url": absolute, "title": title})
    return items


def find_labeled_text(soup, labels):
    label_pattern = re.compile("|".join(re.escape(label) for label in labels), re.IGNORECASE)
    for node in soup.find_all(string=label_pattern):
        parent = node.parent
        if parent is None:
            continue
        text = clean_text(parent.parent.get_text("\n", strip=True) if parent.parent else parent.get_text("\n", strip=True))
        if not text:
            continue
        match = re.search(r"(?:" + "|".join(re.escape(label) for label in labels) + r")\s*[:\-]?\s*(.+)", text, re.IGNORECASE)
        if match:
            value = clean_text(match.group(1))
            if value and value.casefold() not in {label.casefold() for label in labels}:
                return value
    return None


def extract_product(response, source_url, listing_title=None):
    soup = BeautifulSoup(response.text, "html.parser")
    json_ld = load_json_ld(soup)
    products = product_objects(json_ld)
    product = products[0] if products else {}
    page_text = clean_text(soup.get_text("\n", strip=True)) or ""

    title = clean_text(product.get("name")) or listing_title
    if not title:
        title_node = soup.find("h1")
        title = clean_text(title_node.get_text("\n", strip=True)) if title_node else None
    model = first_value(product, ["mpn", "model", "sku"])
    price = None
    offers = product.get("offers") if isinstance(product, dict) else None
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if isinstance(offers, dict):
        price = clean_text(offers.get("price"))
    price = price or find_labeled_text(soup, ["Price", "MRP", "Starting at"])
    if price and not re.search(r"\d", price):
        price = None

    color = clean_text(product.get("color")) if isinstance(product, dict) else None
    storage = find_labeled_text(soup, ["Storage", "Internal memory", "Memory"])
    display = find_labeled_text(soup, ["Display", "Screen size", "Resolution"])
    camera = find_labeled_text(soup, ["Camera", "Main camera", "Front camera"])
    availability = None
    if isinstance(offers, dict):
        availability = clean_text(offers.get("availability"))
    availability = availability or find_labeled_text(soup, ["Availability", "In stock", "Out of stock"])
    if availability and "/" in availability:
        availability = availability.rsplit("/", 1)[-1]

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


def add_low_confidence_flags(item, flags):
    missing = [key for key in DATA_HEADERS[:-1] if not item.get(key)]
    if missing:
        flags.append({
            "Timestamp": datetime.now(timezone.utc).isoformat(),
            "Type": "low-confidence extraction",
            "URL": item.get("Source URL"),
            "Message": "Unverified or missing fields: " + ", ".join(missing),
        })


def read_input(path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(value).strip().casefold() if value is not None else "" for value in rows[0]]
    result = []
    for values in rows[1:]:
        row = {headers[index]: (values[index] if index < len(values) else None) for index in range(len(headers))}
        result.append(row)
    return result


def write_output(path, data, flags):
    workbook = Workbook()
    data_sheet = workbook.active
    data_sheet.title = "Extracted Data"
    data_sheet.append(DATA_HEADERS)
    for item in data:
        data_sheet.append([item.get(header) for header in DATA_HEADERS])
    flag_sheet = workbook.create_sheet("Flags")
    flag_sheet.append(FLAG_HEADERS)
    for flag in flags:
        flag_sheet.append([flag.get(header) for header in FLAG_HEADERS])
    workbook.save(path)
    log(f"Wrote {len(data)} extracted item(s) and {len(flags)} flag(s) to {path}")


def main():
    parser = argparse.ArgumentParser(description="Samsung smartphone listing scraper")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    args = parser.parse_args()

    log("Starting scraper")
    rows = read_input(args.input)
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; ScraperStudio/1.0)",
        "Accept-Language": "en-IN,en;q=0.9",
    })
    extracted = []
    flags = []
    processed = 0

    for row_number, row in enumerate(rows, start=2):
        if args.max_products is not None and processed >= args.max_products:
            break
        keyword = clean_text(row.get("keyword"))
        detail_url = clean_text(row.get("detail_url"))
        listing_url = clean_text(row.get("listing_url")) or DEFAULT_LISTING_URL

        if detail_url:
            log(f"Row {row_number}: using independent detail_url fast path")
            response = fetch(detail_url, session, flags)
            if response is None:
                continue
            item = extract_product(response, detail_url)
            if is_excluded(item.get("Product Title"), detail_url):
                continue
            if keyword and keyword.casefold() not in f"{item.get('Product Title') or ''} {detail_url}".casefold():
                continue
            extracted.append(item)
            add_low_confidence_flags(item, flags)
            processed += 1
            continue

        log(f"Row {row_number}: crawling listing {listing_url}")
        response = fetch(listing_url, session, flags)
        if response is None:
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        candidates = listing_items(soup, listing_url)
        if not candidates:
            flags.append({
                "Timestamp": datetime.now(timezone.utc).isoformat(),
                "Type": "low-confidence extraction",
                "URL": listing_url,
                "Message": "No concrete product links were exposed; listing selectors were not verified.",
            })
        for candidate in candidates:
            if args.max_products is not None and processed >= args.max_products:
                break
            candidate_text = f"{candidate.get('title') or ''} {candidate.get('url') or ''}".casefold()
            if keyword and keyword.casefold() not in candidate_text:
                continue
            if is_excluded(candidate.get("title"), candidate.get("url")):
                continue
            detail_response = fetch(candidate["url"], session, flags)
            if detail_response is None:
                continue
            item = extract_product(detail_response, candidate["url"], candidate.get("title"))
            if is_excluded(item.get("Product Title"), candidate["url"]):
                continue
            extracted.append(item)
            add_low_confidence_flags(item, flags)
            processed += 1

    write_output(args.output, extracted, flags)
    log("Scraper finished")


if __name__ == "__main__":
    main()