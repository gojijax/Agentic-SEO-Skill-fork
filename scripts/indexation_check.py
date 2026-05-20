#!/usr/bin/env python3
"""Compare the number of pages Google has indexed vs. what the site declares.

Strategy:
  1. Query DataForSEO SERP for `site:<domain>` to read Google's reported count
     of indexed pages.
  2. Try to read the sitemap.xml (and its index variants) to count declared URLs.
  3. Optionally read haloscan_data.json (already produced by audit-prospect-
     ecommerce) for `active_page_count` (pages that rank at least once).
  4. Flag a gap when indexed < declared by more than `--gap-threshold` percent.

Auth: expects DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD in env (.env or shell).
Without credentials the script exits 0 with a "skipped" payload so the audit
pipeline keeps running.

Usage:
    python indexation_check.py --domain example.com --json out.json
    python indexation_check.py --domain example.com --haloscan-data audits/example.com/temp/haloscan_data.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from urllib.parse import urlparse

try:
    from env_loader import get_env, load_env
except ImportError:
    from scripts.env_loader import get_env, load_env

try:
    from lib.safe_http import safe_post
except ImportError:
    from scripts.lib.safe_http import safe_post

try:
    from seo_common import discover_sitemap_urls, fetch_url, parse_sitemap_xml
except ImportError:
    from scripts.seo_common import discover_sitemap_urls, fetch_url, parse_sitemap_xml


DATAFORSEO_ENDPOINT = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
DEFAULT_GAP_THRESHOLD = 0.5  # 50% gap = warning


def _normalize_domain(value: str) -> str:
    if "://" in value:
        netloc = urlparse(value).netloc
    else:
        netloc = value
    return netloc.lower().lstrip("www.").rstrip("/")


def _query_google_index(domain: str, login: str, password: str, timeout: int = 30) -> dict:
    creds = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
    }
    body = [{
        "language_code": "fr",
        "location_code": 2250,  # France
        "keyword": f"site:{domain}",
        "depth": 10,
        "device": "desktop",
    }]
    resp = safe_post(DATAFORSEO_ENDPOINT, headers=headers, json=body, timeout=timeout)
    payload = resp.json()
    top_code = payload.get("status_code")
    if resp.status_code >= 400 or (isinstance(top_code, int) and top_code >= 40000):
        return {
            "approx_count": None,
            "raw_status": f"HTTP {resp.status_code} / status_code {top_code}: {payload.get('status_message')}",
        }
    tasks = payload.get("tasks") or []
    if not tasks:
        return {"approx_count": None, "raw_status": payload.get("status_message")}
    task = tasks[0]
    out: dict = {"approx_count": None, "raw_status": task.get("status_message")}
    for outer in task.get("result") or []:
        total = outer.get("total_count") or outer.get("se_results_count")
        if total is not None:
            out["approx_count"] = int(total)
            break
    return out


def _count_sitemap_urls(domain: str, timeout: int = 15, max_sitemaps: int = 25) -> dict:
    site_url = f"https://{domain}"
    queue = list(dict.fromkeys(discover_sitemap_urls(site_url, timeout=timeout)))
    if not queue:
        return {"found": False, "total_urls": 0, "sitemaps_seen": [], "errors": ["no sitemap discovered"]}
    seen_sitemaps: set[str] = set()
    urls: set[str] = set()
    errors: list[str] = []
    while queue and len(seen_sitemaps) < max_sitemaps:
        sm = queue.pop(0)
        if sm in seen_sitemaps:
            continue
        seen_sitemaps.add(sm)
        fetched = fetch_url(sm, timeout=timeout, max_bytes=8_000_000)
        if fetched.get("status") != 200:
            errors.append(f"{sm} -> HTTP {fetched.get('status')}")
            continue
        parsed = parse_sitemap_xml(fetched.get("text") or "", sm)
        if parsed["type"] == "urlset":
            for row in parsed["urls"]:
                urls.add(row["loc"])
        elif parsed["type"] == "sitemapindex":
            for child in parsed["sitemaps"]:
                queue.append(child["loc"])
        else:
            errors.append(f"{sm} -> unsupported sitemap type")
    # "found" = at least one sitemap returned 200 AND parsed to a urlset/sitemapindex.
    # Candidate URLs that all 404 don't count.
    found_real = len(urls) > 0 or any(
        "-> HTTP 200" not in e and "-> HTTP" not in e for e in errors
    )
    # Simpler: found is True iff we actually parsed at least one urlset
    found_real = len(urls) > 0
    return {
        "found": found_real,
        "total_urls": len(urls),
        "sitemaps_seen": list(seen_sitemaps),
        "errors": errors,
    }


def _read_haloscan(haloscan_path: str | None) -> dict:
    if not haloscan_path or not os.path.exists(haloscan_path):
        return {"available": False}
    try:
        with open(haloscan_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "error": str(exc)}
    return {
        "available": True,
        "active_page_count": data.get("traffic"),
        "best_keywords_count": len(data.get("best_keywords") or []),
        "top_pages_count": len(data.get("top_pages") or []),
    }


def run(domain: str, haloscan_path: str | None, gap_threshold: float) -> dict:
    load_env()
    login = get_env("DATAFORSEO_LOGIN")
    password = get_env("DATAFORSEO_PASSWORD")
    if not login or not password:
        return {
            "skipped": True,
            "reason": "DATAFORSEO_LOGIN and/or DATAFORSEO_PASSWORD missing",
        }

    domain = _normalize_domain(domain)
    google_index: dict = {"approx_count": None, "error": None}
    try:
        google_index = _query_google_index(domain, login, password)
    except Exception as exc:
        google_index = {"approx_count": None, "error": str(exc)}

    sitemap_info = _count_sitemap_urls(domain)
    haloscan_info = _read_haloscan(haloscan_path)

    google_count = google_index.get("approx_count")
    sitemap_count = sitemap_info.get("total_urls") if sitemap_info.get("found") else None

    issues: list[dict] = []
    findings: dict = {
        "domain": domain,
        "google_indexed_approx": google_count,
        "sitemap_count": sitemap_count,
        "sitemap_found": sitemap_info.get("found"),
        "sitemap_sources": sitemap_info.get("sitemaps_seen", []),
        "haloscan": haloscan_info,
        "gap_threshold": gap_threshold,
        "google_query_error": google_index.get("error"),
    }

    if google_count is None:
        issues.append({
            "severity": "info",
            "area": "indexation_check",
            "finding": f"Could not retrieve Google index count for {domain}",
            "evidence": google_index.get("error") or google_index.get("raw_status") or "no count returned",
            "fix": "Check DataForSEO credentials and retry. Without this signal the indexation gap cannot be measured.",
        })
    else:
        # Sitemap missing entirely while Google has indexed pages = important SEO gap
        if not sitemap_info.get("found"):
            issues.append({
                "severity": "warning",
                "area": "indexation_check",
                "finding": f"Aucun sitemap.xml exposé alors que Google a indexé environ {google_count} pages",
                "evidence": (
                    f"Aucune des URLs candidates (/sitemap.xml, /sitemap_index.xml, etc.) ne répond. "
                    f"Aucune directive `Sitemap:` détectable dans robots.txt. "
                    f"Google a néanmoins indexé ~{google_count} pages, donc le site est crawlé via "
                    f"d'autres signaux (liens internes/externes)."
                ),
                "fix": (
                    "Créer un sitemap.xml exposé à la racine (PrestaShop : module 'Google sitemap'). "
                    "Ajouter la directive Sitemap dans robots.txt. Soumettre le sitemap dans Search "
                    "Console une fois en place pour accélérer la découverte des pages."
                ),
            })

        # Compare to sitemap if available
        if sitemap_count and sitemap_count > 0:
            gap = (sitemap_count - google_count) / sitemap_count
            findings["gap_vs_sitemap"] = round(gap, 3)
            if gap > gap_threshold:
                issues.append({
                    "severity": "warning",
                    "area": "indexation_check",
                    "finding": f"Indexation gap: {google_count} indexed / {sitemap_count} in sitemap ({round(gap*100, 1)}% missing)",
                    "evidence": f"Google site:{domain} ≈ {google_count}; sitemap declares {sitemap_count} URLs.",
                    "fix": (
                        "Investigate the gap: check noindex tags in bulk, robots.txt disallow rules, "
                        "canonicalization to other URLs, redirect chains, or thin content getting filtered."
                    ),
                })
        # Compare to haloscan rank count if no sitemap
        elif haloscan_info.get("available"):
            rank_count = haloscan_info.get("top_pages_count") or 0
            if rank_count > 0 and google_count > 0:
                rank_ratio = rank_count / google_count
                findings["rank_to_index_ratio"] = round(rank_ratio, 3)
                if rank_ratio < 0.3:
                    issues.append({
                        "severity": "info",
                        "area": "indexation_check",
                        "finding": f"Many indexed pages don't rank: {rank_count} ranking / {google_count} indexed",
                        "evidence": f"Less than 30% of indexed pages rank on at least one keyword.",
                        "fix": "Audit thin content pages or pages with unclear targeting. Consider noindex or content enrichment.",
                    })

    findings["issues"] = issues
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Google indexation vs sitemap/Haloscan")
    parser.add_argument("--domain", required=True, help="Domain to check (with or without scheme)")
    parser.add_argument("--haloscan-data", default=None,
                        help="Optional path to haloscan_data.json from audit-prospect-ecommerce")
    parser.add_argument("--gap-threshold", type=float, default=DEFAULT_GAP_THRESHOLD,
                        help=f"Gap threshold to flag warning (default: {DEFAULT_GAP_THRESHOLD})")
    parser.add_argument("--json", "-j", action="store_true", help="Emit JSON (default)")
    parser.add_argument("--output", default=None, help="Write JSON to this path instead of stdout")
    args = parser.parse_args()

    result = run(args.domain, args.haloscan_data, args.gap_threshold)
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
