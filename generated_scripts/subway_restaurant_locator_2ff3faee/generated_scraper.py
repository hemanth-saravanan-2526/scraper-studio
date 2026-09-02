import argparse
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

TARGET_URL = 'https://restaurants.subway.com/united-states/al'
USER_AGENT = 'Mozilla/5.0 (compatible; SubwayDirectoryScraper/1.0)'


def log(message):
    print(f'[{datetime.now().astimezone().isoformat(timespec="seconds")}] {message}', flush=True)


def clean(value):
    if value is None or pd.isna(value) or str(value).strip().lower() in ("", "nan", "none", "null"):
        return ""
    try:
        if pd.isna(value):
            return ''
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in {'nan', 'none', 'null'}:
        return ''
    return text


def get_proxy():
    return None


def solve_captcha(*args, **kwargs):
    raise NotImplementedError('CAPTCHA solving is intentionally not implemented')


def detect_anti_bot_signals(response):
    signals = []
    body = response.text or ''
    lower = body.lower()
    if response.status_code in {403, 429}:
        signals.append(f'HTTP anti-bot status {response.status_code}')
    markers = {
        'captcha': 'CAPTCHA marker',
        'cloudflare': 'Cloudflare marker',
        'perimeterx': 'PerimeterX marker',
        'datadome': 'DataDome marker',
    }
    for marker, description in markers.items():
        if marker in lower:
            signals.append(description)
    if len(body.strip()) < 500:
        signals.append(f'unusually short HTTP body ({len(body.strip())} characters)')
    return signals


def request_page(session, url, flags):
    log(f'Requesting {url}')
    try:
        response = session.get(
            url,
            headers={'User-Agent': USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9'},
            timeout=30,
            proxies=get_proxy(),
        )
    except requests.RequestException as exc:
        flags.append(flag('error', url, f'HTTP request failed: {exc}'))
        return None
    for signal in detect_anti_bot_signals(response):
        flags.append(flag('anti-bot', url, signal))
    if response.status_code >= 400:
        flags.append(flag('error', url, f'HTTP status {response.status_code}'))
        return None
    return response


def flag(kind, url, message):
    return {
        'Timestamp': datetime.now(timezone.utc).isoformat(),
        'Type': kind,
        'URL': url,
        'Message': message,
    }


def absolute_links(soup, base_url):
    links = []
    for anchor in soup.find_all('a', href=True):
        href = clean(anchor.get('href'))
        if not href or href.startswith(('javascript:', 'mailto:', 'tel:', '#')):
            continue
        links.append((urljoin(base_url, href), anchor.get_text(' ', strip=True)))
    return links


def path_parts(url):
    return [part for part in urlparse(url).path.split('/') if part]


def is_alabama_url(url):
    parts = [part.lower() for part in path_parts(url)]
    return len(parts) >= 2 and parts[0] == 'united-states' and parts[1] == 'al'


def is_detail_url(url):
    return is_alabama_url(url) and len(path_parts(url)) >= 4


def is_city_url(url):
    return is_alabama_url(url) and len(path_parts(url)) == 3


def city_links(soup, base_url, keyword):
    matches = []
    wanted = clean(keyword).lower()
    for url, text in absolute_links(soup, base_url):
        if not is_city_url(url):
            continue
        haystack = f'{text} {url}'.lower()
        if wanted and wanted not in haystack:
            continue
        matches.append(url)
    return list(dict.fromkeys(matches))


def detail_links(soup, base_url):
    matches = []
    for url, _ in absolute_links(soup, base_url):
        if is_detail_url(url):
            matches.append(url)
    return list(dict.fromkeys(matches))


def page_is_excluded(text):
    lower = text.lower()
    return bool(re.search(r'\b(?:temporary|temporarily closed|permanently closed|kiosk)\b', lower))


def extract_detail(response, flags):
    url = response.url
    soup = BeautifulSoup(response.text, 'html.parser')
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.split('\n') if line.strip()]
    if page_is_excluded(text):
        flags.append(flag('filtered', url, 'Location appears closed or temporary/kiosk; skipped'))
        return None

    record = {
        'Store Number': '',
        'Street Address': '',
        'City': '',
        'State': '',
        'Postal Code': '',
        'Phone Number': '',
        'Operating Hours': '',
        'Drive-Thru Available': '',
        'Delivery Available': '',
        'Source URL': url,
    }

    address_match = re.search(
        r'(?im)([0-9][^\n]{2,100}?)\s*,\s*([A-Za-z][A-Za-z .\'-]{1,50}),\s*(AL)\s+(\d{5}(?:-\d{4})?)\b',
        text,
    )
    if address_match:
        record['Street Address'] = address_match.group(1).strip(' ,')
        record['City'] = address_match.group(2).strip()
        record['State'] = address_match.group(3)
        record['Postal Code'] = address_match.group(4)
    else:
        for index, line in enumerate(lines):
            match = re.search(r'\b(AL)\s+(\d{5}(?:-\d{4})?)\b', line)
            if match:
                record['State'] = match.group(1)
                record['Postal Code'] = match.group(2)
                if index:
                    record['Street Address'] = lines[index - 1]
                break

    phone_match = re.search(r'(?<!\d)(?:\(?[2-9]\d{2}\)?[-.\s])\d{3}[-.\s]\d{4}(?!\d)', text)
    if phone_match:
        record['Phone Number'] = phone_match.group(0)

    store_match = re.search(r'(?i)(?:store|restaurant)\s*(?:number|#|no\.?)[\s:#-]*([A-Za-z0-9-]+)', text)
    if store_match:
        record['Store Number'] = store_match.group(1)

    hour_lines = []
    for line in lines:
        if re.search(r'(?i)\b(?:mon|monday|tue|tuesday|wed|wednesday|thu|thursday|fri|friday|sat|saturday|sun|sunday)\b', line) and re.search(r'(?i)\d{1,2}(?::\d{2})?\s*[ap]m', line):
            hour_lines.append(line)
    if hour_lines:
        record['Operating Hours'] = '; '.join(dict.fromkeys(hour_lines))

    lower = text.lower()
    for field, terms in {
        'Drive-Thru Available': ('drive-thru', 'drive thru', 'drive through'),
        'Delivery Available': ('delivery',),
    }.items():
        relevant = [line.lower() for line in lines if any(term in line.lower() for term in terms)]
        if relevant:
            joined = ' '.join(relevant)
            if re.search(r'\b(?:not available|unavailable|no|false)\b', joined):
                record[field] = 'No'
            elif re.search(r'\b(?:available|yes|true)\b', joined):
                record[field] = 'Yes'
            else:
                record[field] = relevant[0]

    required = ['Street Address', 'City', 'State', 'Postal Code', 'Operating Hours']
    missing = [field for field in required if not record[field]]
    if missing:
        flags.append(flag('low-confidence', url, 'Could not confidently extract: ' + ', '.join(missing)))
    if not record['Store Number']:
        flags.append(flag('low-confidence', url, 'Store number was not exposed in the inspected markup'))
    if not record['Phone Number']:
        flags.append(flag('low-confidence', url, 'Phone number was not confidently found'))
    if not record['Drive-Thru Available'] or not record['Delivery Available']:
        flags.append(flag('low-confidence', url, 'One or more service availability values were not exposed'))
    return record


def crawl_listing(session, listing_url, keyword, flags, seen, max_products):
    response = request_page(session, listing_url, flags)
    if response is None:
        return []
    soup = BeautifulSoup(response.text, 'html.parser')
    if is_city_url(listing_url):
        urls = detail_links(soup, listing_url)
    else:
        urls = city_links(soup, listing_url, keyword) if clean(keyword) else detail_links(soup, listing_url)
        if clean(keyword) and not urls:
            flags.append(flag('low-confidence', listing_url, 'No matching Alabama city links found; selectors were not exposed during inspection'))
        if clean(keyword):
            expanded = []
            for city_url in urls:
                city_response = request_page(session, city_url, flags)
                if city_response is not None:
                    expanded.extend(detail_links(BeautifulSoup(city_response.text, 'html.parser'), city_url))
            urls = expanded
    results = []
    for detail_url in list(dict.fromkeys(urls)):
        if max_products is not None and len(seen) >= max_products:
            break
        if detail_url in seen:
            continue
        seen.add(detail_url)
        detail_response = request_page(session, detail_url, flags)
        if detail_response is not None:
            item = extract_detail(detail_response, flags)
            if item is not None:
                results.append(item)
    return results


def main():
    parser = argparse.ArgumentParser(description='Scrape Subway Alabama locations')
    parser.add_argument('--input', default='input.xlsx')
    parser.add_argument('--output', default='output.xlsx')
    parser.add_argument('--max-products', type=int, default=None)
    args = parser.parse_args()

    log('Reading input workbook')
    input_df = pd.read_excel(args.input).fillna('')
    session = requests.Session()
    results = []
    flags = []
    seen = set()

    for _, row in input_df.iterrows():
        keyword = clean(row.get('keyword', ''))
        listing_url = clean(row.get('listing_url', ''))
        detail_url = clean(row.get('detail_url', ''))
        if args.max_products is not None and len(results) >= args.max_products:
            break
        if detail_url:
            log(f'Using independent detail URL path: {detail_url}')
            if detail_url in seen:
                continue
            seen.add(detail_url)
            response = request_page(session, detail_url, flags)
            if response is not None:
                item = extract_detail(response, flags)
                if item is not None:
                    results.append(item)
            continue
        if listing_url or keyword:
            url = listing_url or TARGET_URL
            log(f'Crawling listing flow from {url}')
            results.extend(crawl_listing(session, url, keyword, flags, seen, args.max_products))

    if args.max_products is not None:
        results = results[:args.max_products]
    columns = ['Store Number', 'Street Address', 'City', 'State', 'Postal Code', 'Phone Number', 'Operating Hours', 'Drive-Thru Available', 'Delivery Available', 'Source URL']
    data_df = pd.DataFrame(results, columns=columns)
    flags_df = pd.DataFrame(flags, columns=['Timestamp', 'Type', 'URL', 'Message'])
    log(f'Writing {len(data_df)} extracted rows and {len(flags_df)} flags')
    with pd.ExcelWriter(args.output, engine='openpyxl') as writer:
        data_df.to_excel(writer, sheet_name='Extracted Data', index=False)
        flags_df.to_excel(writer, sheet_name='Flags', index=False)
    log('Completed')


if __name__ == '__main__':
    main()