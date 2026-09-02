import argparse
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://books.toscrape.com/"
USER_AGENT = "Mozilla/5.0 (compatible; ScraperStudio/1.0)"
RATING_VALUES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def log(message):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{timestamp}] {message}", flush=True)


def get_proxy():
    return None


def solve_captcha(*args, **kwargs):
    raise NotImplementedError("CAPTCHA solving is intentionally not implemented")


def detect_anti_bot_signals(response):
    """Return signals found in an HTTP response; this never bypasses anti-bot controls."""
    signals = []
    body = response.text or ""
    lowered = body.lower()
    url = response.url

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
        if marker in lowered:
            signals.append(description)

    if len(body.strip()) < 200:
        signals.append("Unusually short response body")

    return [{"signal": signal, "url": url} for signal in signals]


def add_flag(flags, kind, message, url="", context=""):
    flags.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": kind,
        "message": message,
        "url": url,
        "context": context,
    })


def fetch(session, url, flags):
    log(f"Fetching {url}")
    try:
        response = session.get(url, timeout=30, allow_redirects=True)
        for signal in detect_anti_bot_signals(response):
            add_flag(flags, "anti-bot", signal["signal"], signal["url"])
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        add_flag(flags, "error", f"HTTP request failed: {exc}", url)
        log(f"Request failed: {url}: {exc}")
        return None


def clean_text(node):
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def rating_from_node(node):
    if not node:
        return None
    classes = node.get("class", [])
    for value in classes:
        normalized = value.strip().lower()
        if normalized in RATING_VALUES:
            return normalized.title()
        if normalized.endswith("-star"):
            name = normalized[:-5]
            if name in RATING_VALUES:
                return name.title()
    text = clean_text(node).lower()
    for name in RATING_VALUES:
        if name in text:
            return name.title()
    return None


def rating_number(rating):
    return RATING_VALUES.get((rating or "").lower())


def parse_price(node):
    value = clean_text(node)
    return value


def parse_availability(node):
    return clean_text(node)


def category_from_detail(soup, fallback=""):
    breadcrumb = soup.select_one("ul.breadcrumb")
    if breadcrumb:
        links = breadcrumb.select("a")
        if len(links) >= 2:
            return clean_text(links[-1])
    return fallback


def extract_detail(response, flags, fallback_category=""):
    soup = BeautifulSoup(response.text, "html.parser")
    title = clean_text(soup.select_one("div.product_main h1, h1"))
    price = parse_price(soup.select_one("div.product_main p.price_color, p.price_color"))
    availability = parse_availability(soup.select_one("div.product_main p.instock, p.instock"))
    rating_node = soup.select_one("div.product_main p.star-rating, p.star-rating")
    rating = rating_from_node(rating_node)
    category = category_from_detail(soup, fallback_category)
    product_url = response.url
    upc = ""

    for row in soup.select("table.table tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2 and clean_text(cells[0]).lower() == "upc":
            upc = clean_text(cells[1])
            break

    missing = []
    for field, value in (("title", title), ("price", price), ("availability", availability),
                         ("star rating", rating), ("UPC", upc)):
        if not value:
            missing.append(field)
    if missing:
        add_flag(flags, "low-confidence", "Missing detail fields: " + ", ".join(missing), product_url)

    if not rating or rating_number(rating) < 3:
        return None
    if not availability:
        return None
    if "out of stock" in availability.lower():
        return None

    return {
        "Book Title": title,
        "Category": category,
        "Price (GBP)": price,
        "Star Rating": rating,
        "Availability Status": availability,
        "Product URL": product_url,
        "UPC Code": upc,
    }


def listing_cards(soup):
    cards = soup.select("article.product_pod")
    if not cards:
        cards = soup.select("article")
    return cards


def parse_listing(response, fallback_category=""):
    soup = BeautifulSoup(response.text, "html.parser")
    heading = clean_text(soup.select_one("div.page-header h1, h1")) or fallback_category
    items = []
    for card in listing_cards(soup):
        link = card.select_one("h3 a")
        if not link or not link.get("href"):
            continue
        items.append({
            "url": urljoin(response.url, link["href"]),
            "category": heading,
            "listing_title": link.get("title") or clean_text(link),
        })
    next_link = soup.select_one("li.next a")
    next_url = urljoin(response.url, next_link["href"]) if next_link and next_link.get("href") else None
    return items, next_url


def resolve_keyword(session, keyword, flags):
    if keyword.strip().lower() == "travel":
        return urljoin(BASE_URL, "catalogue/category/books/travel_2/index.html"), "Travel"

    response = fetch(session, BASE_URL, flags)
    if not response:
        return None, keyword
    soup = BeautifulSoup(response.text, "html.parser")
    wanted = keyword.strip().casefold()
    for link in soup.select("ul.nav-list a, a"):
        if clean_text(link).casefold() == wanted and link.get("href"):
            return urljoin(response.url, link["href"]), clean_text(link)
    add_flag(flags, "low-confidence", f"No category link matched keyword '{keyword}'", BASE_URL)
    return None, keyword


def crawl_listing(session, start_url, fallback_category, flags, max_products, results, seen_details):
    current = start_url
    seen_pages = set()
    while current and current not in seen_pages:
        if max_products is not None and len(results) >= max_products:
            break
        seen_pages.add(current)
        response = fetch(session, current, flags)
        if not response:
            break
        cards, next_url = parse_listing(response, fallback_category)
        log(f"Found {len(cards)} cards on listing page")
        for card in cards:
            if max_products is not None and len(results) >= max_products:
                break
            detail_url = card["url"]
            if detail_url in seen_details:
                continue
            seen_details.add(detail_url)
            detail_response = fetch(session, detail_url, flags)
            if not detail_response:
                continue
            item = extract_detail(detail_response, flags, card["category"])
            if item:
                results.append(item)
        current = next_url
        if current:
            log("Following next listing page")


def read_cell(row, name):
    value = row.get(name, "")
    if pd.isna(value):
        return ""
    return str(value).strip()


def main():
    parser = argparse.ArgumentParser(description="Books to Scrape category/detail scraper")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    args = parser.parse_args()

    log("Reading input workbook")
    try:
        input_df = pd.read_excel(args.input)
    except Exception as exc:
        raise SystemExit(f"Could not read input workbook: {exc}")

    results = []
    flags = []
    seen_details = set()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})

    for row_number, row in input_df.iterrows():
        if args.max_products is not None and len(results) >= args.max_products:
            break
        keyword = read_cell(row, "keyword")
        listing_url = read_cell(row, "listing_url")
        detail_url = read_cell(row, "detail_url")
        log(f"Processing input row {row_number + 2}")

        if detail_url:
            # Independent fast path: no listing discovery is performed.
            if detail_url in seen_details:
                continue
            seen_details.add(detail_url)
            response = fetch(session, detail_url, flags)
            if response:
                item = extract_detail(response, flags, keyword)
                if item:
                    results.append(item)
        elif listing_url:
            crawl_listing(session, listing_url, keyword, flags, args.max_products, results, seen_details)
        elif keyword:
            resolved, category = resolve_keyword(session, keyword, flags)
            if resolved:
                crawl_listing(session, resolved, category, flags, args.max_products, results, seen_details)
        else:
            add_flag(flags, "error", "Input row has no keyword, listing_url, or detail_url", context=f"row {row_number + 2}")

    columns = ["Book Title", "Category", "Price (GBP)", "Star Rating", "Availability Status", "Product URL", "UPC Code"]
    extracted_df = pd.DataFrame(results, columns=columns)
    flags_columns = ["timestamp", "type", "message", "url", "context"]
    flags_df = pd.DataFrame(flags, columns=flags_columns)
    log(f"Writing {len(extracted_df)} extracted items and {len(flags_df)} flags")
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        extracted_df.to_excel(writer, sheet_name="Extracted Data", index=False)
        flags_df.to_excel(writer, sheet_name="Flags", index=False)
    log("Finished")


if __name__ == "__main__":
    main()
