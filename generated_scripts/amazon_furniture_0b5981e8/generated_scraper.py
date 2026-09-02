import argparse
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote, urljoin

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


OUTPUT_COLUMNS = [
    'Product Title', 'Model Code', 'Price (INR)', 'Color options',
    'Storage capacity', 'Display specs', 'Camera specs',
    'Availability status', 'Source URL'
]
FLAG_COLUMNS = ['Timestamp', 'URL', 'Flag type', 'Message']


def log(message):
    print(f'[{datetime.now().astimezone().isoformat(timespec="seconds")}] {message}', flush=True)


def get_proxy():
    return None


def solve_captcha(*args, **kwargs):
    raise NotImplementedError('CAPTCHA solving and anti-bot bypass are not implemented')


def detect_anti_bot_signals(response, body=''):
    signals = []
    status = getattr(response, 'status', response if isinstance(response, int) else None)
    url = getattr(response, 'url', '') if not isinstance(response, int) else ''
    text = body or ''
    lowered = text.lower()
    if status in (403, 429):
        signals.append(f'HTTP status {status}')
    markers = {
        'captcha': 'CAPTCHA marker',
        'cloudflare': 'Cloudflare marker',
        'perimeterx': 'PerimeterX marker',
        'datadome': 'DataDome marker',
        'javascript is disabled': 'JavaScript-disabled response marker',
        'enable javascript': 'JavaScript-required response marker'
    }
    for marker, description in markers.items():
        if marker in lowered:
            signals.append(description)
    if text and len(text.strip()) < 500:
        signals.append(f'Unusually short response body ({len(text.strip())} characters)')
    return signals


def add_flag(flags, url, flag_type, message):
    flags.append({
        'Timestamp': datetime.now(timezone.utc).isoformat(),
        'URL': url or '',
        'Flag type': flag_type,
        'Message': message
    })


def dismiss_cookie_banner(page, flags, url):
    attempted = True
    matches = ['Accept all', 'Accept', 'I agree', 'Allow all', 'Agree']
    try:
        for text in matches:
            locator = page.get_by_role('button', name=re.compile(rf'^\\s*{re.escape(text)}\\s*$', re.I)).first
            if locator.count() and locator.is_visible(timeout=400):
                locator.click(timeout=700)
                log(f'Cookie consent dismissed on {url}')
                return True
    except Exception:
        pass
    if attempted:
        log(f'Cookie dismissal was attempted but no matching button was found on {url}')
        add_flag(flags, url, 'possible layout change', 'Cookie dismissal attempted but no common consent button matched')
    return False


def safe_goto(page, url, flags):
    log(f'Opening {url}')
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=30000)
        # This must remain the first operation after every successful page.goto.
        dismiss_cookie_banner(page, flags, url)
        return True
    except Exception as exc:
        add_flag(flags, url, 'error', f'Navigation failed: {exc}')
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
    return ''


def page_text(page):
    try:
        return page.locator('body').inner_text(timeout=3000)
    except Exception:
        return ''


def labeled_value(text, labels):
    label_pattern = '|'.join(re.escape(label) for label in labels)
    match = re.search(rf'(?:{label_pattern})\\s*[:\\-]?\\s*([^\\n|;]+)', text, re.I)
    return match.group(1).strip() if match else ''


def extract_detail(page, url, flags):
    text = page_text(page)
    title = first_text(page, [
        '#productTitle', 'h1 span', 'h1', '[data-feature-name="title"]'
    ])
    if not title:
        title = first_text(page, ['title'])
    price = first_text(page, [
        '.a-price .a-offscreen', '#priceblock_ourprice', '#priceblock_dealprice',
        '.a-price-whole'
    ])
    if price and not re.search(r'(?:₹|INR)', price, re.I):
        price = ''
    model = labeled_value(text, ['model number', 'model code', 'item model number', 'model'])
    if not model:
        match = re.search(r'\\b[A-Z0-9][A-Z0-9._-]{2,}\\b', text)
        model = match.group(0) if match else ''
    color = labeled_value(text, ['color', 'colour'])
    storage = labeled_value(text, ['storage', '容量', 'memory storage', 'hard disk size'])
    display = labeled_value(text, ['display', 'screen size', 'display size', 'resolution'])
    camera = labeled_value(text, ['camera', 'rear camera', 'front camera'])
    availability = first_text(page, [
        '#availability', '#outOfStock', '#buybox', '.a-color-success', '.a-color-price'
    ])
    if not availability:
        availability = labeled_value(text, ['availability', 'in stock', 'currently unavailable'])
    if not availability:
        availability = 'Unknown'

    low_confidence = []
    if not title:
        low_confidence.append('product title was not found')
    for field, value in [('Model Code', model), ('Price (INR)', price), ('Color options', color),
                         ('Storage capacity', storage), ('Display specs', display),
                         ('Camera specs', camera)]:
        if not value:
            low_confidence.append(f'{field} was not found')
    if low_confidence:
        add_flag(flags, url, 'low-confidence extraction', '; '.join(low_confidence))
    if not text or 'javascript is disabled' in text.lower():
        add_flag(flags, url, 'blocked or empty page', 'No usable rendered product content was available')

    return {
        'Product Title': title,
        'Model Code': model,
        'Price (INR)': price,
        'Color options': color,
        'Storage capacity': storage,
        'Display specs': display,
        'Camera specs': camera,
        'Availability status': availability,
        'Source URL': url
    }


def product_links(page, base_url, keyword=''):
    links = []
    keyword_lower = keyword.lower().strip()
    try:
        anchors = page.locator('a[href*="/dp/"], a[href*="/gp/product/"]')
        for index in range(anchors.count()):
            anchor = anchors.nth(index)
            href = anchor.get_attribute('href') or ''
            label = anchor.inner_text(timeout=500).strip()
            absolute = urljoin(base_url, href)
            haystack = f'{label} {absolute}'.lower()
            if keyword_lower and keyword_lower not in haystack:
                continue
            if absolute not in links:
                links.append(absolute)
    except Exception:
        pass
    return links


def excluded_product(title, url):
    haystack = f'{title} {url}'.lower()
    return any(term in haystack for term in ['accessor', 'case', 'refurbished', 'renewed'])


def main():
    parser = argparse.ArgumentParser(description='Amazon product scraper')
    parser.add_argument('--input', default='input.xlsx')
    parser.add_argument('--output', default='output.xlsx')
    parser.add_argument('--max-products', type=int, default=None)
    args = parser.parse_args()

    log(f'Reading input workbook {args.input}')
    try:
        input_df = pd.read_excel(args.input)
    except Exception as exc:
        log(f'Could not read input workbook: {exc}')
        raise

    results = []
    flags = []
    processed = 0

    with sync_playwright() as playwright:
        log('Starting Chromium browser; Amazon was observed to require JavaScript rendering')
        browser = playwright.chromium.launch(headless=True, proxy=get_proxy())
        context = browser.new_context()

        def inspect_response(response):
            try:
                body = response.body().decode('utf-8', errors='ignore')
            except Exception:
                body = ''
            for signal in detect_anti_bot_signals(response, body):
                add_flag(flags, response.url, 'anti-bot signal', signal)

        context.on('response', inspect_response)
        page = context.new_page()

        for row_number, row in input_df.iterrows():
            if args.max_products is not None and processed >= args.max_products:
                break
            keyword = str(row.get('keyword', '') or '').strip()
            listing_url = str(row.get('listing_url', '') or '').strip()
            detail_url = str(row.get('detail_url', '') or '').strip()

            if detail_url:
                # Independent fast path: it does not depend on a listing crawl.
                if safe_goto(page, detail_url, flags):
                    item = extract_detail(page, detail_url, flags)
                    if not excluded_product(item['Product Title'], detail_url):
                        results.append(item)
                        processed += 1
                    else:
                        add_flag(flags, detail_url, 'filtered product', 'Ignored accessory, case, refurbished, or renewed item')
                continue

            target_listing = listing_url
            if not target_listing and keyword:
                target_listing = f'https://www.amazon.com/s?k={quote(keyword)}'
            if not target_listing:
                add_flag(flags, '', 'error', f'Input row {row_number + 2} has no keyword, listing_url, or detail_url')
                continue

            if not safe_goto(page, target_listing, flags):
                continue
            links = product_links(page, target_listing, keyword)
            if not links:
                add_flag(flags, target_listing, 'low-confidence extraction', 'No product links were observed; the page may be JavaScript-disabled or structurally changed')
            for link in links:
                if args.max_products is not None and processed >= args.max_products:
                    break
                if not safe_goto(page, link, flags):
                    continue
                item = extract_detail(page, link, flags)
                title_or_slug = f'{item["Product Title"]} {link}'.lower()
                if keyword and keyword.lower() not in title_or_slug:
                    add_flag(flags, link, 'filtered product', 'Title or URL does not contain the requested keyword')
                    continue
                if excluded_product(item['Product Title'], link):
                    add_flag(flags, link, 'filtered product', 'Ignored accessory, case, refurbished, or renewed item')
                    continue
                results.append(item)
                processed += 1

        context.close()
        browser.close()

    log(f'Writing {len(results)} extracted items and {len(flags)} flags to {args.output}')
    extracted_df = pd.DataFrame(results, columns=OUTPUT_COLUMNS)
    flags_df = pd.DataFrame(flags, columns=FLAG_COLUMNS)
    with pd.ExcelWriter(args.output, engine='openpyxl') as writer:
        extracted_df.to_excel(writer, sheet_name='Extracted Data', index=False)
        flags_df.to_excel(writer, sheet_name='Flags', index=False)
    log('Run complete')


if __name__ == '__main__':
    main()
