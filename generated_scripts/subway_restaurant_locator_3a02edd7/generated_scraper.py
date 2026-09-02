import argparse
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://restaurants.subway.com/united-states/al"
USER_AGENT = "Mozilla/5.0 (compatible; SubwayLocationScraper/1.0)"

DATA_COLUMNS = [
    "Store Number",
    "Street Address",
    "City",
    "State",
    "Postal Code",
    "Phone Number",
    "Operating Hours",
    "Drive-Thru Available",
    "Delivery Available",
    "Detail URL",
]
FLAG_COLUMNS = ["Timestamp", "Type", "URL", "Message"]


def log(message):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def get_proxy():
    """Placeholder for proxy configuration; bypass behavior is intentionally absent."""
    return None


def solve_captcha(*args, **kwargs):
    """CAPTCHA solving is intentionally not implemented."""
    raise NotImplementedError("CAPTCHA solving is not implemented")


def detect_anti_bot_signals(response):
    """Return signal messages for one HTTP response without attempting any bypass."""
    signals = []
    body = response.text or ""
    lowered = body.lower()
    if response.status_code == 403:
        signals.append("HTTP 403 response")
    elif response.status_code == 429:
        signals.append("HTTP 429 response")
    markers = {
        "captcha": "CAPTCHA marker",
        "cloudflare": "Cloudflare marker",
        "perimeterx": "PerimeterX marker",
        "datadome": "DataDome marker",
    }
    for marker, message in markers.items():
        if marker in lowered:
            signals.append(message)
    if len(body.strip()) < 500:
        signals.append("Unusually short HTTP response body")
    return signals


def clean(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def add_flags(flags, flag_type, url, message):
    flags.append({
        "Timestamp": datetime.now().isoformat(timespec="seconds"),
        "Type": flag_type,
        "URL": url or "",
        "Message": message,
    })


def fetch(session, url, flags):
    log(f"Fetching {url}")
    try:
        response = session.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            timeout=30,
            proxies=get_proxy(),
        )
        for signal in detect_anti_bot_signals(response):
            add_flags(flags, "Anti-bot signal", url, signal)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        add_flags(flags, "Error", url, f"HTTP request failed: {exc}")
        log(f"Request failed: {url}: {exc}")
        return None


def soup_for(response):
    return BeautifulSoup(response.text, "html.parser")


def path_parts(url):
    return [part for part in urlparse(url).path.strip("/").split("/") if part]


def is_alabama_city_url(url):
    parts = path_parts(url)
    return len(parts) == 3 and parts[:2] == ["united-states", "al"]


def is_location_url(url):
    parts = path_parts(url)
    return len(parts) >= 4 and parts[:2] == ["united-states", "al"]


def unique_urls(urls):
    seen = set()
    result = []
    for url in urls:
        normalized = url.split("#", 1)[0].rstrip("/")
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def city_links(soup, base_url, keyword):
    matches = []
    needle = clean(keyword).lower()
    for anchor in soup.select("a[href]"):
        href = urljoin(base_url, anchor.get("href", ""))
        if not is_alabama_city_url(href):
            continue
        label = clean(anchor.get_text(" ", strip=True)).lower()
        slug = path_parts(href)[-1].replace("-", " ").lower()
        if needle and (needle in label or needle in slug):
            matches.append(href)
    return unique_urls(matches)


def location_links(soup, base_url):
    matches = []
    for anchor in soup.select("a[href]"):
        href = urljoin(base_url, anchor.get("href", ""))
        if is_location_url(href):
            matches.append(href)
    return unique_urls(matches)


def text_lines(soup):
    return [clean(line) for line in soup.get_text("\n", strip=True).splitlines() if clean(line)]


def label_value(lines, labels):
    for index, line in enumerate(lines):
        lowered = line.lower()
        for label in labels:
            if lowered.startswith(label.lower()):
                value = line[len(label):].lstrip(" :#-")
                if value:
                    return clean(value)
                if index + 1 < len(lines):
                    return clean(lines[index + 1])
    return ""


def extract_hours(lines):
    day_pattern = re.compile(
        r"(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)",
        re.IGNORECASE,
    )
    hour_lines = []
    for line in lines:
        if day_pattern.search(line) and re.search(r"\d{1,2}:\d{2}", line):
            hour_lines.append(line)
    return "; ".join(unique_urls(hour_lines)) if hour_lines else ""


def availability(text, terms):
    lowered = text.lower()
    for term in terms:
        pattern = re.compile(
            rf"{re.escape(term)}\s*[:\-]?\s*(yes|no|available|unavailable|not available)",
            re.IGNORECASE,
        )
        match = pattern.search(lowered)
        if match:
            value = match.group(1).lower()
            if value in {"yes", "available"}:
                return "Yes"
            return "No"
    return ""


def extract_item(soup, url, flags):
    lines = text_lines(soup)
    full_text = "\n".join(lines)
    lowered = full_text.lower()
    status_terms = ["temporarily closed", "closed", "temporary kiosk", "kiosk"]
    if any(term in lowered for term in status_terms):
        add_flags(flags, "Filtered location", url, "Location appears closed or to be a temporary kiosk")
        return None

    address_match = re.search(
        r"\b(\d+[^\n,]*?)\s+([A-Za-z .'-]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b",
        full_text,
    )
    street = label_value(lines, ["Street Address", "Address"])
    city = label_value(lines, ["City"])
    state = label_value(lines, ["State"])
    postal = label_value(lines, ["Postal Code", "Zip Code", "ZIP"])
    if address_match:
        street = street or clean(address_match.group(1))
        city = city or clean(address_match.group(2))
        state = state or address_match.group(3)
        postal = postal or address_match.group(4)

    store_number = label_value(lines, ["Store Number", "Store #", "Store No."])
    phone = label_value(lines, ["Phone", "Phone Number", "Telephone"])
    if not phone:
        phone_match = re.search(r"\(?[2-9]\d{2}\)?[\s.-]+\d{3}[\s.-]+\d{4}", full_text)
        phone = phone_match.group(0) if phone_match else ""

    name = ""
    for line in lines[:8]:
        if "subway" in line.lower() and len(line) > len("subway"):
            name = line
            break

    item = {
        "Store Number": store_number,
        "Street Address": street,
        "City": city,
        "State": state,
        "Postal Code": postal,
        "Phone Number": phone,
        "Operating Hours": extract_hours(lines),
        "Drive-Thru Available": availability(full_text, ["drive-thru", "drive thru", "drive through"]),
        "Delivery Available": availability(full_text, ["delivery"]),
        "Detail URL": url,
    }
    missing = [field for field in DATA_COLUMNS if field != "Detail URL" and not item[field]]
    if missing:
        add_flags(flags, "Low-confidence extraction", url, "Fields not exposed or not confidently detected: " + ", ".join(missing))
    if not name:
        add_flags(flags, "Low-confidence extraction", url, "Store heading/name was not confidently detected")
    return item


def collect_from_listing(session, listing_url, flags, detail_urls):
    response = fetch(session, listing_url, flags)
    if not response:
        return
    soup = soup_for(response)
    found = location_links(soup, listing_url)
    if not found:
        add_flags(flags, "Low-confidence extraction", listing_url, "No individual location links detected; exact production selector is uncertain")
    detail_urls.extend(found)


def main():
    parser = argparse.ArgumentParser(description="Scrape Subway Alabama locations")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    args = parser.parse_args()

    log("Starting scraper")
    flags = []
    records = []
    session = requests.Session()
    try:
        input_df = pd.read_excel(args.input)
    except Exception as exc:
        log(f"Unable to read input workbook: {exc}")
        add_flags(flags, "Error", args.input, f"Unable to read input workbook: {exc}")
        input_df = pd.DataFrame(columns=["keyword", "listing_url", "detail_url"])

    detail_urls = []
    for _, row in input_df.iterrows():
        detail_url = clean(row.get("detail_url", ""))
        listing_url = clean(row.get("listing_url", ""))
        keyword = clean(row.get("keyword", ""))
        if detail_url:
            log(f"Using independent detail URL fast path: {detail_url}")
            detail_urls.append(detail_url)
        elif listing_url:
            collect_from_listing(session, listing_url, flags, detail_urls)
        elif keyword:
            log(f"Searching Alabama city links for keyword: {keyword}")
            response = fetch(session, BASE_URL, flags)
            if response:
                matches = city_links(soup_for(response), BASE_URL, keyword)
                if not matches:
                    add_flags(flags, "Low-confidence extraction", BASE_URL, f"No city link matched keyword {keyword!r}")
                for city_url in matches:
                    collect_from_listing(session, city_url, flags, detail_urls)
        else:
            add_flags(flags, "Error", "", "Input row has no keyword, listing_url, or detail_url")

    detail_urls = unique_urls(detail_urls)
    if args.max_products is not None:
        detail_urls = detail_urls[:max(0, args.max_products)]
    log(f"Extracting {len(detail_urls)} detail page(s)")
    for url in detail_urls:
        response = fetch(session, url, flags)
        if not response:
            continue
        item = extract_item(soup_for(response), url, flags)
        if item:
            records.append(item)

    output_df = pd.DataFrame(records, columns=DATA_COLUMNS)
    flags_df = pd.DataFrame(flags, columns=FLAG_COLUMNS)
    log(f"Writing {len(output_df)} extracted row(s) and {len(flags_df)} flag(s)")
    try:
        with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
            output_df.to_excel(writer, sheet_name="Extracted Data", index=False)
            flags_df.to_excel(writer, sheet_name="Flags", index=False)
    except Exception as exc:
        log(f"Unable to write output workbook: {exc}")
        raise
    log("Scraper finished")


if __name__ == "__main__":
    main()