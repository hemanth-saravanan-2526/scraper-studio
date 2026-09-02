import argparse
import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

TARGET_URL = "https://restaurants.subway.com/united-states/al"
OUTPUT_COLUMNS = [
    "Store Number",
    "Street Address",
    "City",
    "State",
    "Postal Code",
    "Phone Number",
    "Operating Hours",
    "Drive-Thru Available",
    "Delivery Available",
]

flags = []


def log(message):
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}", flush=True)


def add_flag(flag_type, message, url=""):
    flags.append({
        "Timestamp": datetime.now(timezone.utc).isoformat(),
        "Type": flag_type,
        "Message": message,
        "URL": url,
    })


def get_proxy():
    return None


def solve_captcha(*args, **kwargs):
    raise NotImplementedError("CAPTCHA solving is intentionally not implemented")


async def detect_anti_bot_signals(response):
    """Flag anti-bot responses without attempting to bypass them."""
    url = response.url
    status = response.status
    resource_type = response.request.resource_type
    if status in (403, 429):
        add_flag("anti-bot", f"HTTP status {status}", url)
    if resource_type != "document":
        return
    try:
        body = await response.text()
    except Exception as exc:
        add_flag("response-check", f"Could not inspect response body: {exc}", url)
        return
    marker_pattern = re.compile(
        r"captcha|cloudflare|perimeterx|datadome|verify you are human|access denied",
        re.IGNORECASE,
    )
    if marker_pattern.search(body):
        add_flag("anti-bot", "Possible CAPTCHA, WAF, or bot-verification marker", url)
    if len(body.strip()) < 500:
        add_flag("anti-bot", "Unusually short HTML document body", url)


async def dismiss_cookie_banner(page):
    """Try common consent labels; absence of a banner is treated as non-fatal."""
    labels = ["Accept all", "Accept", "I agree", "Allow all", "Agree"]
    for label in labels:
        try:
            locator = page.get_by_text(label, exact=True).first
            await locator.wait_for(state="visible", timeout=700)
            await locator.click(timeout=700)
            log(f"Dismissed a possible cookie banner using '{label}'")
            return
        except Exception:
            continue
    log("Cookie-banner dismissal was attempted, but no consent button matched")
    add_flag("layout-change", "Cookie-banner dismissal attempted but no consent button matched", page.url)


async def goto_with_dismissal(page, url):
    log(f"Opening {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await dismiss_cookie_banner(page)
    await page.wait_for_timeout(1000)


def clean_text(value):
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def yes_no_from_text(text, terms):
    lower = text.lower()
    for term in terms:
        if term in lower:
            window_start = max(0, lower.find(term) - 20)
            window = lower[window_start:lower.find(term) + len(term) + 35]
            if re.search(r"\b(no|not available|unavailable|false)\b", window):
                return "No"
            if re.search(r"\b(yes|available|true)\b", window):
                return "Yes"
    return None


def extract_records(html, source_url):
    soup = BeautifulSoup(html, "html.parser")
    zip_nodes = soup.find_all(string=re.compile(r"\bAL\s+\d{5}(?:-\d{4})?\b", re.IGNORECASE))
    records = []
    seen = set()
    for node in zip_nodes:
        ancestor = node.parent
        chosen = None
        for _ in range(7):
            if ancestor is None:
                break
            text = ancestor.get_text("\n", strip=True)
            lower = text.lower()
            if re.search(r"\bAL\s+\d{5}(?:-\d{4})?\b", text, re.IGNORECASE) and re.search(
                r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b.*?\b(?:am|pm)\b",
                text,
                re.IGNORECASE | re.DOTALL,
            ):
                chosen = text
                if len(text) < 1800:
                    break
            ancestor = ancestor.parent
        if not chosen:
            continue
        normalized = clean_text(chosen)
        if re.search(r"\b(?:closed|temporar(?:y|ily)|kiosk)\b", normalized, re.IGNORECASE):
            add_flag("filtered", "Possible closed location or temporary kiosk was skipped", source_url)
            continue
        match = re.search(
            r"(?P<street>\d+\s+[^,\n]+?)\s+(?P<city>[A-Za-z][A-Za-z .'-]*),\s*AL\s+(?P<postal>\d{5}(?:-\d{4})?)",
            chosen,
            re.IGNORECASE,
        )
        if not match:
            add_flag("low-confidence", "Could not reliably parse an Alabama street address", source_url)
            continue
        street = clean_text(match.group("street"))
        city = clean_text(match.group("city"))
        postal = match.group("postal")
        key = (street.lower(), city.lower(), postal)
        if key in seen:
            continue
        seen.add(key)
        lines = [clean_text(x) for x in chosen.split("\n") if clean_text(x)]
        hour_lines = [
            line for line in lines
            if re.search(r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", line, re.IGNORECASE)
            and re.search(r"\b(?:am|pm)\b", line, re.IGNORECASE)
        ]
        phone_match = re.search(r"\(?[2-9]\d{2}\)?[-.\s]\d{3}[-.\s]\d{4}", chosen)
        store_match = re.search(r"(?:store\s*(?:number|no\.?|#)|location\s*(?:number|no\.?|#))\s*[:#]?\s*([A-Za-z0-9-]+)", chosen, re.IGNORECASE)
        record = {
            "Store Number": store_match.group(1) if store_match else None,
            "Street Address": street,
            "City": city,
            "State": "AL",
            "Postal Code": postal,
            "Phone Number": phone_match.group(0) if phone_match else None,
            "Operating Hours": ", ".join(hour_lines) if hour_lines else None,
            "Drive-Thru Available": yes_no_from_text(chosen, ["drive-thru", "drive thru", "drive-through"]),
            "Delivery Available": yes_no_from_text(chosen, ["delivery"]),
        }
        if any(record[field] is None for field in ["Store Number", "Phone Number", "Drive-Thru Available", "Delivery Available"]):
            add_flag("low-confidence", "Some requested fields were not visible or could not be inferred", source_url)
        records.append(record)
    return records


async def extract_current_page(page):
    html = await page.content()
    return extract_records(html, page.url)


async def discover_city_links(page, keyword):
    links = await page.locator("a").evaluate_all(
        "els => els.map(a => ({text: (a.innerText || '').trim(), href: a.href}))"
    )
    result = []
    wanted = (keyword or "").strip().lower()
    for item in links:
        href = item.get("href", "")
        text = item.get("text", "")
        path = urlparse(href).path.rstrip("/").lower()
        if "/united-states/al/" not in path:
            continue
        if not text or path.count("/") != 4:
            continue
        if wanted and wanted not in f"{text} {path.rsplit('/', 1)[-1]}`".lower():
            continue
        if href not in [x[1] for x in result]:
            result.append((text, href))
    return result


async def crawl_listing(page, listing_url, keyword, max_products):
    records = []
    await goto_with_dismissal(page, listing_url)
    city_links = await discover_city_links(page, keyword)
    if not city_links:
        add_flag("low-confidence", "No matching Alabama city links were exposed in the rendered listing", listing_url)
        records.extend(await extract_current_page(page))
        return records[:max_products] if max_products else records
    for city_name, city_url in city_links:
        if max_products and len(records) >= max_products:
            break
        log(f"Selecting city link: {city_name}")
        before = page.url
        try:
            await page.locator("a").filter(has_text=city_name).first.click(timeout=5000)
            await page.wait_for_timeout(700)
        except Exception as exc:
            add_flag("navigation", f"City-link click failed: {exc}", city_url)
        if page.url == before or "/united-states/al/" not in urlparse(page.url).path:
            await goto_with_dismissal(page, city_url)
        records.extend(await extract_current_page(page))
        if max_products:
            records = records[:max_products]
    return records


async def run(input_path, output_path, max_products):
    log("Reading input workbook")
    frame = pd.read_excel(input_path)
    for column in ["keyword", "listing_url", "detail_url"]:
        if column not in frame.columns:
            frame[column] = ""
    records = []
    proxy = get_proxy()
    if proxy is not None:
        log("A proxy was supplied by get_proxy, but proxy use is not implemented")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        async def response_handler(response):
            asyncio.create_task(detect_anti_bot_signals(response))

        page.on("response", response_handler)
        rows = frame.to_dict("records")
        if not rows:
            rows = [{"keyword": "", "listing_url": TARGET_URL, "detail_url": ""}]
        for row in rows:
            if max_products and len(records) >= max_products:
                break
            detail_url = str(row.get("detail_url") or "").strip()
            listing_url = str(row.get("listing_url") or "").strip() or TARGET_URL
            keyword = str(row.get("keyword") or "").strip()
            if detail_url:
                log(f"Using independent detail URL fast path: {detail_url}")
                await goto_with_dismissal(page, detail_url)
                records.extend(await extract_current_page(page))
            else:
                records.extend(await crawl_listing(page, listing_url, keyword, max_products))
            if max_products:
                records = records[:max_products]
        await page.wait_for_timeout(500)
        await browser.close()
    log(f"Writing {len(records)} extracted records")
    extracted = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    flag_frame = pd.DataFrame(flags, columns=["Timestamp", "Type", "Message", "URL"])
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        extracted.to_excel(writer, sheet_name="Extracted Data", index=False)
        flag_frame.to_excel(writer, sheet_name="Flags", index=False)
    log("Run complete")


def main():
    parser = argparse.ArgumentParser(description="Scrape Subway Alabama listings")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(run(args.input, args.output, args.max_products))


if __name__ == "__main__":
    main()