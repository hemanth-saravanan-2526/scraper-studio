import argparse
import json
import re
import sys
import traceback
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from openpyxl import Workbook, load_workbook
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

TARGET_URL = "https://www.samsung.com/in/smartphones/all-smartphones/"


def log(message):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def get_proxy():
    return None


def solve_captcha(*args, **kwargs):
    raise NotImplementedError("CAPTCHA solving and anti-bot bypass are not implemented")


def detect_anti_bot_signals(status, body, url=""):
    """Return signals only; this function never attempts to bypass anything."""
    signals = []
    text = (body or "")[:100000].lower()
    if status in (403, 429):
        signals.append(f"HTTP status {status}")
    markers = {
        "captcha": "CAPTCHA marker",
        "cloudflare": "Cloudflare marker",
        "cf-chl-": "Cloudflare challenge marker",
        "perimeterx": "PerimeterX marker",
        "datadome": "DataDome marker",
    }
    for marker, description in markers.items():
        if marker in text:
            signals.append(description)
    if len(body or "") < 200:
        signals.append("Unusually short HTTP response body")
    return signals


def dismiss_cookie_banner(page):
    """Try common consent labels; absence of a banner is not fatal."""
    labels = ["Accept all", "Accept", "I agree", "Allow all", "Agree"]
    for label in labels:
        try:
            locator = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I)).first
            if locator.is_visible(timeout=500):
                locator.click(timeout=700)
                log(f"Cookie consent button clicked: {label}")
                return True
        except Exception:
            pass
    log("Cookie dismissal was attempted but no consent button matched; possible layout change")
    return False


def add_flag(flags, flag_type, url, message):
    flags.append({"Type": flag_type, "URL": url or "", "Message": message})


def jsonld_objects(html):
    soup = BeautifulSoup(html, "html.parser")
    objects = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
            if isinstance(value, list):
                objects.extend(value)
            else:
                objects.append(value)
        except Exception:
            continue
    return objects


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def first_key(objects, keys):
    wanted = {key.lower() for key in keys}
    for root in objects:
        for value in walk(root):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() in wanted and item not in (None, "", []):
                        return item
    return None


def all_key_values(objects, keys):
    wanted = {key.lower() for key in keys}
    result = []
    for root in objects:
        for value in walk(root):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() in wanted and item not in (None, "", []):
                        if isinstance(item, list):
                            result.extend(item)
                        else:
                            result.append(item)
    return result


def clean_values(values):
    output = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("name") or value.get("value") or value.get("option")
        if value is not None:
            value = str(value).strip()
            if value and value not in output:
                output.append(value)
    return output


def product_items(objects):
    items = []
    for root in objects:
        for value in walk(root):
            if isinstance(value, dict) and str(value.get("@type", "")).lower() == "itemlist":
                entries = value.get("itemListElement", [])
                if isinstance(entries, list):
                    for entry in entries:
                        item = entry.get("item") if isinstance(entry, dict) else entry
                        if isinstance(item, str):
                            items.append({"url": item})
                        elif isinstance(item, dict):
                            items.append(item)
    return items


def title_slug_match(item, keyword):
    if not keyword:
        return True
    needle = str(keyword).strip().lower()
    title = str(item.get("name") or item.get("title") or "").lower()
    slug = urlparse(str(item.get("url") or item.get("@id") or "")).path.lower()
    return needle in title or needle in slug


def extract_detail(page, url, keyword=None):
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    objects = jsonld_objects(html)
    title = first_key(objects, ["name", "headline"])
    if not title:
        heading = soup.select_one("h1") or soup.select_one("h2")
        title = heading.get_text(" ", strip=True) if heading else None
    title = str(title).strip() if title else None
    if keyword and not (str(keyword).lower() in (title or "").lower() or str(keyword).lower() in urlparse(url).path.lower()):
        return None

    price = first_key(objects, ["price"])
    if isinstance(price, dict):
        price = price.get("value") or price.get("amount")
    currency = first_key(objects, ["priceCurrency", "currency"])
    model = first_key(objects, ["mpn", "model", "modelNumber", "sku"])
    availability = first_key(objects, ["availability"])
    if isinstance(availability, dict):
        availability = availability.get("value") or availability.get("name")

    colors = clean_values(all_key_values(objects, ["color", "colour", "colorOptions", "colourOptions"]))
    storage = clean_values(all_key_values(objects, ["storage", "storageCapacity", "storageOptions", "capacity"]))
    display = first_key(objects, ["display", "displaySize", "screenSize", "displaySpecs"])
    camera = first_key(objects, ["camera", "cameraSpecs", "cameraResolution"])

    # The page exposes Device, Storage, and Colour controls.  Use their option
    # text only as a fallback because the exact control DOM was not specified.
    for label, target in (("storage", storage), ("colour", colors), ("color", colors)):
        if not target:
            pattern = re.compile(label, re.I)
            heading = next((h for h in soup.find_all(["h2", "h3", "label", "legend"]) if pattern.search(h.get_text(" ", strip=True))), None)
            if heading:
                container = heading.parent
                target.extend(clean_values([x.get_text(" ", strip=True) for x in container.find_all(["button", "option", '[role="option"]'])]))

    return {
        "Product Title": title,
        "Model Code": model,
        "Price (INR)": price if str(currency or "INR").upper() in ("INR", "INR ", "₹") else price,
        "Color options": "; ".join(colors) if colors else None,
        "Storage capacity": "; ".join(storage) if storage else None,
        "Display specs": display,
        "Camera specs": camera,
        "Availability status": availability,
        "URL": url,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    args = parser.parse_args()

    rows = []
    try:
        workbook = load_workbook(args.input, read_only=True, data_only=True)
        sheet = workbook.active
        headers = [str(x).strip().lower() if x is not None else "" for x in next(sheet.iter_rows(values_only=True))]
        for values in sheet.iter_rows(min_row=2, values_only=True):
            row = dict(zip(headers, values))
            rows.append({key: row.get(key) for key in ("keyword", "listing_url", "detail_url")})
    except Exception as exc:
        log(f"Input read error: {exc}")
        rows = [{"keyword": None, "listing_url": TARGET_URL, "detail_url": None}]

    extracted = []
    flags = []
    seen = set()
    count = 0
    log("Starting browser-based scraper; no API was observed during inspection")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, proxy=get_proxy())
        page = browser.new_page()

        def inspect_response(response):
            try:
                body = response.body()
                signals = detect_anti_bot_signals(response.status, body.decode("utf-8", "ignore"), response.url)
                for signal in signals:
                    add_flag(flags, "anti-bot", response.url, signal)
            except Exception:
                pass

        page.on("response", inspect_response)
        for input_row in rows:
            if args.max_products is not None and count >= args.max_products:
                break
            keyword = input_row.get("keyword")
            detail_url = input_row.get("detail_url")
            listing_url = input_row.get("listing_url") or TARGET_URL
            try:
                if detail_url:
                    urls = [(str(detail_url).strip(), keyword)]
                else:
                    log(f"Opening listing: {listing_url}")
                    page.goto(str(listing_url), wait_until="domcontentloaded", timeout=60000)
                    if not dismiss_cookie_banner(page):
                        add_flag(flags, "low-confidence", listing_url, "Cookie dismissal attempted but no common consent button matched")
                    page.wait_for_timeout(1500)
                    items = product_items(jsonld_objects(page.content()))
                    if not items:
                        add_flag(flags, "low-confidence", listing_url, "ItemList JSON-LD found no usable product entries")
                    urls = []
                    for item in items:
                        if title_slug_match(item, keyword):
                            raw = item.get("url") or item.get("@id")
                            if raw:
                                urls.append((urljoin(str(listing_url), str(raw)), keyword))
                for url, item_keyword in urls:
                    if args.max_products is not None and count >= args.max_products:
                        break
                    if url in seen:
                        continue
                    seen.add(url)
                    log(f"Opening detail page: {url}")
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    if not dismiss_cookie_banner(page):
                        add_flag(flags, "low-confidence", url, "Cookie dismissal attempted but no common consent button matched")
                    page.wait_for_timeout(1200)
                    record = extract_detail(page, url, item_keyword)
                    if record is None:
                        continue
                    extracted.append(record)
                    count += 1
                    for field in ("Model Code", "Price (INR)", "Color options", "Storage capacity", "Display specs", "Camera specs", "Availability status"):
                        if record.get(field) in (None, ""):
                            add_flag(flags, "low-confidence", url, f"Field not exposed or not confidently extracted: {field}")
            except PlaywrightTimeoutError as exc:
                add_flag(flags, "error", str(detail_url or listing_url), f"Page timeout: {exc}")
            except Exception as exc:
                add_flag(flags, "error", str(detail_url or listing_url), f"Extraction error: {exc}")
                log(traceback.format_exc())
        browser.close()

    log(f"Writing {len(extracted)} extracted rows and {len(flags)} flags")
    out = Workbook()
    data_sheet = out.active
    data_sheet.title = "Extracted Data"
    data_headers = ["Product Title", "Model Code", "Price (INR)", "Color options", "Storage capacity", "Display specs", "Camera specs", "Availability status", "URL"]
    data_sheet.append(data_headers)
    for record in extracted:
        data_sheet.append([record.get(header) for header in data_headers])
    flag_sheet = out.create_sheet("Flags")
    flag_sheet.append(["Type", "URL", "Message"])
    for flag in flags:
        flag_sheet.append([flag["Type"], flag["URL"], flag["Message"])
    out.save(args.output)
    log("Done")


if __name__ == "__main__":
    main()
