import argparse
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://restaurants.subway.com"
STATE_URL = "https://restaurants.subway.com/united-states/al"

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
    "Source URL",
]
FLAG_COLUMNS = ["timestamp", "flag_type", "message", "url", "keyword"]


def log(message):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def get_proxy():
    return None


def solve_captcha(*args, **kwargs):
    raise NotImplementedError("CAPTCHA solving is intentionally not implemented")


def detect_anti_bot_signals(response):
    """Return signals only; this function never attempts to bypass protection."""
    signals = []
    body = response.text or ""
    lower = body.lower()
    if response.status_code in (403, 429):
        signals.append(f"HTTP status {response.status_code}")
    markers = {
        "captcha": "CAPTCHA marker",
        "cloudflare": "Cloudflare marker",
        "perimeterx": "PerimeterX marker",
        "datadome": "DataDome marker",
    }
    for marker, description in markers.items():
        if marker in lower:
            signals.append(description)
    if len(body.strip()) < 500:
        signals.append(f"unusually short response body ({len(body.strip())} characters)")
    return signals


def clean_text(value):
    return re.sub(r"\\s+", " ", value or "").strip()


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; ScraperStudio/1.0)",
        "Accept-Language": "en-US,en;q=0.9",
    })
    proxy = get_proxy()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def fetch(session, url, flags, keyword=""):
    log(f"Fetching {url}")
    try:
        response = session.get(url, timeout=30)
        for signal in detect_anti_bot_signals(response):
            flags.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "flag_type": "anti-bot",
                "message": signal,
                "url": url,
                "keyword": keyword,
            })
        response.raise_for_status()
        return response
    except Exception as exc:
        flags.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "flag_type": "error",
            "message": f"Request failed: {exc}",
            "url": url,
            "keyword": keyword,
        })
        log(f"Request failed for {url}: {exc}")
        return None


def soup_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor.get("href"))
        parsed = urlparse(href)
        if parsed.netloc and parsed.netloc != urlparse(BASE_URL).netloc:
            continue
        links.append((clean_text(anchor.get_text(" ", strip=True)), href.split("#", 1)[0]))
    return soup, links


def path_parts(url):
    return [part for part in urlparse(url).path.split("/") if part]


def city_links(html, page_url, keyword):
    _, links = soup_links(html, page_url)
    wanted = (keyword or "").casefold()
    result = []
    seen = set()
    for text, href in links:
        parts = path_parts(href)
        if len(parts) == 3 and parts[:2] == ["united-states", "al"]:
            candidate = f"{text} {parts[2]}".casefold()
            if wanted in candidate and href not in seen:
                seen.add(href)
                result.append(href)
    return result


def detail_links(html, page_url):
    _, links = soup_links(html, page_url)
    result = []
    seen = set()
    for _, href in links:
        parts = path_parts(href)
        if (len(parts) >= 4 and parts[:2] == ["united-states", "al"]
                and href.rstrip("/") != page_url.rstrip("/") and href not in seen):
            seen.add(href)
            result.append(href)
    return result


def parse_hours(text):
    patterns = [
        r"(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)[^.;|]{0,80}?\d{1,2}:\d{2}\s*[AP]M[^.;|]{0,20}(?:\d{1,2}:\d{2}\s*[AP]M|Closed)",
    ]
    found = []
    for pattern in patterns:
        found.extend(re.findall(pattern, text, flags=re.I))
    unique = []
    for item in found:
        item = clean_text(item)
        if item not in unique:
            unique.append(item)
    return "; ".join(unique)


def extract_detail(html, url, flags, keyword):
    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(soup.get_text(" ", strip=True))
    lower = text.casefold()
    if "temporary kiosk" in lower or "temporarily closed" in lower or re.search(r"\bclosed\b", lower):
        flags.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "flag_type": "filtered",
            "message": "Location appears closed or temporary; excluded per rules",
            "url": url,
            "keyword": keyword,
        })
        return None

    row = {column: "" for column in DATA_COLUMNS}
    row["Source URL"] = url
    number_match = re.search(r"(?:store\s*(?:number|no\\.?|#)|location\s*(?:number|no\\.?|#))\s*[:#]?\s*([A-Z0-9-]+)", text, re.I)
    if number_match:
        row["Store Number"] = number_match.group(1)

    address_match = re.search(r"(\d+\s+[^,;|]+?),\s*([A-Za-z .'-]+?),\s*(AL)\s+(\d{5}(?:-\d{4})?)", text, re.I)
    if not address_match:
        address_match = re.search(r"(\d+\s+[^;|]+?)\s+([A-Za-z .'-]+),\s*(AL)\s+(\d{5}(?:-\d{4})?)", text, re.I)
    if address_match:
        row["Street Address"] = clean_text(address_match.group(1))
        row["City"] = clean_text(address_match.group(2))
        row["State"] = address_match.group(3).upper()
        row["Postal Code"] = address_match.group(4)

    phone_match = re.search(r"(?:phone|tel(?:ephone)?)?\s*[:.]?\s*(\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4})", text, re.I)
    if phone_match:
        row["Phone Number"] = phone_match.group(1)
    row["Operating Hours"] = parse_hours(text)

    drive_match = re.search(r"drive[- ]?thru[^.]{0,40}", text, re.I)
    delivery_match = re.search(r"delivery[^.]{0,40}", text, re.I)
    if drive_match:
        row["Drive-Thru Available"] = drive_match.group(0)
    if delivery_match:
        row["Delivery Available"] = delivery_match.group(0)

    uncertain = [name for name in DATA_COLUMNS[:-1] if not row[name]]
    if uncertain:
        flags.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "flag_type": "low-confidence",
            "message": "Fields not confidently found: " + ", ".join(uncertain),
            "url": url,
            "keyword": keyword,
        })
    return row


def scrape_detail(session, url, flags, keyword):
    response = fetch(session, url, flags, keyword)
    if response is None:
        return None
    return extract_detail(response.text, url, flags, keyword)


def crawl_listing(session, listing_url, flags, keyword, max_products, rows):
    response = fetch(session, listing_url, flags, keyword)
    if response is None:
        return
    links = detail_links(response.text, listing_url)
    if not links:
        flags.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "flag_type": "low-confidence",
            "message": "No individual location links found; exact DOM selectors were not available in inspection",
            "url": listing_url,
            "keyword": keyword,
        })
    for url in links:
        if max_products is not None and len(rows) >= max_products:
            return
        item = scrape_detail(session, url, flags, keyword)
        if item:
            rows.append(item)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    args = parser.parse_args()

    log("Reading input workbook")
    input_df = pd.read_excel(args.input)
    session = make_session()
    rows, flags = [], []

    for _, record in input_df.iterrows():
        if args.max_products is not None and len(rows) >= args.max_products:
            break
        keyword = "" if pd.isna(record.get("keyword", "")) else str(record.get("keyword", "")).strip()
        detail_url = "" if pd.isna(record.get("detail_url", "")) else str(record.get("detail_url", "")).strip()
        listing_url = "" if pd.isna(record.get("listing_url", "")) else str(record.get("listing_url", "")).strip()

        if detail_url:
            log(f"Using independent detail fast path: {detail_url}")
            item = scrape_detail(session, detail_url, flags, keyword)
            if item:
                rows.append(item)
            continue

        if listing_url:
            log(f"Crawling supplied listing URL: {listing_url}")
            crawl_listing(session, listing_url, flags, keyword, args.max_products, rows)
            continue

        if keyword:
            log(f"Matching city links for keyword: {keyword}")
            response = fetch(session, STATE_URL, flags, keyword)
            if response is not None:
                cities = city_links(response.text, STATE_URL, keyword)
                if not cities:
                    flags.append({
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "flag_type": "low-confidence",
                        "message": "No matching Alabama city link found; search behavior was not observable in inspection",
                        "url": STATE_URL,
                        "keyword": keyword,
                    })
                for city_url in cities:
                    if args.max_products is not None and len(rows) >= args.max_products:
                        break
                    crawl_listing(session, city_url, flags, keyword, args.max_products, rows)

    log("Writing output workbook")
    output_df = pd.DataFrame(rows, columns=DATA_COLUMNS)
    flags_df = pd.DataFrame(flags, columns=FLAG_COLUMNS)
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        output_df.to_excel(writer, sheet_name="Extracted Data", index=False)
        flags_df.to_excel(writer, sheet_name="Flags", index=False)
    log(f"Finished: {len(rows)} extracted item(s), {len(flags)} flag(s)")


if __name__ == "__main__":
    main()
