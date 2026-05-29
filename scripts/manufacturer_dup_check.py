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
import os
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

# Domaines à exclure des résultats SERP : CDN d'images, comparateurs, agrégateurs
# qui matchent par hasard (images de produits indexées Google, snippets de
# fragments très courts). Ces matches ne sont pas des vraies pages de
# description concurrente.
EXCLUDED_DOMAINS = {
    # CDN images e-commerce
    "scene7.com", "boulanger.scene7.com", "media.adeo.com",
    "i.pinimg.com", "pinterest.com", "pinterest.fr",
    "cdn.shopify.com", "shopifycdn.com",
    # Réseaux sociaux qui apparaissent souvent
    "facebook.com", "instagram.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "linkedin.com", "tiktok.com",
    # Comparateurs / portails qui agrègent sans copier
    "google.com", "google.fr",
}


def _is_excluded_domain(domain: str) -> bool:
    """True if a domain should be ignored in the duplicate analysis (CDN, social, etc.)."""
    if not domain:
        return True
    d = domain.lower().lstrip("www.")
    if d in EXCLUDED_DOMAINS:
        return True
    # Match suffixes too (e.g. random.scene7.com)
    return any(d.endswith("." + x) or d == x for x in EXCLUDED_DOMAINS)


def _is_ui_chrome(snippet: str) -> bool:
    s = snippet.lower()
    return any(w in s for w in UI_GENERIC_WORDS)


# Map a detected CMS to the subset of selectors most likely to identify its
# long description block. When we know the CMS we try ONLY those selectors
# first — avoids matching a foreign selector (e.g. WordPress .entry-content
# on a PrestaShop theme that happens to include similar markup) which would
# pick up the wrong block and produce false positives downstream.
CMS_LONG_DESCRIPTION_PRIORITY = {
    "PrestaShop": [
        ".product-description",
        "#product-description",
        ".tab-pane#description",
        ".product-information .product-description",
        "[itemprop='description']",
    ],
    "WooCommerce": [
        "#tab-description",
        ".woocommerce-tabs .panel#tab-description",
        ".woocommerce-Tabs-panel--description",
        ".entry-content",
        "[itemprop='description']",
    ],
    "Shopify": [
        ".product__description",
        ".product-single__description",
        ".product-content",
        ".rte",
        "[itemprop='description']",
    ],
    "Magento": [
        ".product.attribute.description .value",
        "#description.value",
        "[itemprop='description']",
    ],
    "Drupal": [
        ".field--name-body",
        ".field--name-field-description",
        "[itemprop='description']",
    ],
    "WordPress": [
        ".entry-content",
        "#post-content",
        "[itemprop='description']",
    ],
}


def _clean_text(soup: BeautifulSoup, cms_hint: str | None = None) -> tuple[str, str]:
    """Return (text, source_selector). source_selector identifies which strategy
    picked the text — useful to diagnose why a snippet looks weird.

    cms_hint (e.g. "PrestaShop"): when provided, we ONLY try the selectors
    known to match that CMS first. Only if none of them produce sufficient
    content do we fall back to the broad LONG_DESCRIPTION_SELECTORS list.
    """
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

    # 3a) CMS-prioritised selectors (when CMS is known and recognised)
    if cms_hint:
        # Hybrid label like "PrestaShop + WooCommerce (hybride)" → keep first token
        primary = cms_hint.split(" +")[0].split(" (")[0].strip()
        cms_selectors = CMS_LONG_DESCRIPTION_PRIORITY.get(primary)
        if cms_selectors:
            for sel in cms_selectors:
                try:
                    zone = soup.select_one(sel)
                except Exception:
                    continue
                if zone:
                    text = zone.get_text(separator=" ", strip=True)
                    if len(text) > 100:
                        return re.sub(r"\s+", " ", text), f"cms[{primary}]:{sel}"

    # 3b) Broad CMS-aware long-description selectors (CMS unknown or
    # CMS-specific selectors didn't match)
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
    if len(cleaned) < 120:
        return [cleaned] if cleaned else []

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    candidates: list[str] = []
    # Min 120 chars: shorter snippets generate too much SERP noise (generic
    # phrases match dozens of unrelated sites). A 120-char sentence is
    # distinctive enough that an exact-match SERP hit is meaningful.
    for s in sentences:
        s = s.strip().rstrip(".!?").strip()
        if 120 <= len(s) <= snippet_len and not _is_ui_chrome(s):
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


def _fetch_snippet(url: str, snippet_len: int, timeout: int = 12, cms_hint: str | None = None) -> dict:
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
    text, source = _clean_text(soup, cms_hint=cms_hint)
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


# --- Niveau 2 : validation HTML brut (refonte 29/05/2026) ---
#
# DataForSEO SERP est un endpoint de tracking, pas de detection de duplication.
# Google tolere les guillemets sur les phrases longues (>= 60 chars) et renvoie
# des resultats semantiquement proches sans la phrase exacte. Sur BSD le 29/05,
# 12 domaines "tiers" remontes par BHUNA ont tous ete invalides au check Google
# manuel. Le script comptait des faux positifs.
#
# Solution adoptee : N1 (DataForSEO) reste un candidate filter. N2 (ce bloc)
# fetch le HTML de chaque candidat et verifie litteralement que la phrase
# apparait dans le texte rendu. Cache par URL pour amortir.

_WHITESPACE_RE = re.compile(r"\s+")
# Caracteres unicode "fancy" qui cassent un match exact entre l'export HTML
# d'un site (guillemets typographiques, apostrophes courbes, tirets cadratins)
# et la version brute du snippet. Tous normalises vers leur equivalent ASCII.
_FANCY_PUNCT_MAP = str.maketrans({
    "‘": "'", "’": "'",  # apostrophes courbes
    "“": '"', "”": '"',  # guillemets typographiques
    "«": '"', "»": '"',  # guillemets francais
    "–": "-", "—": "-",  # tirets demi/cadratin
    "…": "...",                # ellipsis
    " ": " ",                  # nbsp
})


def _normalize_for_match(text: str) -> str:
    """Lowercase + ASCII-fy fancy punctuation + collapse whitespace.

    Garantit qu'un snippet rendu en HTML par un CMS (guillemets typo, nbsp,
    apostrophes courbes) matche bien la version source meme si la copie a
    transite par un editeur WYSIWYG qui re-encode les caracteres."""
    if not text:
        return ""
    s = text.translate(_FANCY_PUNCT_MAP)
    s = s.lower()
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


def _html_to_text(html: str) -> str:
    """Extrait le texte d'une page HTML. Strip scripts/styles/noscript/header/
    footer/nav. Conserve le texte des balises restantes."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    # Ne pas supprimer header/footer/nav par defaut : la description produit
    # peut etre dans une div sans semantique claire, et ce niveau de strip
    # n'est pas necessaire au check de match. On extrait tout le body.
    return soup.get_text(separator=" ", strip=True)


# Mots vides ignores au calcul du fallback fuzzy. Garde seulement les mots
# significatifs >= 4 chars. Le fallback fuzzy n'est utilise que si le match
# exact normalise echoue (devrait etre rare).
_FUZZY_MIN_CONSECUTIVE_WORDS = 12


def _validate_match_in_html(
    candidate_url: str,
    snippet_normalized: str,
    html_cache: dict[str, str | None],
    timeout: int = 8,
) -> dict:
    """Niveau 2 : verifier que la phrase recherchee apparait litteralement
    dans le HTML de l'URL candidate.

    Retourne {"valid": bool, "reason": str, "url": str}.

    Logique :
    - Cache par URL (la meme URL peut etre candidate sur 2 snippets distincts
      via les SERP DataForSEO, surtout sur des catalogue similaires).
    - safe_get protege contre les SSRF (IPs privees, etc.).
    - Si HTTP error (404, 403, 5xx) ou timeout : invalid, raison loggee.
    - Match exact sur le texte normalise : si la phrase normalisee est dans
      le texte normalise du HTML, c'est valide.
    - Fallback fuzzy : si match exact echoue, decouper le snippet en mots
      significatifs (>= 4 chars), chercher une fenetre glissante de >= 12 mots
      consecutifs identiques dans le texte. Couvre les cas ou le site cible
      a tronque la phrase ou ajoute un mot au milieu.
    - Sinon : invalid, raison "phrase absente du HTML".
    """
    if candidate_url in html_cache:
        cached_html = html_cache[candidate_url]
        if cached_html is None:
            return {"valid": False, "reason": "fetch_error (cached)", "url": candidate_url}
    else:
        try:
            resp = safe_get(
                candidate_url,
                headers=default_headers(),
                timeout=timeout,
                allow_redirects=True,
                max_response_bytes=2 * 1024 * 1024,
            )
            if resp.status_code >= 400:
                html_cache[candidate_url] = None
                return {
                    "valid": False,
                    "reason": f"HTTP {resp.status_code}",
                    "url": candidate_url,
                }
            cached_html = resp.text
            html_cache[candidate_url] = cached_html
        except Exception as exc:
            html_cache[candidate_url] = None
            return {
                "valid": False,
                "reason": f"fetch_error: {type(exc).__name__}",
                "url": candidate_url,
            }

    page_text_normalized = _normalize_for_match(_html_to_text(cached_html))
    if not page_text_normalized:
        return {"valid": False, "reason": "empty_text", "url": candidate_url}

    # Match exact sur le texte normalise.
    if snippet_normalized in page_text_normalized:
        return {"valid": True, "reason": "exact_match", "url": candidate_url}

    # Fallback fuzzy : >= 12 mots consecutifs significatifs identiques.
    snippet_words = [w for w in snippet_normalized.split() if len(w) >= 4]
    if len(snippet_words) < _FUZZY_MIN_CONSECUTIVE_WORDS:
        return {"valid": False, "reason": "phrase_absent_short_snippet", "url": candidate_url}

    page_words = page_text_normalized.split()
    page_set_index: dict[str, list[int]] = {}
    for i, w in enumerate(page_words):
        page_set_index.setdefault(w, []).append(i)

    # Fenetre glissante : pour chaque position de depart du snippet, chercher
    # si une fenetre de N mots consecutifs apparait dans la page.
    n = _FUZZY_MIN_CONSECUTIVE_WORDS
    for start_pos in range(0, len(snippet_words) - n + 1):
        window = snippet_words[start_pos:start_pos + n]
        first_word = window[0]
        candidates_pos = page_set_index.get(first_word, [])
        for p in candidates_pos:
            if p + n > len(page_words):
                continue
            if page_words[p:p + n] == window:
                return {"valid": True, "reason": f"fuzzy_match_{n}_words", "url": candidate_url}
    return {"valid": False, "reason": "phrase_absent_fuzzy", "url": candidate_url}


def _parse_serp_response(payload: dict, audited_domain: str) -> dict:
    """Extract candidate items from a DataForSEO live advanced response.

    Refonte 29/05 : retourne maintenant la liste des items (domain + url)
    plutot que des domain_counts. La validation N2 (HTML brut) est faite
    en aval dans la boucle run(), parce qu'il faut acceder a l'URL exacte
    pour fetcher le HTML, pas juste le domaine."""
    result: dict = {"items": [], "raw_status": None}
    tasks = payload.get("tasks") or []
    if not tasks:
        result["raw_status"] = payload.get("status_message")
        return result
    task = tasks[0]
    result["raw_status"] = task.get("status_message")
    seen_urls: set[str] = set()
    for outer in task.get("result") or []:
        items = outer.get("items") or []
        for item in items:
            if item.get("type") != "organic":
                continue
            domain = (item.get("domain") or "").lower().lstrip("www.")
            url = item.get("url") or ""
            if not domain or domain == audited_domain or not url:
                continue
            if _is_excluded_domain(domain):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            result["items"].append({"domain": domain, "url": url})
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

    # Look up the CMS detected at scraping time. Try urls.json's own
    # cms_detected field first, then the sibling scraping.json / output.json
    # produced by the audit-prospect-ecommerce scraper.py.
    cms_hint: str | None = urls_meta.get("cms_detected") or None
    if not cms_hint:
        urls_dir = os.path.dirname(os.path.abspath(urls_file))
        for sibling in ("01-ECOM-scraping.json", "scraping.json", "output.json"):
            sibling_path = os.path.join(urls_dir, sibling)
            if os.path.exists(sibling_path):
                try:
                    with open(sibling_path, "r", encoding="utf-8") as f:
                        scraping = json.load(f)
                    candidate = scraping.get("cms") or scraping.get("cms_detected")
                    if candidate and candidate != "Autre" and candidate != "Non détecté":
                        cms_hint = candidate
                        break
                except (OSError, json.JSONDecodeError):
                    continue
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
    # Cache HTML partage entre toutes les URLs auditees. Si plusieurs fiches
    # BSD ont les memes concurrents dans leur SERP, on ne fetch chaque URL
    # tierce qu'une seule fois.
    html_cache: dict[str, str | None] = {}
    for entry in products:
        url = entry["url"]
        page = _fetch_snippet(url, snippet_len, timeout=timeout, cms_hint=cms_hint)
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
            # Compteurs N2 (validation HTML). Niveau 1 (SERP) = candidates.
            # Niveau 2 (fetch HTML + check phrase) = validated.
            "n2_total_candidates": 0,
            "n2_validated": 0,
            "n2_rejected": [],
        }
        if snippets_tried:
            # Per-domain count across snippets: a domain that appears in 2+
            # snippets is a much stronger signal of duplicated description
            # than a domain matched on a single snippet.
            #
            # Refonte 29/05 : chaque candidat SERP est valide via fetch HTML
            # avant d'etre compte dans domain_hits. Les candidats invalides
            # (phrase absente du HTML, HTTP error, timeout) sont jetes et
            # logges dans n2_rejected.
            domain_hits: dict[str, int] = {}
            matched_snippets = 0
            statuses = []
            for snip in snippets_tried:
                snip_normalized = _normalize_for_match(snip)
                try:
                    response = _dataforseo_serp(snip, login, password)
                    parsed = _parse_serp_response(response, audited_domain)
                    statuses.append(parsed["raw_status"] or "ok")
                    n1_candidates = parsed["items"]
                    record["n2_total_candidates"] += len(n1_candidates)
                    # Validation N2 : fetch HTML de chaque candidat, verifier
                    # presence litterale de la phrase. Seuls les valides
                    # comptent dans domain_hits.
                    validated_domains: set[str] = set()
                    for cand in n1_candidates:
                        check = _validate_match_in_html(
                            cand["url"], snip_normalized, html_cache,
                            timeout=timeout,
                        )
                        if check["valid"]:
                            validated_domains.add(cand["domain"])
                            record["n2_validated"] += 1
                        else:
                            record["n2_rejected"].append({
                                "domain": cand["domain"],
                                "url": cand["url"],
                                "snippet_excerpt": snip[:60],
                                "reason": check["reason"],
                            })
                    if validated_domains:
                        matched_snippets += 1
                    for d in validated_domains:
                        domain_hits[d] = domain_hits.get(d, 0) + 1
                    record["per_snippet_results"].append({
                        "snippet": snip[:80],
                        "n1_candidates": len(n1_candidates),
                        "n2_validated_domains": sorted(validated_domains)[:5],
                        "domains_count": len(validated_domains),
                        "sample_domains": sorted(validated_domains)[:5],
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
            # Garde-fou N2 : si > 50% des candidats SERP echouent au check
            # HTML brut, Google a probablement tolere les guillemets sur le
            # snippet (longue chaine, mots-cles communs). Signal de
            # tolerance: le compteur N1 etait gonfle par des faux positifs.
            # On expose la statistique sans changer le finding (le finding
            # est deja base sur N2 valide), mais on la log dans le rapport.
            n2_total = record["n2_total_candidates"]
            n2_valid = record["n2_validated"]
            if n2_total > 0:
                n2_rejection_ratio = (n2_total - n2_valid) / n2_total
                record["n2_rejection_ratio"] = round(n2_rejection_ratio, 2)
                if n2_rejection_ratio > 0.5:
                    record["n2_warning"] = (
                        f"Forte tolerance Google : {n2_total - n2_valid}/{n2_total} "
                        f"candidats SERP rejetes au check HTML brut. Le compteur "
                        f"N1 etait gonfle par des faux positifs (phrase tronquee "
                        f"ou tolerance guillemets sur snippet long)."
                    )

            # SUSPECT requires recurrent domains. A domain matched on a single
            # snippet is almost always noise — a generic phrase or product name
            # that happens to be shared. Real duplication is when the SAME
            # domain matches across 2+ snippets — that means it carries enough
            # of our text to be considered copied (or copying us).
            #
            # Total unique count alone is unreliable: tested on BSD with 17
            # unique single-hit domains (all noise, 0 recurrent) → would have
            # been a false positive. Switched to recurrent-only signal.
            record["manufacturer_copy_suspect"] = len(recurrent) > 0
            if record["manufacturer_copy_suspect"]:
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

            recurrent_str = ", ".join(
                f"{r['domain']} ({r['matched_snippets']} snippets)" for r in recurrent[:5]
            )
            other_sample = ", ".join(record.get("external_domains_sample") or [])

            # Recurrent count drives the severity. Single recurrent on 2 of 3
            # snippets is a clear shared-text signal — warning. 2+ recurrent
            # is overwhelming evidence of broad sharing — still warning, but
            # with stronger wording in the finding.
            severity = "warning"
            many_recurrent = len(recurrent) >= 2

            issues.append({
                "severity": severity,
                "area": "manufacturer_dup_check",
                "finding": (
                    f"Description produit : {unique_count} domaine(s) tiers reprennent au moins "
                    f"une phrase de votre fiche. Parmi eux, {len(recurrent)} domaine(s) "
                    f"récurrent(s) partagent ≥2 phrases distinctes — signal robuste de "
                    f"duplication. URL : {record['url']}"
                ),
                "evidence": (
                    f"Domaines récurrents : {recurrent_str}. "
                    f"Échantillon des autres domaines (1 phrase commune) : {other_sample[:200]}. "
                    f"Intensité {intensity} ({matched}/{total} snippets matchés). "
                    f"Source du texte analysé : {record.get('text_source', '?')}."
                ),
                "fix": (
                    "Deux interprétations possibles :\n"
                    "(a) Vous avez repris la description d'origine (fabricant, distributeur "
                    "officiel, autre revendeur). Dans ce cas, réécrire avec un angle unique "
                    "apporte un avantage SEO : cas d'usage, comparaison, conseil installation, "
                    "retour client.\n"
                    "(b) Un site externe a copié votre contenu. Vous pouvez demander le retrait "
                    "par email au webmaster du site copieur. Si la copie est manifeste et le "
                    "site ne répond pas, signalement DMCA ou formulaire Google « Contenu "
                    "dupliqué ». Pas d'autre action SEO directe.\n"
                    "Méthode pour trancher : ouvrir l'URL d'un domaine récurrent et comparer "
                    "le contenu mot à mot avec votre fiche."
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
