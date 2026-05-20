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
    # E-commerce UI chrome that pollutes the snippet:
    "cart", "panier", "minicart", "mini-cart", "basket", "checkout",
    "account", "compte", "search-bar", "search", "login", "register",
    "popup", "modal", "overlay", "toolbar", "loader", "loading", "spinner",
)

# CSS selectors for the LONG description block, by CMS. Order matters: try the
# most specific first. The matching block (>= 100 chars) is preferred over a
# generic body scan because it isolates the prose actually written for the
# product (vs short summary / specs table / cross-sell).
LONG_DESCRIPTION_SELECTORS = [
    # PrestaShop
    ".product-description",
    "#product-description",
    ".tab-pane#description",
    ".product-information .product-description",
    # WooCommerce
    "#tab-description",
    ".woocommerce-tabs .panel#tab-description",
    ".woocommerce-Tabs-panel--description",
    # Shopify
    ".product__description",
    ".product-single__description",
    ".product-content",
    # Magento
    ".product.attribute.description .value",
    "#description.value",
    # Drupal Commerce
    ".field--name-body",
    ".field--name-field-description",
    # WordPress (generic, last resort)
    ".entry-content",
    "#post-content",
    # Common fallback inside any CMS
    "[itemprop='description']",
]
# Selectors to ALWAYS strip before extracting the description (short summaries
# that pollute the duplicate-text signal because they're usually short and reused)
SHORT_SUMMARY_SELECTORS = [
    ".product-short-description",
    ".woocommerce-product-details__short-description",
    ".product__excerpt",
    ".product-meta__excerpt",
    ".product.attribute.overview",
    ".excerpt",
    ".summary-content",
]
# UI words that, if present in a snippet, mean it's most likely cart/account
# chrome rather than product description. A snippet with any of these is rejected.
UI_GENERIC_WORDS = {
    "panier", "sous-total", "loading", "continuer mes achats", "voir mon panier",
    "ajouter au panier", "newsletter", "code promo", "mon compte", "se connecter",
    "créer un compte", "mot de passe", "adresse email", "loading",
}
DEFAULT_SNIPPET_LEN = 180
DEFAULT_MAX_URLS = 5
DEFAULT_THRESHOLD = 5
PRODUCT_TYPES = {"product_strong", "product_weak", "product"}


def _is_ui_chrome(snippet: str) -> bool:
    s = snippet.lower()
    return any(w in s for w in UI_GENERIC_WORDS)


def _clean_text(soup: BeautifulSoup) -> tuple[str, str]:
    """Return (text, source_selector). source_selector identifies which strategy
    picked the text — useful to diagnose why a snippet looks weird."""
    # 1) Strip noise tags + classes
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(class_=lambda c: c and any(n in " ".join(c).lower() for n in NOISE_CLASSES)):
        tag.decompose()
    # 2) Strip short-summary blocks BEFORE picking the long description
    for sel in SHORT_SUMMARY_SELECTORS:
        try:
            for tag in soup.select(sel):
                tag.decompose()
        except Exception:
            continue
    # 3) Prefer CMS-aware long-description selectors
    for sel in LONG_DESCRIPTION_SELECTORS:
        try:
            zone = soup.select_one(sel)
        except Exception:
            continue
        if zone:
            text = zone.get_text(separator=" ", strip=True)
            if len(text) > 100:
                return re.sub(r"\s+", " ", text), f"long_description:{sel}"
    # 4) Generic main containers as a fallback
    for sel in ("main", "[id*='main']", "[class*='main']", "[class*='product']", "article"):
        try:
            zone = soup.select_one(sel)
        except Exception:
            continue
        if zone:
            text = zone.get_text(separator=" ", strip=True)
            if len(text) > 200:
                return re.sub(r"\s+", " ", text), f"main_zone:{sel}"
    # 5) Body fallback
    body = soup.body
    if body:
        return re.sub(r"\s+", " ", body.get_text(separator=" ", strip=True)), "body"
    return "", "none"


def _extract_snippets(text: str, snippet_len: int, max_candidates: int = 3) -> list[str]:
    """Return up to N distinct sentence-snippets to cross-check against Google.

    A single snippet can miss a duplicated page if it falls on a less-quoted
    sentence. Three snippets distributed across the content (start / middle /
    end) catch most manufacturer-boilerplate cases.
    """
    if not text:
        return []
    cleaned = re.sub(r"[\"“”‘’]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) < 60:
        return [cleaned] if cleaned else []

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    candidates: list[str] = []
    for s in sentences:
        s = s.strip().rstrip(".!?").strip()
        if 60 <= len(s) <= snippet_len and not _is_ui_chrome(s):
            words = re.findall(r"\b\w{3,}\b", s.lower())
            if len(set(words)) >= 8:
                candidates.append(s)
    if not candidates:
        # Fallback to mid-text crop
        if len(cleaned) >= snippet_len + 40:
            start = max(40, (len(cleaned) - snippet_len) // 2)
            while start < len(cleaned) and cleaned[start] != " ":
                start += 1
            start += 1
            end = min(len(cleaned), start + snippet_len)
            while end > start and cleaned[end - 1] != " ":
                end -= 1
            return [cleaned[start:end].strip()]
        return [cleaned[:snippet_len].strip()]

    if len(candidates) <= max_candidates:
        return candidates
    # Pick start / middle / end to spread coverage
    return [
        candidates[0],
        candidates[len(candidates) // 2],
        candidates[-1],
    ]


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
    # Encoding: requests guesses from headers, but many sites send no charset or
    # the wrong one, producing mojibake like "Ã©" (UTF-8 read as Latin-1).
    # Use apparent_encoding (chardet) when the declared encoding is the
    # suspicious ISO-8859-1 default OR when accented chars look broken.
    declared = (resp.encoding or "").lower()
    raw_bytes = resp.content
    if declared in ("iso-8859-1", "latin-1", "latin1", "") or b"\xc3\x83" in raw_bytes[:5000]:
        try:
            html_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            html_text = resp.text
    else:
        html_text = resp.text
    soup = BeautifulSoup(html_text, "html.parser")
    text, source = _clean_text(soup)
    snippets = _extract_snippets(text, snippet_len)
    if not snippets or all(len(s) < 60 for s in snippets):
        out["fetch_error"] = f"main content too short ({len(text)} chars from {source})"
        out["text_source"] = source
        return out
    out["snippets"] = snippets
    out["snippet"] = snippets[0]  # backward compat
    out["text_source"] = source
    out["text_length"] = len(text)
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
        snippets_tried = page.get("snippets") or ([page["snippet"]] if page.get("snippet") else [])
        record = {
            "url": url,
            "type": entry.get("type"),
            "snippet": page.get("snippet"),  # the primary one shown in report
            "snippets_tried": snippets_tried,
            "fetch_error": page.get("fetch_error"),
            "text_source": page.get("text_source"),
            "text_length": page.get("text_length"),
            "external_domains_count": None,
            "external_domains_sample": [],
            "manufacturer_copy_suspect": False,
            "serp_status": None,
            "per_snippet_results": [],
        }
        if snippets_tried:
            # Per-domain count across snippets: a domain that appears in 2+
            # snippets is a much stronger signal of duplicated description
            # than a domain matched on a single snippet.
            domain_hits: dict[str, int] = {}
            matched_snippets = 0
            statuses = []
            for snip in snippets_tried:
                try:
                    response = _dataforseo_serp(snip, login, password)
                    parsed = _parse_serp_response(response, audited_domain)
                    if parsed["domains"]:
                        matched_snippets += 1
                    for d in parsed["domains"]:
                        domain_hits[d] = domain_hits.get(d, 0) + 1
                    statuses.append(parsed["raw_status"] or "ok")
                    record["per_snippet_results"].append({
                        "snippet": snip[:80],
                        "domains_count": len(parsed["domains"]),
                        "sample_domains": parsed["domains"][:5],
                    })
                except Exception as exc:
                    statuses.append(f"request failed: {exc}")
                    record["per_snippet_results"].append({
                        "snippet": snip[:80],
                        "error": str(exc),
                    })

            total_unique = len(domain_hits)
            recurrent = sorted(
                [d for d, c in domain_hits.items() if c >= 2],
                key=lambda d: (-domain_hits[d], d),
            )

            # Intensity: how many of the tested snippets matched at least one
            # external domain. A single snippet matching is weak signal; all
            # three matching is overwhelming.
            total_snippets = len(snippets_tried)
            if matched_snippets == 0:
                intensity = "NONE"
            elif matched_snippets == 1:
                intensity = "LOW"
            elif matched_snippets == 2:
                intensity = "MEDIUM"
            else:
                intensity = "HIGH"

            record["external_domains_count"] = total_unique
            record["external_domains_sample"] = sorted(domain_hits.keys())[:10]
            record["recurrent_domains"] = [
                {"domain": d, "matched_snippets": domain_hits[d]}
                for d in recurrent[:10]
            ]
            record["matched_snippets"] = matched_snippets
            record["total_snippets"] = total_snippets
            record["intensity"] = intensity
            record["serp_status"] = "; ".join(statuses)

            # Suspect if either total unique domains crosses threshold OR
            # intensity is MEDIUM/HIGH (which is independent of threshold).
            if total_unique >= threshold or intensity in ("MEDIUM", "HIGH"):
                record["manufacturer_copy_suspect"] = True
                suspect_count += 1
        checked.append(record)

    issues: list[dict] = []
    for record in checked:
        if record.get("manufacturer_copy_suspect"):
            intensity = record.get("intensity", "?")
            matched = record.get("matched_snippets", 0)
            total = record.get("total_snippets", 0)
            recurrent = record.get("recurrent_domains") or []
            unique_count = record.get("external_domains_count", 0)

            # Recurrent domains are the strongest signal — list them first
            recurrent_str = (
                ", ".join(f"{r['domain']} ({r['matched_snippets']} snippets)" for r in recurrent[:5])
                if recurrent else "aucun domaine ne matche sur plusieurs snippets"
            )
            other_sample = ", ".join(record.get("external_domains_sample") or [])

            severity = "warning" if intensity == "HIGH" else ("warning" if intensity == "MEDIUM" else "info")

            issues.append({
                "severity": severity,
                "area": "manufacturer_dup_check",
                "finding": (
                    f"Description produit probablement copiée du constructeur — "
                    f"intensité {intensity} ({matched}/{total} snippets matchés sur "
                    f"{unique_count} domaines externes uniques) — {record['url']}"
                ),
                "evidence": (
                    f"Domaines récurrents (≥2 snippets matchés) : {recurrent_str}. "
                    f"Échantillon de tous les domaines détectés : {other_sample}. "
                    f"Source du texte analysé : {record.get('text_source', '?')}."
                ),
                "fix": (
                    "Réécrire la description avec un angle unique : cas d'usage, comparaison "
                    "avec un produit voisin de votre catalogue, conseil d'installation, "
                    "retours d'expérience client. Une fiche dupliquée plafonne mécaniquement "
                    "le positionnement longue traîne face aux concurrents qui ré-écrivent."
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
