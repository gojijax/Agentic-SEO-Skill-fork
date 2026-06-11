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
# F88 (11/06) : endpoints Standard Queue (task_post / tasks_ready /
# task_get) pour passer de $0.002/appel (Live) a $0.0006/appel
# (Standard, 5 min de latence). Sur un audit en background qui tourne
# 30-60 min, 5 min de plus est imperceptible.
DATAFORSEO_TASK_POST = "https://api.dataforseo.com/v3/serp/google/organic/task_post"
DATAFORSEO_TASKS_READY = "https://api.dataforseo.com/v3/serp/google/organic/tasks_ready"
DATAFORSEO_TASK_GET = "https://api.dataforseo.com/v3/serp/google/organic/task_get/advanced"

# F88 (11/06) : blocklist des marketplaces et sites d'annonces qui
# republient verbatim le texte fabricant. Faux positifs structurels :
# une fiche revendeur qui copie le fabricant matche aussi sur eBay /
# NaturaBuy parce que le vendeur copie aussi le fabricant. Ce n'est
# pas un signal de duplication concurrentielle utile.
MARKETPLACE_BLOCKLIST = frozenset({
    "naturabuy.fr",
    "ebay.fr", "ebay.com",
    "leboncoin.fr",
    "amazon.fr", "amazon.com",
    "cdiscount.com",
    "rakuten.fr", "rakuten.com",
    "fnac.com",
    "vinted.fr",
    "aliexpress.com", "aliexpress.fr",
})

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
DEFAULT_MAX_URLS = 20  # Etendu 01/06 (avant : 5) : couple avec sample
                       # ECOM passe a 10+10 fiches. Permet une vraie
                       # gradation severity (10+/20 = Critical) au lieu
                       # d'un sample de 5 qui plafonnait a Important.
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
    d = domain.lower().removeprefix("www.")
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


# F88 (11/06) : Standard Queue (priority=1, ~5min de latence) au lieu
# de Live Mode (~6s) — divise le cout par 3 ($0.0006 vs $0.002 par
# appel). Mode batch : POST tous les snippets d'un coup au debut,
# attendre que les tasks soient pretes, GET les resultats en bloc.

def _dataforseo_creds_header(login: str, password: str) -> dict:
    creds = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


def _dataforseo_task_post_batch(
    snippets: list[str], login: str, password: str, timeout: int = 30,
) -> list:
    """POST un batch de tasks Standard Queue. Retourne la liste des
    task_ids (un par snippet, dans l'ordre). Si une task ne se cree
    pas, l'entree est None.

    DataForSEO accepte jusqu'a 100 tasks par requete POST.
    """
    headers = _dataforseo_creds_header(login, password)
    task_ids: list = []
    # Chunk de 100 par appel POST (limite API DataForSEO).
    for i in range(0, len(snippets), 100):
        chunk = snippets[i:i + 100]
        body = [
            {
                "language_code": "fr",
                "location_code": 2250,
                "keyword": f'"{s}"',
                "depth": 20,
                "device": "desktop",
                "priority": 1,
            }
            for s in chunk
        ]
        resp = safe_post(DATAFORSEO_TASK_POST, headers=headers, json=body, timeout=timeout)
        payload = resp.json()
        top_code = payload.get("status_code")
        if resp.status_code >= 400 or (isinstance(top_code, int) and top_code >= 40000):
            raise RuntimeError(
                f"DataForSEO task_post HTTP {resp.status_code} / status_code "
                f"{top_code}: {payload.get('status_message')}"
            )
        for task in payload.get("tasks") or []:
            tcode = task.get("status_code")
            if isinstance(tcode, int) and tcode >= 40000:
                task_ids.append(None)
            else:
                task_ids.append(task.get("id"))
    return task_ids


def _dataforseo_tasks_ready_set(login: str, password: str, timeout: int = 30) -> set:
    """Retourne l'ensemble des task_ids disponibles pour retrieval."""
    headers = _dataforseo_creds_header(login, password)
    resp = safe_get(DATAFORSEO_TASKS_READY, headers=headers, timeout=timeout)
    payload = resp.json()
    ready: set = set()
    for outer in payload.get("tasks") or []:
        for item in outer.get("result") or []:
            tid = item.get("id")
            if tid:
                ready.add(tid)
    return ready


def _dataforseo_task_get(task_id: str, login: str, password: str, timeout: int = 30) -> dict:
    """GET le resultat d'une task SERP standard queue."""
    headers = _dataforseo_creds_header(login, password)
    url = f"{DATAFORSEO_TASK_GET}/{task_id}"
    resp = safe_get(url, headers=headers, timeout=timeout)
    return resp.json()


def _dataforseo_wait_and_collect(
    task_ids: list,
    login: str,
    password: str,
    poll_interval: int = 30,
    max_wait: int = 1800,
    log_progress: bool = True,
) -> dict:
    """Poll /tasks_ready jusqu'a ce que tous les task_ids soient pretes
    (ou que max_wait soit atteint), puis GET chaque resultat.

    Retourne un dict {task_id: serp_response_payload}. Les tasks qui
    n'ont pas pu etre fetchees apparaissent avec valeur None.
    """
    pending = set(t for t in task_ids if t)
    if not pending:
        return {}
    import time as _time
    elapsed = 0
    ready_collected: set = set()
    results: dict = {}
    while elapsed < max_wait and pending - ready_collected:
        try:
            ready = _dataforseo_tasks_ready_set(login, password)
        except Exception as exc:
            if log_progress:
                print(f"[manufacturer_dup_check] tasks_ready poll failed: {exc}", file=sys.stderr)
            ready = set()
        newly = (ready & pending) - ready_collected
        for tid in newly:
            try:
                results[tid] = _dataforseo_task_get(tid, login, password)
            except Exception as exc:
                if log_progress:
                    print(f"[manufacturer_dup_check] task_get {tid} failed: {exc}", file=sys.stderr)
                results[tid] = None
            ready_collected.add(tid)
        if pending - ready_collected:
            if log_progress:
                remaining = len(pending - ready_collected)
                print(
                    f"[manufacturer_dup_check] standard queue: {len(ready_collected)}/{len(pending)} ready, "
                    f"{remaining} pending, +{elapsed}s",
                    file=sys.stderr,
                )
            _time.sleep(poll_interval)
            elapsed += poll_interval
    # Tasks restantes non pretes : on essaie quand meme le task_get
    # (peut-etre dispo malgre la non-apparition dans tasks_ready).
    for tid in pending - ready_collected:
        try:
            results[tid] = _dataforseo_task_get(tid, login, password)
        except Exception:
            results[tid] = None
    return results


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

    # Fallback fuzzy : decoupe le snippet en tokens significatifs et
    # cherche une fenetre du HTML candidat qui contient >= TOKEN_OVERLAP_RATIO
    # de ces tokens. Tolere le paraphrasing fabricant (1-2 mots changes,
    # ordre legerement different) que le check 12-mots-consecutifs ratait.
    # Cas observe Beaurepaire 11/06 : browning.eu remonte par Google comme
    # candidat sur la phrase Beaurepaire mais rejete en fuzzy strict.
    snippet_words = [w for w in snippet_normalized.split() if len(w) >= 4]
    if len(snippet_words) < 6:
        return {"valid": False, "reason": "phrase_absent_short_snippet", "url": candidate_url}

    page_words = page_text_normalized.split()
    if not page_words:
        return {"valid": False, "reason": "empty_page", "url": candidate_url}

    # F88 (11/06) : token-overlap dans une fenetre glissante.
    # - taille fenetre = 1.5 * len(snippet_words) pour absorber les insertions
    # - seuil = 70% des tokens du snippet presents dans la fenetre
    snippet_token_set = set(snippet_words)
    window_size = max(int(len(snippet_words) * 1.5), len(snippet_words))
    threshold = max(int(len(snippet_words) * 0.70), 5)

    # Index inverse : pour chaque token, ses positions dans la page.
    page_set_index: dict[str, list[int]] = {}
    for i, w in enumerate(page_words):
        if w in snippet_token_set:
            page_set_index.setdefault(w, []).append(i)

    # Toutes les positions ou un token du snippet apparait, ordonnees.
    hit_positions = sorted({p for ps in page_set_index.values() for p in ps})
    if len(hit_positions) < threshold:
        return {
            "valid": False,
            "reason": f"phrase_absent_fuzzy (overlap {len(hit_positions)}/{len(snippet_words)})",
            "url": candidate_url,
        }

    # Pour chaque position de depart, compter combien de tokens distincts
    # du snippet apparaissent dans la fenetre [p, p+window_size].
    for start in hit_positions:
        end = start + window_size
        tokens_in_window = set()
        for p in hit_positions:
            if p < start:
                continue
            if p >= end:
                break
            tokens_in_window.add(page_words[p])
        if len(tokens_in_window) >= threshold:
            return {
                "valid": True,
                "reason": f"fuzzy_overlap_{len(tokens_in_window)}/{len(snippet_words)}",
                "url": candidate_url,
            }
    return {
        "valid": False,
        "reason": f"phrase_absent_fuzzy (max overlap in window < {threshold})",
        "url": candidate_url,
    }


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
            domain = (item.get("domain") or "").lower().removeprefix("www.")
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


def _select_random_crawl_products(
    crawl_pages_file: str,
    urls_meta: dict,
    sample_size: int,
) -> list:
    """F88 (11/06) : selectionne `sample_size` fiches produit aleatoires
    depuis le crawl complet, en excluant celles deja presentes dans
    Haloscan (urls_meta) qui rankent deja sur des requetes commerciales.

    Le but est d'avoir un echantillon representatif du long-tail du
    catalogue, plus pertinent pour detecter du duplicate descriptions
    que les top fiches qui performent deja malgre tout.

    Format attendu de 04-CRAWL-pages.jsonl : 1 JSON par ligne avec
    au moins {url, status_code, content_type (ou indexable)}.
    """
    import random
    halo_urls = {
        e.get("url") for e in (urls_meta.get("urls") or [])
        if isinstance(e, dict) and e.get("haloscan_source") == "best_pages"
    }
    audited_domain = (urls_meta.get("domain") or "").lower().removeprefix("www.")
    # Heuristique URL produit pour les CMS courants (PrestaShop, Shopify,
    # WooCommerce, custom). On filtre sur les patterns qui sortent
    # presque toujours d'une fiche produit. Le crawl peut etre tres
    # bruite (categories, pages techniques, etc.).
    product_url_patterns = [
        re.compile(r"/produit[s]?/", re.IGNORECASE),
        re.compile(r"/product[s]?/", re.IGNORECASE),
        re.compile(r"-p\d+\.html?$", re.IGNORECASE),
        re.compile(r"-pid-\d+", re.IGNORECASE),
        re.compile(r"/article[s]?/", re.IGNORECASE),
        re.compile(r"-f\d+\.html?$", re.IGNORECASE),  # PrestaShop fiche
        re.compile(r"-\d{3,}-?\d*\.html?$"),           # PrestaShop product ID
    ]
    candidates: list = []
    try:
        with open(crawl_pages_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    page = json.loads(line)
                except json.JSONDecodeError:
                    continue
                url = page.get("url")
                if not url or url in halo_urls:
                    continue
                status = page.get("status_code") or page.get("status")
                if status and isinstance(status, int) and (status < 200 or status >= 400):
                    continue
                netloc = urlparse(url).netloc.lower().removeprefix("www.")
                if audited_domain and netloc != audited_domain:
                    continue
                if not any(p.search(url) for p in product_url_patterns):
                    continue
                candidates.append({"url": url, "type": "product_crawl_sample"})
    except OSError:
        return []
    if not candidates:
        return []
    # Tirage aleatoire pseudo-deterministe par seed du domaine (audit
    # reproductible). Si reproductibilite non requise, utiliser random.
    rng = random.Random(audited_domain or "fallback")
    rng.shuffle(candidates)
    return candidates[:sample_size]


def run(urls_file: str, max_urls: int, snippet_len: int, threshold: int,
        timeout: int = 12, crawl_pages_file: str = None,
        sample_size: int = 50, use_standard_queue: bool = True) -> dict:
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

    audited_domain = (urls_meta.get("domain") or "").lower().removeprefix("www.")

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
                audited_domain = urlparse(entry["url"]).netloc.lower().removeprefix("www.")
                break

    # F88 (11/06) : selection des fiches a scanner.
    # - Si crawl_pages_file fourni : sample aleatoire de `sample_size`
    #   fiches du crawl (en excluant celles deja dans Haloscan best_pages).
    #   Plus pertinent pour detecter le duplicate sur le long-tail.
    # - Sinon : fallback historique = top `max_urls` fiches d'urls.json
    #   (qui rankent deja sur Haloscan).
    if crawl_pages_file and os.path.exists(crawl_pages_file):
        products = _select_random_crawl_products(
            crawl_pages_file, urls_meta, sample_size,
        )
        if not products:
            print(
                f"[manufacturer_dup_check] crawl sample empty, fallback "
                f"sur urls.json",
                file=sys.stderr,
            )
            products = [
                entry for entry in urls_meta.get("urls", [])
                if isinstance(entry, dict)
                and entry.get("type") in PRODUCT_TYPES
                and entry.get("url")
            ][:max_urls]
    else:
        products = [
            entry for entry in urls_meta.get("urls", [])
            if isinstance(entry, dict)
            and entry.get("type") in PRODUCT_TYPES
            and entry.get("url")
        ][:max_urls]
    if not products:
        return {"skipped": True, "reason": "no product URLs found", "checked": []}

    checked: list[dict] = []
    suspect_count = 0
    # Cache HTML partage entre toutes les URLs auditees. Si plusieurs fiches
    # BSD ont les memes concurrents dans leur SERP, on ne fetch chaque URL
    # tierce qu'une seule fois.
    html_cache: dict[str, str | None] = {}

    # F88 (11/06) : pre-pass pour collecter tous les snippets de toutes
    # les fiches, puis POST en batch Standard Queue si active. Le mapping
    # snippet -> SERP response est utilise dans la boucle principale au
    # lieu d'un appel synchrone par snippet.
    print(
        f"[manufacturer_dup_check] phase 1/3 : fetch {len(products)} fiches "
        f"+ extraction snippets...",
        file=sys.stderr,
    )
    fetched_pages = []
    for entry in products:
        page = _fetch_snippet(
            entry["url"], snippet_len, timeout=timeout, cms_hint=cms_hint,
        )
        fetched_pages.append((entry, page))

    snippet_to_response: dict = {}
    if use_standard_queue:
        # Collecter tous les snippets uniques de toutes les fiches.
        all_snippets_set: set = set()
        for _, page in fetched_pages:
            for s in (page.get("snippets") or []):
                if s:
                    all_snippets_set.add(s)
            if page.get("snippet"):
                all_snippets_set.add(page["snippet"])
        all_snippets = sorted(all_snippets_set)
        if all_snippets:
            print(
                f"[manufacturer_dup_check] phase 2/3 : Standard Queue, POST "
                f"{len(all_snippets)} snippets uniques ($0.0006/snippet, latence ~5min)...",
                file=sys.stderr,
            )
            try:
                task_ids = _dataforseo_task_post_batch(
                    all_snippets, login, password,
                )
                snippet_to_task = {
                    s: t for s, t in zip(all_snippets, task_ids) if t
                }
                print(
                    f"[manufacturer_dup_check] {len(snippet_to_task)}/{len(all_snippets)} "
                    f"tasks posted, polling tasks_ready...",
                    file=sys.stderr,
                )
                task_results = _dataforseo_wait_and_collect(
                    list(snippet_to_task.values()),
                    login, password,
                    poll_interval=30, max_wait=1800,
                )
                snippet_to_response = {
                    snip: task_results.get(tid)
                    for snip, tid in snippet_to_task.items()
                }
                ready_count = sum(1 for v in snippet_to_response.values() if v)
                print(
                    f"[manufacturer_dup_check] phase 2/3 done : "
                    f"{ready_count}/{len(snippet_to_task)} snippets recoltes",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(
                    f"[manufacturer_dup_check] Standard Queue failed, fallback "
                    f"sur Live Mode synchrone : {exc}",
                    file=sys.stderr,
                )
                snippet_to_response = {}

    print(
        f"[manufacturer_dup_check] phase 3/3 : N2 validation HTML des "
        f"candidats SERP...",
        file=sys.stderr,
    )
    for entry, page in fetched_pages:
        url = entry["url"]
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
                    # F88 (11/06) : si le batch Standard Queue a pre-recolte
                    # une reponse pour ce snippet, l'utiliser. Sinon, retomber
                    # sur l'appel synchrone Live Mode.
                    response = snippet_to_response.get(snip)
                    if response is None:
                        response = _dataforseo_serp(snip, login, password)
                    parsed = _parse_serp_response(response, audited_domain)
                    statuses.append(parsed["raw_status"] or "ok")
                    n1_candidates = parsed["items"]
                    record["n2_total_candidates"] += len(n1_candidates)
                    # Validation N2 : fetch HTML de chaque candidat, verifier
                    # presence litterale de la phrase. Seuls les valides
                    # comptent dans domain_hits.
                    validated_domains: set[str] = set()
                    validated_urls_by_domain: dict = {}
                    for cand in n1_candidates:
                        # F88 (11/06) : exclure les marketplaces et sites
                        # d'annonces qui republient le texte fabricant
                        # verbatim. Faux positifs structurels sans valeur
                        # de signal concurrentiel.
                        if cand["domain"].lower() in MARKETPLACE_BLOCKLIST:
                            record["n2_rejected"].append({
                                "domain": cand["domain"],
                                "url": cand["url"],
                                "snippet_excerpt": snip[:60],
                                "reason": "marketplace_blocklist",
                            })
                            continue
                        check = _validate_match_in_html(
                            cand["url"], snip_normalized, html_cache,
                            timeout=timeout,
                        )
                        if check["valid"]:
                            validated_domains.add(cand["domain"])
                            # F87 (11/06) : conserver l'URL exacte du
                            # candidat valide. Avant on ne gardait que
                            # le domaine, ce qui forcait le builder a
                            # mettre la home du concurrent dans le
                            # finding ("partage avec naturabuy.fr") au
                            # lieu de l'URL precise de la page tierce.
                            validated_urls_by_domain.setdefault(
                                cand["domain"], []
                            ).append(cand["url"])
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
                        # F87 : accumuler les URLs validees par domaine
                        # au niveau du record, pour pouvoir les afficher
                        # dans le finding consolide.
                        record.setdefault("validated_urls_by_domain", {})
                        record["validated_urls_by_domain"].setdefault(d, [])
                        for u in validated_urls_by_domain.get(d, []):
                            if u not in record["validated_urls_by_domain"][d]:
                                record["validated_urls_by_domain"][d].append(u)
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
            # F87 (11/06) : exposer les URLs exactes des pages tierces
            # validees, par domaine recurrent. Permet au finding
            # consolide de pointer vers l'URL precise de la fiche
            # concurrente / fabricant au lieu du seul nom de domaine.
            validated_urls_map = record.get("validated_urls_by_domain") or {}
            record["recurrent_domains"] = [
                {
                    "domain": d,
                    "matched_snippets": domain_hits[d],
                    "urls": validated_urls_map.get(d, [])[:5],
                }
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

            # F87 (11/06) : structurer les URLs tierces par domaine
            # pour permettre au builder de citer l'URL precise de la
            # page concurrente / fabricant au lieu du seul nom de
            # domaine. La liste est limitee a 3 URLs par domaine pour
            # eviter de polluer l'evidence.
            external_urls_by_domain = {
                rd["domain"]: rd.get("urls", [])[:3]
                for rd in record.get("recurrent_domains", [])
            }
            issues.append({
                "severity": severity,
                "area": "manufacturer_dup_check",
                # F58e-bis (09/06) : URL retiree du texte finding et
                # mise dans _urls. Avant, le finding contenait
                # "URL : {url}" en fin de phrase, ce qui faisait
                # tronquer l'URL a "URL : ht..." cote builder
                # bhuna_actions_builder (qui cap le title a 200 chars).
                # Maintenant, l'URL est dans _urls qui est lu par le
                # builder pour remplir page_concernee/pages_concerned_sample
                # correctement. Le texte finding ne mentionne plus l'URL
                # directement (la fiche concernee est visible dans
                # l'echantillon de pages cote Notion).
                "finding": (
                    f"Description produit reprise sur {unique_count} site(s) tiers. "
                    f"Parmi eux, {len(recurrent)} site(s) recurrent(s) "
                    f"partagent au moins 2 phrases distinctes, signal robuste "
                    "de duplication."
                ),
                "_urls": [record["url"]],
                "_external_urls_by_domain": external_urls_by_domain,
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
    # F88 (11/06) : nouveaux flags pour la selection 50 fiches crawl
    # aleatoires et le mode Standard Queue ($0.0006 vs $0.002 par appel).
    parser.add_argument(
        "--crawl-pages-file", default=None,
        help="Path to 04-CRAWL-pages.jsonl. Si fourni, sample aleatoire de "
             "--sample-size fiches du crawl (en excluant les top fiches "
             "Haloscan deja en best_pages). Si absent, comportement historique "
             "(top --max-urls fiches d'urls.json).",
    )
    parser.add_argument(
        "--sample-size", type=int, default=50,
        help="Taille du sample aleatoire crawl (default: 50). Ignore si "
             "--crawl-pages-file n'est pas fourni.",
    )
    parser.add_argument(
        "--use-live", action="store_true",
        help="Force Live Mode (synchrone, ~6s/appel, $0.002). Par defaut "
             "Standard Queue (batch async, ~5min, $0.0006/appel).",
    )
    args = parser.parse_args()

    result = run(
        args.urls_file, args.max_urls, args.snippet_length, args.threshold,
        crawl_pages_file=args.crawl_pages_file,
        sample_size=args.sample_size,
        use_standard_queue=not args.use_live,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(payload)
            f.write("\n")
        print(f"Wrote {args.output}")
    else:
        # Fix 01/06 apres Meyson : print(payload) plante sur console Windows
        # cp1252 quand le payload contient des caracteres unicode hors
        # latin-1 (caracteres special, emoji, etc. dans les titres
        # produits scrapes). On force l'output en UTF-8 + fallback "?"
        # sur les caracteres non-encodable, et on flush sur stdout.buffer
        # pour bypass le wrapping cp1252 par defaut.
        import sys as _sys
        try:
            _sys.stdout.buffer.write(payload.encode("utf-8") + b"\n")
            _sys.stdout.flush()
        except (AttributeError, OSError):
            # Fallback : encode + decode avec replace pour eviter le crash
            print(payload.encode("utf-8", errors="replace").decode("utf-8", errors="replace"))


if __name__ == "__main__":
    main()
