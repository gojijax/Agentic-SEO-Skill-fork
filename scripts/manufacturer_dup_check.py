#!/usr/bin/env python3
"""Detect product descriptions copied from the manufacturer.

For each product URL listed in urls.json, this script:
  1. Fetches the page and extracts a distinctive text snippet from the main
     content (~180 chars, avoiding boilerplate intros).
  2. Queries DataForSEO's SERP organic live API for the snippet quoted between
     double quotes ("exact match" search).
  3. Counts how many distinct external domains return that snippet.
  4. Flags the URL as "manufacturer_copy_suspect" when the count is above a
     configurable threshold.

This is the automated version of Franck's manual workflow: copy-paste a chunk
of product text into Google, and if it shows up on dozens of other sites, the
description is the manufacturer's boilerplate.

Auth: expects DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD in the env (.env file or
exported variables). Without credentials the script exits 0 with a "skipped"
result so the rest of the audit pipeline keeps working.

Usage:
    python manufacturer_dup_check.py --urls-file path/to/urls.json --json out.json
    python manufacturer_dup_check.py --urls-file urls.json --max-urls 5 --threshold 5
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from urllib.parse import urlparse

try:
    from env_loader import get_env, load_env
except ImportError:
    from scripts.env_loader import get_env, load_env

try:
    from lib.safe_http import safe_get, safe_post, default_headers
except ImportError:
    from scripts.lib.safe_http import safe_get, safe_post, default_headers

try:
    from bs4 import BeautifulSoup
except ImportError:
    print(json.dumps({"error": "beautifulsoup4 required (pip install beautifulsoup4)"}))
    sys.exit(0)


DATAFORSEO_ENDPOINT = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"

NOISE_TAGS = ("nav", "header", "footer", "aside", "script", "style", "noscript")
NOISE_CLASSES = (
    "breadcrumb", "menu", "sidebar", "footer", "header", "nav", "cookie",
    "newsletter", "product-list", "related-products", "reviews",
)
DEFAULT_SNIPPET_LEN = 180
DEFAULT_MAX_URLS = 5
DEFAULT_THRESHOLD = 5
PRODUCT_TYPES = {"product_strong", "product_weak", "product"}


def _clean_text(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(class_=lambda c: c and any(n in " ".join(c).lower() for n in NOISE_CLASSES)):
        tag.decompose()
    for sel in ("main", "[id*='main']", "[class*='main']", "[class*='product']", "article"):
        zone = soup.select_one(sel)
        if zone:
            text = zone.get_text(separator=" ", strip=True)
            if len(text) > 200:
                return re.sub(r"\s+", " ", text)
    body = soup.body
    if body:
        return re.sub(r"\s+", " ", body.get_text(separator=" ", strip=True))
    return ""


def _extract_snippet(text: str, snippet_len: int) -> str:
    """Pull a distinctive ~snippet_len-char window from the middle of the text."""
    if not text or len(text) < snippet_len + 40:
        return text.strip()
    start = max(40, (len(text) - snippet_len) // 2)
    while start < len(text) and text[start] != " ":
        start += 1
    start += 1
    end = min(len(text), start + snippet_len)
    while end > start and text[end - 1] != " ":
        end -= 1
    snippet = text[start:end].strip()
    snippet = re.sub(r"[\"“”‘’]", " ", snippet)
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return snippet


def _fetch_snippet(url: str, snippet_len: int, timeout: int = 12) -> dict:
    out = {"url": url, "snippet": None, "fetch_error": None}
    try:
        resp = safe_get(url, timeout=timeout, headers=default_headers())
    except Exception as exc:
        out["fetch_error"] = f"fetch failed: {exc}"
        return out
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype.lower():
        out["fetch_error"] = f"non-html content-type: {ctype}"
        return out
    soup = BeautifulSoup(resp.text, "html.parser")
    text = _clean_text(soup)
    snippet = _extract_snippet(text, snippet_len)
    if not snippet or len(snippet) < 60:
        out["fetch_error"] = "main content too short to extract a distinctive snippet"
        return out
    out["snippet"] = snippet
    return out


def _dataforseo_serp(snippet: str, login: str, password: str, timeout: int = 30) -> dict:
    """Query DataForSEO SERP organic live (advanced) for the quoted snippet."""
    creds = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
    }
    body = [{
        "language_code": "fr",
        "location_code": 2250,  # France
        "keyword": f'"{snippet}"',
        "depth": 20,
        "device": "desktop",
    }]
    resp = safe_post(DATAFORSEO_ENDPOINT, headers=headers, json=body, timeout=timeout)
    payload = resp.json()
    top_code = payload.get("status_code")
    if resp.status_code >= 400 or (isinstance(top_code, int) and top_code >= 40000):
        raise RuntimeError(
            f"DataForSEO HTTP {resp.status_code} / status_code {top_code}: "
            f"{payload.get('status_message')}"
        )
    return payload


def _parse_serp_response(payload: dict, audited_domain: str) -> dict:
    """Extract domain counts from a DataForSEO live advanced response."""
    result = {"total_results": 0, "domains": [], "raw_status": None}
    tasks = payload.get("tasks") or []
    if not tasks:
        result["raw_status"] = payload.get("status_message")
        return result
    task = tasks[0]
    result["raw_status"] = task.get("status_message")
    for outer in task.get("result") or []:
        items = outer.get("items") or []
        domain_counts: dict[str, int] = {}
        for item in items:
            if item.get("type") != "organic":
                continue
            domain = (item.get("domain") or "").lower().lstrip("www.")
            if not domain or domain == audited_domain:
                continue
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
        result["domains"] = sorted(domain_counts.keys())
        result["total_results"] = sum(domain_counts.values())
    return result


def run(urls_file: str, max_urls: int, snippet_len: int, threshold: int,
        timeout: int = 12) -> dict:
    load_env()
    login = get_env("DATAFORSEO_LOGIN")
    password = get_env("DATAFORSEO_PASSWORD")
    if not login or not password:
        return {
            "skipped": True,
            "reason": "DATAFORSEO_LOGIN and/or DATAFORSEO_PASSWORD missing from environment",
            "checked": [],
        }

    try:
        with open(urls_file, "r", encoding="utf-8") as f:
            urls_meta = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": f"could not read urls file: {exc}", "checked": []}

    audited_domain = (urls_meta.get("domain") or "").lower().lstrip("www.")
    if not audited_domain:
        # Fallback: derive from the first URL.
        for entry in urls_meta.get("urls", []):
            if entry.get("url"):
                audited_domain = urlparse(entry["url"]).netloc.lower().lstrip("www.")
                break

    products = [
        entry for entry in urls_meta.get("urls", [])
        if isinstance(entry, dict) and entry.get("type") in PRODUCT_TYPES and entry.get("url")
    ]
    products = products[:max_urls]
    if not products:
        return {"skipped": True, "reason": "no product URLs in urls.json", "checked": []}

    checked: list[dict] = []
    suspect_count = 0
    for entry in products:
        url = entry["url"]
        page = _fetch_snippet(url, snippet_len, timeout=timeout)
        record = {
            "url": url,
            "type": entry.get("type"),
            "snippet": page.get("snippet"),
            "fetch_error": page.get("fetch_error"),
            "external_domains_count": None,
            "external_domains_sample": [],
            "manufacturer_copy_suspect": False,
            "serp_status": None,
        }
        if page.get("snippet"):
            try:
                response = _dataforseo_serp(page["snippet"], login, password)
                parsed = _parse_serp_response(response, audited_domain)
                record["external_domains_count"] = len(parsed["domains"])
                record["external_domains_sample"] = parsed["domains"][:10]
                record["serp_status"] = parsed["raw_status"]
                if record["external_domains_count"] >= threshold:
                    record["manufacturer_copy_suspect"] = True
                    suspect_count += 1
            except Exception as exc:
                record["serp_status"] = f"request failed: {exc}"
        checked.append(record)

    issues: list[dict] = []
    for record in checked:
        if record.get("manufacturer_copy_suspect"):
            sample_domains = ", ".join(record.get("external_domains_sample") or [])
            issues.append({
                "severity": "warning",
                "area": "manufacturer_dup_check",
                "finding": f"Product description likely copied from manufacturer: {record['url']}",
                "evidence": (
                    f"Quoted snippet of {len(record.get('snippet') or '')} chars found on "
                    f"{record.get('external_domains_count')} external domains. "
                    f"Sample: {sample_domains}"
                ),
                "fix": (
                    "Rewrite this product description with unique value-add content "
                    "(features in context, use cases, comparisons). Boilerplate manufacturer "
                    "descriptions cap the long-tail SEO potential across the catalogue."
                ),
            })
        elif record.get("fetch_error"):
            issues.append({
                "severity": "info",
                "area": "manufacturer_dup_check",
                "finding": f"Could not check manufacturer-copy status for {record['url']}",
                "evidence": record["fetch_error"],
                "fix": "Verify the URL is reachable and contains a substantive product description in HTML.",
            })

    return {
        "skipped": False,
        "domain": audited_domain,
        "threshold": threshold,
        "snippet_length": snippet_len,
        "max_urls": max_urls,
        "products_checked": len(checked),
        "products_suspect": suspect_count,
        "checked": checked,
        "issues": issues,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect manufacturer-copied product descriptions via DataForSEO SERP")
    parser.add_argument("--urls-file", required=True, help="Path to urls.json produced by audit-prospect-cowork")
    parser.add_argument("--json", "-j", action="store_true", help="Emit JSON only (default)")
    parser.add_argument("--max-urls", type=int, default=DEFAULT_MAX_URLS,
                        help=f"Max product URLs to check (default: {DEFAULT_MAX_URLS})")
    parser.add_argument("--snippet-length", type=int, default=DEFAULT_SNIPPET_LEN,
                        help=f"Snippet length in characters (default: {DEFAULT_SNIPPET_LEN})")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                        help=f"Min external domains to flag suspect (default: {DEFAULT_THRESHOLD})")
    parser.add_argument("--output", default=None, help="Write JSON to this path instead of stdout")
    args = parser.parse_args()

    result = run(args.urls_file, args.max_urls, args.snippet_length, args.threshold)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(payload)
            f.write("\n")
        print(f"Wrote {args.output}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
