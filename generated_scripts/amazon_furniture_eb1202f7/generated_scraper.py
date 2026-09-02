import argparse
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


TARGET = "https://www.amazon.com/"


def log(message):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def get_proxy():
    """Proxy configuration is intentionally not implemented."""
    return None


def solve_captcha(*args, **kwargs):
    """CAPTCHA solving and anti-bot bypass are intentionally not implemented."""
    raise NotImplementedError("CAPTCHA solving is not implemented")


def detect_anti_bot_signals(response, body=None):
    """Return anti-bot signal descriptions for one HTTP response."""
    signals = []
    status = getattr(response, "status", None)
    url = getattr(response, "url", "")
    if status in (403, 429):
        signals.append(f"HTTP status {status}")

    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type", "")).lower()
    text = (body or "")[:200000].lower()
    markers = {
        "captcha": "CAPTCHA marker",
        "cloudflare": "Cloudflare marker",
        "perimeterx": "PerimeterX marker",
        "datadome": "DataDome marker",
    }
    for marker, description in markers.items():
        if marker in text:
            signals.append(description)

    if body is not None and ("text/html" in content_type or not content_type):
        if len(body.strip()) < 200:
            signals.append("Unusually short HTML response body")
    return signals


def dismiss_cookie_banner(page, flags, page_url):
    """Try common consent controls; absence is recorded as a possible layout change."""
    consent_texts = ["Accept all", "Accept", "I agree", "Allow all", "Agree"]
    matched = False
    attempted = True
    try:
        for text in consent_texts:
            locator = page.get_by_role("button", name=re.compile(rf"^{re.escape(text)}$", re.I)).first
            if locator.count() and locator.is_visible(timeout=700):
                locator.click(timeout=700)
                matched = True
                log(f"Dismissed a consent control on {page_url}")
                break
    except Exception:
        pass
    if attempted and not matched:
        log(f"No consent control matched on {page_url}; possible layout change")
        flags.append({
            "Type": "low-confidence extraction",
            "URL": page_url,
            "Message": "No common cookie-consent button matched; possible layout change",
        })


def navigate(page, url, flags):
    """Navigate and immediately perform the required consent check."""
    log(f"Navigating to {url}")
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # This must remain immediately after every page.goto call.
        dismiss_cookie_banner(page, flags, url)
        if response is not None:
            try:
                body = response.text()
            except Exception:
                body = None
            for signal in detect_anti_bot_signals(response, body):
                flags.append({"Type": "anti-bot signal", "URL": url, "Message": signal})
        return True
    except PlaywrightTimeoutError as exc:
        flags.append({"Type": "error", "URL": url, "Message": f"Navigation timeout: {exc}"})
        return False
    except Exception as exc:
        flags.append({"Type": "error", "URL": url, "Message": f"Navigation failed: {exc}"})
        return False


def first_text(page, selectors):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible(timeout=500):
                value = locator.inner_text(timeout=1000).strip()
                if value:
                    return value
        except Exception:
            continue
    return ""


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def extract_detail(page, detail_url, listing_url, keyword, flags):
    current_url = page.url or detail_url
    title = first_text(page, ["#productTitle", "h1"])
    asin = ""
    try:
        asin = page.locator("#detailBullets_feature_div li, #prodDetails tr").all_inner_texts()
        asin = " ".join(asin)
    except Exception:
        asin = ""
    asin_match = re.search(r"\bB0[A-Z0-9]{8}\b|\b[0-9A-Z]{10}\b", asin, re.I)
    asin = asin_match.group(0) if asin_match else ""
    if not asin:
        asin_match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", current_url, re.I)
        asin = asin_match.group(1) if asin_match else ""

    category = first_text(page, ["#wayfinding-breadcrumbs_feature_div", "nav[aria-label='Breadcrumb']"])
    cost = first_text(page, ["#corePriceDisplay_desktop_feature_div .a-price", "#priceblock_ourprice", "#priceblock_dealprice", ".a-price.aok-align-center"])
    msrp = first_text(page, [".basisPrice", "#priceblock_listprice", "span.a-text-price"])
    ratings = first_text(page, ["#acrPopover", "#averageCustomerReviews .a-icon-alt"])
    reviews = first_text(page, ["#acrCustomerReviewLink", "#averageCustomerReviews"])
    badge = first_text(page, ["#dealBadge_feature_div", "#acBadge_feature_div", ".badge-wrapper"])

    if not title:
        flags.append({"Type": "low-confidence extraction", "URL": current_url, "Message": "Product title selector returned no value"})
    if not asin:
        flags.append({"Type": "low-confidence extraction", "URL": current_url, "Message": "ASIN could not be identified"})

    return {
        "Category": clean_text(category),
        "Retailers": "Amazon",
        "List Page URL": listing_url,
        "Product Title": clean_text(title),
        "asin": asin,
        "Product Cost": clean_text(cost),
        "MSRP": clean_text(msrp),
        "Ratings": clean_text(ratings),
        "Reviews": clean_text(reviews),
        "Item Badge": clean_text(badge),
        "Product URL": current_url,
    }


def collect_listing_links(page, listing_url, keyword, flags):
    links = []
    cards = page.locator("[data-component-type='s-search-result']")
    try:
        count = cards.count()
    except Exception:
        count = 0
    for index in range(count):
        card = cards.nth(index)
        try:
            title = first_text(card, ["h2", "h2 a"])
            anchor = card.locator("h2 a, a.a-link-normal").first
            href = anchor.get_attribute("href") if anchor.count() else None
            if not href:
                continue
            absolute = urljoin(listing_url, href)
            haystack = f"{title} {absolute}".lower()
            if keyword and keyword.lower() not in haystack:
                continue
            links.append(absolute.split("?")[0])
        except Exception:
            continue

    if not links:
        flags.append({"Type": "low-confidence extraction", "URL": listing_url, "Message": "No verified Amazon listing selectors or product links were found"})
    return list(dict.fromkeys(links))


def handle_row(row, page, results, flags, max_products, processed):
    keyword = clean_text(str(row.get("keyword", "")) if pd.notna(row.get("keyword", "")) else "")
    listing_url = clean_text(str(row.get("listing_url", "")) if pd.notna(row.get("listing_url", "")) else "")
    detail_url = clean_text(str(row.get("detail_url", "")) if pd.notna(row.get("detail_url", "")) else "")

    # Independent detail_url fast path: it never depends on a listing crawl.
    if detail_url:
        if navigate(page, detail_url, flags):
            results.append(extract_detail(page, detail_url, listing_url, keyword, flags))
            processed[0] += 1
        return

    if not listing_url:
        if keyword:
            listing_url = f"{TARGET}s?k={quote_plus(keyword)}"
        else:
            flags.append({"Type": "error", "URL": "", "Message": "Row has neither detail_url, listing_url, nor keyword"})
            return

    if not navigate(page, listing_url, flags):
        return
    links = collect_listing_links(page, listing_url, keyword, flags)
    for product_url in links:
        if max_products is not None and processed[0] >= max_products:
            break
        if navigate(page, product_url, flags):
            results.append(extract_detail(page, product_url, listing_url, keyword, flags))
            processed[0] += 1


def main():
    parser = argparse.ArgumentParser(description="Amazon scraper")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    args = parser.parse_args()

    log(f"Reading input workbook {args.input}")
    try:
        input_df = pd.read_excel(args.input)
    except Exception as exc:
        log(f"Could not read input workbook: {exc}")
        raise

    results = []
    flags = []
    processed = [0]
    with sync_playwright() as playwright:
        log("Launching Chromium browser for JavaScript-rendered pages")
        browser = playwright.chromium.launch(headless=True, proxy=get_proxy())
        context = browser.new_context(user_agent="Mozilla/5.0 (compatible; ScraperStudio/1.0)")
        page = context.new_page()

        def response_handler(response):
            try:
                content_type = str(response.headers.get("content-type", "")).lower()
                body = response.text() if ("text/html" in content_type or not content_type) else None
                for signal in detect_anti_bot_signals(response, body):
                    flags.append({"Type": "anti-bot signal", "URL": response.url, "Message": signal})
            except Exception:
                pass

        page.on("response", response_handler)
        for _, row in input_df.iterrows():
            if args.max_products is not None and processed[0] >= args.max_products:
                break
            handle_row(row, page, results, flags, args.max_products, processed)
        browser.close()

    log(f"Writing {len(results)} extracted items and {len(flags)} flags to {args.output}")
    columns = ["Category", "Retailers", "List Page URL", "Product Title", "asin", "Product Cost", "MSRP", "Ratings", "Reviews", "Item Badge", "Product URL"]
    flags_columns = ["Type", "URL", "Message"]
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        pd.DataFrame(results, columns=columns).to_excel(writer, sheet_name="Extracted Data", index=False)
        pd.DataFrame(flags, columns=flags_columns).to_excel(writer, sheet_name="Flags", index=False)
    log("Run complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"Fatal error: {exc}")
        sys.exit(1)
