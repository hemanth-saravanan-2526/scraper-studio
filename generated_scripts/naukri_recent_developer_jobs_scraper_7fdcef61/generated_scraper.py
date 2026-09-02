import argparse
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://www.naukri.com/developer-jobs?k=developer&jobAge=1"
OUTPUT_COLUMNS = [
    "Job Title",
    "Company Name",
    "Experience Required",
    "Salary Range",
    "Job Location",
    "Key Skills",
    "Posted Date",
    "Job Detail URL",
]


def log(message):
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    print(f"[{timestamp}] {message}", flush=True)


def clean(value):
    if value is None or pd.isna(value) or str(value).strip().lower() in ("", "nan", "none", "null"):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).replace("\xa0", " ").strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def value_from_row(row, column):
    if column not in row:
        return ""
    return clean(row[column])


def get_proxy():
    return None


def solve_captcha(*args, **kwargs):
    raise NotImplementedError(" CAPTCHA solving and anti-bot bypass are not implemented.")


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
        signals.append(f"Unusually short response body ({len(body.strip())} characters)")
    return signals


def add_flag(flags, kind, message, url=""):
    flags.append({
        "Timestamp": datetime.now(timezone.utc).isoformat(),
        "Type": clean(kind),
        "Message": clean(message),
        "URL": clean(url),
    })


def request_page(session, url, flags):
    log(f"Requesting {url}")
    try:
        response = session.get(url, timeout=30, allow_redirects=True)
    except requests.RequestException as exc:
        add_flag(flags, "error", f"Request failed: {exc}", url)
        return None
    for signal in detect_anti_bot_signals(response):
        add_flag(flags, "anti-bot", signal, response.url or url)
    if response.status_code >= 400:
        add_flag(flags, "error", f"HTTP status {response.status_code}", response.url or url)
        return None
    return response


def text_of(node):
    if node is None:
        return ""
    return clean(node.get_text("\n", strip=True))


def first_text(soup, selectors):
    for selector in selectors:
        node = soup.select_one(selector)
        value = text_of(node)
        if value:
            return value
    return ""


def labeled_value(text, labels):
    if not text:
        return ""
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{label_pattern})\s*[:\-]?\s*([^\n|]+)",
        text,
        flags=re.IGNORECASE,
    )
    return clean(match.group(1)) if match else ""


def normalize_url(url, base_url):
    absolute = urljoin(base_url, clean(url))
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def extract_detail(response, flags):
    soup = BeautifulSoup(response.text, "html.parser")
    page_text = text_of(soup)
    title = first_text(soup, [
        "h1",
        "[class*='jobTitle']",
        "[class*='job-title']",
        "meta[property='og:title']",
    ])
    if not title:
        meta = soup.select_one("meta[property='og:title']")
        title = clean(meta.get("content", "")) if meta else ""
    company = first_text(soup, [
        "[class*='companyName']",
        "[class*='company-name']",
        "[class*='companyInfo']",
        "[class*='company-info']",
    ]) or labeled_value(page_text, ["Company", "Company Name", "Employer"])
    experience = first_text(soup, [
        "[class*='experience']",
        "[class*='exp']",
    ]) or labeled_value(page_text, ["Experience", "Experience Required", "Exp"])
    salary = first_text(soup, [
        "[class*='salary']",
        "[class*='Salary']",
    ]) or labeled_value(page_text, ["Salary", "Salary Range", "CTC"])
    location = first_text(soup, [
        "[class*='location']",
        "[class*='Location']",
    ]) or labeled_value(page_text, ["Location", "Job Location"])
    skills = first_text(soup, [
        "[class*='skills']",
        "[class*='Skills']",
        "[class*='key-skill']",
    ]) or labeled_value(page_text, ["Key Skills", "Skills", "Key skill"])
    posted = first_text(soup, [
        "[class*='posted']",
        "[class*='datePosted']",
        "time",
    ]) or labeled_value(page_text, ["Posted", "Posted Date", "Date Posted"])
    if not title:
        add_flag(flags, "low-confidence", "Could not identify a job title with the fallback selectors.", response.url)
    if not company:
        add_flag(flags, "low-confidence", "Company extraction was not verified by the inspection findings.", response.url)
    if not any((experience, salary, location, skills, posted)):
        add_flag(flags, "low-confidence", "No labeled job fields were found; live detail selectors were not inspected.", response.url)
    return {
        "Job Title": title,
        "Company Name": company,
        "Experience Required": experience,
        "Salary Range": salary,
        "Job Location": location,
        "Key Skills": skills,
        "Posted Date": posted,
        "Job Detail URL": response.url,
        "_page_text": page_text,
    }


def is_sponsored_course(item):
    text = clean(" ".join(str(item.get(key, "")) for key in OUTPUT_COLUMNS)).lower()
    return bool(re.search(r"\b(course|courses|training|learn|certification|academy)\b", text))


def appears_full_time(item):
    text = clean(item.get("_page_text", "")).lower()
    if re.search(r"\b(part[- ]time|contract|internship|freelance|temporary)\b", text):
        return False
    return True


def listing_links(soup, base_url):
    links = []
    for anchor in soup.select("a[href]"):
        href = normalize_url(anchor.get("href", ""), base_url)
        label = clean(anchor.get_text(" ", strip=True)).lower()
        if not href:
            continue
        if (re.search(r"(?:page[-_=]?\d+|[?&]page=\d+)", href, re.IGNORECASE)
                or label in {"next", ">", "›", "next page"}):
            links.append(href)
    return list(dict.fromkeys(links))


def extract_listing_items(response, keyword, flags):
    soup = BeautifulSoup(response.text, "html.parser")
    anchors = []
    for anchor in soup.select("a[href]"):
        href = normalize_url(anchor.get("href", ""), response.url)
        if href and ("job" in href.lower() or "naukri" in href.lower()):
            anchors.append((anchor, href))
    if not anchors:
        add_flag(flags, "low-confidence", "No listing elements were found; the rendered page may require JavaScript or access may be limited.", response.url)
    items = []
    seen = set()
    keyword_pattern = re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE) if keyword else None
    for anchor, href in anchors:
        container = anchor
        for _ in range(4):
            if container.parent is None:
                break
            container = container.parent
            candidate_text = text_of(container)
            if len(candidate_text) >= 40:
                break
        title = clean(anchor.get_text(" ", strip=True))
        block_text = text_of(container)
        if len(title) < 3:
            title = first_text(container, ["h1", "h2", "h3", "h4", "[class*='title']"])
        if not title or is_sponsored_course({"_page_text": block_text, "Job Title": title}):
            continue
        if keyword_pattern and not keyword_pattern.search(f"{title} {block_text}"):
            continue
        key = href.split("?", 1)[0].lower()
        if key in seen:
            continue
        seen.add(key)
        items.append({"Job Title": title, "Job Detail URL": href, "_listing_text": block_text})
    return items


def with_keyword(url, keyword):
    if not keyword:
        return url
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["k"] = keyword
    query.setdefault("jobAge", "1")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))


def crawl_listing(session, listing_url, keyword, max_products, flags):
    queue = [with_keyword(listing_url, keyword)]
    visited_pages = set()
    records = []
    seen_jobs = set()
    while queue and len(records) < max_products:
        page_url = queue.pop(0)
        if page_url in visited_pages:
            continue
        visited_pages.add(page_url)
        response = request_page(session, page_url, flags)
        if response is None:
            continue
        log(f"Parsing listing page {page_url}")
        candidates = extract_listing_items(response, keyword, flags)
        for candidate in candidates:
            detail_url = candidate["Job Detail URL"]
            if detail_url in seen_jobs:
                continue
            seen_jobs.add(detail_url)
            detail_response = request_page(session, detail_url, flags)
            if detail_response is None:
                continue
            item = extract_detail(detail_response, flags)
            if not item["Job Title"]:
                item["Job Title"] = candidate["Job Title"]
            if not appears_full_time(item):
                continue
            item.pop("_page_text", None)
            item.pop("_listing_text", None)
            records.append(item)
            if len(records) >= max_products:
                break
        for next_url in listing_links(BeautifulSoup(response.text, "html.parser"), response.url):
            if next_url not in visited_pages and next_url not in queue:
                queue.append(next_url)
    return records


def main():
    parser = argparse.ArgumentParser(description="Naukri developer job scraper")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    args = parser.parse_args()

    log("Starting scraper")
    input_df = pd.read_excel(args.input).fillna("")
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; ScraperStudio/1.0)",
        "Accept-Language": "en-US,en;q=0.9",
    })
    records = []
    flags = []
    cap = args.max_products if args.max_products and args.max_products > 0 else 10**9

    for row_number, row in input_df.iterrows():
        keyword = value_from_row(row, "keyword")
        listing_url = value_from_row(row, "listing_url")
        detail_url = value_from_row(row, "detail_url")
        if detail_url:
            log(f"Processing independent detail URL from input row {row_number + 2}")
            response = request_page(session, detail_url, flags)
            if response is not None:
                item = extract_detail(response, flags)
                item.pop("_page_text", None)
                if not is_sponsored_course(item) and appears_full_time({"_page_text": "", **item}):
                    records.append({key: item.get(key, "") for key in OUTPUT_COLUMNS})
            continue
        if listing_url or keyword:
            target = listing_url or TARGET_URL
            log(f"Processing listing flow from input row {row_number + 2}")
            records.extend(crawl_listing(session, target, keyword, max(0, cap - len(records)), flags))
        else:
            add_flag(flags, "error", "Input row has no usable keyword, listing_url, or detail_url.")
        if len(records) >= cap:
            break

    records = records[:cap]
    log(f"Writing {len(records)} extracted records and {len(flags)} flags")
    output_df = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    flags_df = pd.DataFrame(flags, columns=["Timestamp", "Type", "Message", "URL"])
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        output_df.to_excel(writer, sheet_name="Extracted Data", index=False)
        flags_df.to_excel(writer, sheet_name="Flags", index=False)
    log("Scraper finished")


if __name__ == "__main__":
    main()