"""
scraper.py — ekantipur.com Playwright scraper  
==========================================================
Extracts:
  1. Top 5 Entertainment (मनोरञ्जन) news articles
  2. Cartoon of the Day (व्यंग्यचित्र)

Fixes over v1:
  - Added DOM-dump diagnostic so selector mismatches are immediately visible
  - Article card selector now uses a live DOM-inspection pass first
  - Image resolution handles the ekantipur CDN proxy URL pattern:
    https://assets-cdn-api.ekantipur.com/thumb.php?src=...
  - Cartoon extraction uses JavaScript evaluate() to search by Nepali text
  - networkidle wrapped everywhere (can time-out on ad-heavy pages)
  - Added page.evaluate() scroll to trigger lazy-load before extraction
  - Author regex fixed to cover Devanagari dash characters

Run:
    pip install playwright
    playwright install chromium
    python scraper.py
"""

import json
import re
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL          = "https://ekantipur.com"
ENTERTAINMENT_URL = f"{BASE_URL}/entertainment"
OUTPUT_FILE       = "output.json"
DEFAULT_TIMEOUT   = 25_000   # ms


# ─────────────────────────────────────────────────────────────────
# DOM Debug Helper
# ─────────────────────────────────────────────────────────────────

def dump_dom_snapshot(page, label: str, max_chars: int = 6000):
    """
    Writes a trimmed HTML snapshot to disk for manual inspection.
    Call this when selectors return nothing — open the file in a browser
    or text editor to see what Playwright actually rendered.
    """
    try:
        html = page.content()
        snapshot_file = f"debug_snapshot_{label}.html"
        with open(snapshot_file, "w", encoding="utf-8") as f:
            f.write(html[:max_chars])
        print(f"[DEBUG] DOM snapshot -> {snapshot_file}  ({len(html)} total chars)")
    except Exception as e:
        print(f"[DEBUG] Snapshot write failed: {e}")


def detect_card_selector(page) -> tuple[str, list]:
    """
    Introspect the live DOM to find which CSS selector matches article cards.

    Tries ekantipur-specific patterns first, falls back to generic ones.
    If nothing matches it writes a debug snapshot so you can inspect the DOM.

    Returns (winning_selector, list_of_elements).
    """
    candidates = [
        # ── ekantipur-specific (reverse-engineered from CDN URL / page structure) ──
        ".normal-news .news-post",
        ".lead-news .news-post",
        ".news-post",
        ".post-item",
        ".news-item",
        ".article-list .article-item",
        ".category-news .news-card",
        ".news-card",
        # ── generic fallbacks ──
        "article.normal-news-item",
        "article",
        "div.news-item",
        "li.news-item",
    ]

    for sel in candidates:
        try:
            els = page.query_selector_all(sel)
            if els:
                print(f"[INFO] Card selector matched: '{sel}' -> {len(els)} elements")
                return sel, els
        except Exception:
            continue

    print("[WARN] No card selector matched - writing DOM snapshot for diagnosis")
    dump_dom_snapshot(page, "entertainment")
    return "", []


# ─────────────────────────────────────────────────────────────────
# Safe extraction helpers
# ─────────────────────────────────────────────────────────────────

def safe_text(element, selector: str) -> str | None:
    """Return stripped inner-text of first CSS match, or None."""
    try:
        el = element.query_selector(selector)
        if el:
            text = el.inner_text().strip()
            return text if text else None
    except Exception:
        pass
    return None


def safe_attr(element, selector: str, attr: str) -> str | None:
    """Return attribute value of first CSS match (resolves relative URLs), or None."""
    try:
        el = element.query_selector(selector)
        if el:
            val = el.get_attribute(attr)
            if val:
                val = val.strip()
                if attr in ("src", "href") and val.startswith("/"):
                    val = BASE_URL + val
                return val
    except Exception:
        pass
    return None


def img_element_src(img) -> str | None:
    """Resolve image URL from a Playwright ElementHandle <img> (lazy-load aware)."""
    if not img:
        return None
    try:
        for attr in ("data-src", "data-lazy-src", "data-original"):
            val = img.get_attribute(attr)
            if val and val.strip():
                v = val.strip()
                if "placeholder" in v and "thumb.php" not in v:
                    continue
                return v if v.startswith("http") else BASE_URL + v

        srcset = img.get_attribute("srcset")
        if srcset:
            first = srcset.split(",")[0].split()[0].strip()
            if first and "placeholder" not in first:
                return first if first.startswith("http") else BASE_URL + first

        src = img.get_attribute("src")
        if src and src.strip():
            s = src.strip()
            if "placeholder" in s and "thumb.php" not in s:
                return None
            return s if s.startswith("http") else BASE_URL + s
    except Exception:
        pass
    return None


def resolve_img_src(element) -> str | None:
    """
    ekantipur lazy-loads images.  Attribute priority:
      1. data-src             (lazysizes pattern)
      2. data-lazy-src
      3. data-original
      4. srcset first token
      5. src

    Also correctly handles the CDN proxy pattern used by ekantipur:
      https://assets-cdn-api.ekantipur.com/thumb.php?src=<real_url>&w=601&h=0
    These are real, usable URLs — NOT placeholders — so we return them as-is.

    Falls back to background-image inline style for image-less cards.
    """
    img = element.query_selector("img")
    if not img:
        # Some cards use CSS background-image instead of <img>
        try:
            style = element.get_attribute("style") or ""
            m = re.search(r'url\(["\']?(https?://[^"\')\s]+)["\']?\)', style)
            if m:
                return m.group(1)
        except Exception:
            pass
        return None

    return img_element_src(img)


def cartoonist_from_img_alt(alt: str | None) -> str | None:
    """
    Cartoon carousel <img alt> text is like:
      'कान्तिपुर दैनिकमा आज प्रकाशित अविनको कार्टुन'
    The cartoonist name is the Devanagari word immediately before 'को कार्टुन'.
    """
    if not alt or not alt.strip():
        return None
    parts = re.findall(r"([\u0900-\u097F]+)को\s*कार्टुन", alt.strip())
    return parts[-1] if parts else None


def clean_author(raw: str | None) -> str | None:
    """
    Normalise author strings.
    Returns None for whitespace-only / pure-punctuation / empty strings.
    Covers ASCII and common Devanagari separator characters.
    """
    if not raw:
        return None
    raw = raw.strip()
    # Author blocks on article pages often include date + author in multiple lines.
    # Keep the last non-empty line, which is typically the byline value.
    if "\n" in raw:
        parts = [p.strip() for p in raw.splitlines() if p.strip()]
        if parts:
            raw = parts[-1]
    # Unicode ranges: \u2013 (en-dash), \u2014 (em-dash), \u0964 (Devanagari danda)
    if re.fullmatch(r"[\s\u2013\u2014\u0964\-|/]*", raw):
        return None
    return raw


def extract_links_from_page(page) -> list[str]:
    """
    Collect unique entertainment article URLs from the current listing page.
    This is a robust fallback when card selectors drift.
    """
    try:
        links = page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const normalize = (u) => {
                    if (!u) return null;
                    const clean = u.split('#')[0].trim();
                    return clean.endsWith('/') ? clean.slice(0, -1) : clean;
                };

                for (const a of document.querySelectorAll('a[href]')) {
                    const href = a.href || a.getAttribute('href');
                    if (!href) continue;
                    if (!href.includes('/entertainment/')) continue;
                    if (!href.startsWith('http')) continue;
                    if (href.includes('/author/')) continue;
                    if (href.includes('/category/')) continue;
                    const n = normalize(href);
                    if (n && !seen.has(n)) {
                        seen.add(n);
                        out.push(n);
                    }
                }
                return out;
            }"""
        )
        return links or []
    except Exception:
        return []


def parse_ldjson_candidates(page) -> list[dict]:
    """
    Try to read entertainment candidates from application/ld+json on listing page.
    """
    try:
        items = page.evaluate(
            """() => {
                const results = [];
                const scripts = document.querySelectorAll('script[type="application/ld+json"]');

                const pushItem = (obj) => {
                    if (!obj || typeof obj !== 'object') return;
                    const url = obj.url || obj.mainEntityOfPage?.['@id'] || obj.mainEntityOfPage;
                    const headline = obj.headline || obj.name || null;
                    if (!url || !headline) return;
                    if (!String(url).includes('/entertainment/')) return;
                    results.push({
                        url: String(url),
                        title: String(headline).trim(),
                        author: obj.author?.name || obj.author?.[0]?.name || null,
                        image_url: obj.image?.url || obj.image?.[0]?.url || obj.image || null
                    });
                };

                for (const s of scripts) {
                    try {
                        const data = JSON.parse(s.textContent || '');
                        if (Array.isArray(data)) {
                            for (const it of data) pushItem(it);
                        } else if (data?.['@graph'] && Array.isArray(data['@graph'])) {
                            for (const it of data['@graph']) pushItem(it);
                        } else {
                            pushItem(data);
                            if (Array.isArray(data?.itemListElement)) {
                                for (const it of data.itemListElement) {
                                    pushItem(it.item || it);
                                }
                            }
                        }
                    } catch (_) {}
                }
                return results;
            }"""
        )
        return items or []
    except Exception:
        return []


def extract_article_detail(page, url: str) -> dict | None:
    """
    Open article URL and extract title/author/image with multiple fallbacks.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        try:
            page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass

        title = (
            safe_text(page, "h1")
            or safe_text(page, ".article-title")
            or safe_text(page, ".news-title")
            or safe_attr(page, "meta[property='og:title']", "content")
        )
        author = clean_author(
            safe_text(page, ".author-name")
            or safe_text(page, ".author a")
            or safe_text(page, ".author")
            or safe_text(page, ".byline .name")
            or safe_text(page, "[class*='author']")
            or safe_attr(page, "meta[name='author']", "content")
        )
        image_url = (
            safe_attr(page, "meta[property='og:image']", "content")
            or safe_attr(page, "meta[name='twitter:image']", "content")
        )

        if not title:
            return None

        return {
            "title": title,
            "image_url": image_url,
            "category": "मनोरञ्जन",
            "author": author,
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────
# Task 1 — Entertainment News
# ─────────────────────────────────────────────────────────────────

def scrape_entertainment_news(page) -> list[dict]:
    """Navigate to /entertainment and extract the top 5 article cards."""
    print(f"\n[INFO] Navigating to entertainment section -> {ENTERTAINMENT_URL}")
    page.goto(ENTERTAINMENT_URL, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)

    # Wait for any content block
    try:
        page.wait_for_selector(
            "article, .news-post, .post-item, .news-item, .news-card, [class*='news']",
            timeout=DEFAULT_TIMEOUT,
        )
    except PlaywrightTimeoutError:
        print("[WARN] Initial selector wait timed out - continuing")

    # Let XHR/JS settle; ignore timeout on ad-heavy pages
    try:
        page.wait_for_load_state("networkidle", timeout=12_000)
    except PlaywrightTimeoutError:
        print("[WARN] networkidle timeout - page still usable")

    # Scroll to trigger lazy image loading
    page.evaluate("window.scrollBy(0, 800)")
    time.sleep(1.2)

    # ── Discover correct card selector for this page ──
    _sel, cards = detect_card_selector(page)
    if not cards:
        print("[WARN] No article cards found - trying link/article fallback")
        links = extract_links_from_page(page)
        if not links:
            print("[WARN] No entertainment links found - trying ld+json fallback")
            ld_items = parse_ldjson_candidates(page)
            results = []
            seen_titles = set()
            for item in ld_items:
                t = (item.get("title") or "").strip()
                if not t or t in seen_titles:
                    continue
                seen_titles.add(t)
                results.append(
                    {
                        "title": t,
                        "image_url": item.get("image_url"),
                        "category": "मनोरञ्जन",
                        "author": clean_author(item.get("author")),
                    }
                )
                if len(results) >= 5:
                    break
            if results:
                print(f"[INFO] Extracted {len(results)} items from ld+json")
                return results
            print("[WARN] No fallback data found - returning []")
            return []

        detailed = []
        for link in links[:12]:
            article = extract_article_detail(page, link)
            if article:
                detailed.append(article)
            if len(detailed) >= 5:
                break

        if detailed:
            print(f"[INFO] Extracted {len(detailed)} items via article-page fallback")
            return detailed
        print("[WARN] Article-page fallback returned nothing - returning []")
        return []

    articles: list[dict] = []

    for card in cards[:15]:   # check up to 15 to find 5 usable ones
        if len(articles) >= 5:
            break

        # Title — try multiple heading/link patterns
        title = (
            safe_text(card, "h1 a") or safe_text(card, "h1")
            or safe_text(card, "h2 a") or safe_text(card, "h2")
            or safe_text(card, "h3 a") or safe_text(card, "h3")
            or safe_text(card, ".title a") or safe_text(card, ".title")
            or safe_text(card, ".headline a") or safe_text(card, ".headline")
            or safe_text(card, "a.post-title") or safe_text(card, ".post-title")
            or safe_text(card, ".news-title a") or safe_text(card, ".news-title")
        )
        if not title:
            continue   # skip ads / separators / empty promo blocks

        # Image URL
        image_url = resolve_img_src(card) or safe_attr(card, "img", "src")

        # Category (coloured tag above the headline)
        category = (
            safe_text(card, ".cat a") or safe_text(card, ".category a")
            or safe_text(card, ".tag a") or safe_text(card, ".section-tag a")
            or safe_text(card, ".section-tag") or safe_text(card, ".cat-name")
            or safe_text(card, ".cat-label")
            or safe_text(card, "[class*='cat'] a")
            or safe_text(card, "[class*='category']")
            or safe_text(card, "[class*='tag']")
        )

        # Author
        author = clean_author(
            safe_text(card, ".author-name")
            or safe_text(card, ".author a")
            or safe_text(card, ".by-author")
            or safe_text(card, ".reporter-name")
            or safe_text(card, "[class*='author']")
            or safe_text(card, "[class*='reporter']")
        )

        articles.append({
            "title":     title,
            "image_url": image_url,
            "category":  category,
            "author":    author,
        })

    print(f"[INFO] Extracted {len(articles)} entertainment articles")
    return articles


# ─────────────────────────────────────────────────────────────────
# Task 2 — Cartoon of the Day
# ─────────────────────────────────────────────────────────────────

def scrape_cartoon_of_the_day(page) -> dict:
    """
    Find the व्यंग्यचित्र widget and return title / image_url / author.

    Three-pass strategy:
      Pass 1 — Homepage via CSS class selectors
      Pass 2 — Homepage via JS evaluate (searches by Nepali heading text;
                most robust against class-name changes)
      Pass 3 — Dedicated /photo/cartoon listing page
    """
    EMPTY = {"title": None, "image_url": None, "author": None}

    def extract_from_section(sec) -> dict | None:
        """Pull title/image/author from a known section element."""
        # Homepage widget is often `.cartoon-slider` (Swiper): visible caption lives on
        # the active slide's <img alt>, not in headings or <p>. Prefer that slide's img.
        img_el = sec.query_selector(".swiper-slide-active img") or sec.query_selector("img")
        img_alt = None
        try:
            if img_el:
                raw_alt = img_el.get_attribute("alt")
                if raw_alt and raw_alt.strip():
                    img_alt = raw_alt.strip()
        except Exception:
            pass

        title = (
            safe_text(sec, ".cartoon-title") or safe_text(sec, ".post-title")
            or safe_text(sec, ".title") or safe_text(sec, "figcaption")
            or safe_text(sec, "h2 a") or safe_text(sec, "h2")
            or safe_text(sec, "h3 a") or safe_text(sec, "h3")
            or (img_alt if img_alt else None)
            or safe_text(sec, "p")
        )
        image_url = (
            (img_element_src(img_el) if img_el else None)
            or resolve_img_src(sec)
            or safe_attr(sec, "img", "src")
        )
        author = clean_author(
            safe_text(sec, ".artist-name") or safe_text(sec, ".cartoonist")
            or safe_text(sec, ".author-name") or safe_text(sec, ".author a")
            or safe_text(sec, "[class*='author']") or safe_text(sec, "[class*='artist']")
            or cartoonist_from_img_alt(img_alt)
        )
        return {"title": title, "image_url": image_url, "author": author} if image_url else None

    def try_css(p) -> dict | None:
        for sel in [".cartoon-section", "[class*='cartoon']", "#cartoon", "[id*='cartoon']"]:
            sec = p.query_selector(sel)
            if sec:
                print(f"[INFO] Cartoon CSS match: '{sel}'")
                r = extract_from_section(sec)
                if r:
                    return r
        return None

    def try_js_heading(p) -> dict | None:
        """
        Use JavaScript to locate the section whose heading contains
        'व्यंग्यचित्र' (Unicode: \\u0935\\u094d\\u092f\\u0902\\u0917\\u094d\\u092f\\u091a\\u093f\\u0924\\u094d\\u0930).
        Inject its HTML into a temp div so we can use Playwright selectors on it.
        """
        try:
            html_str = p.evaluate(r"""
                () => {
                    const needle = '\u0935\u094d\u092f\u0902\u0917\u094d\u092f\u091a\u093f\u0924\u094d\u0930';
                    const headings = document.querySelectorAll(
                        'h2, h3, h4, .section-title, .widget-title, .block-title'
                    );
                    for (const h of headings) {
                        if (h.innerText && h.innerText.includes(needle)) {
                            const block =
                                h.closest('section, aside, .widget, .sidebar-widget') ||
                                h.closest('div[class*="cartoon"]') ||
                                h.parentElement;
                            return block ? block.outerHTML : null;
                        }
                    }
                    return null;
                }
            """)
            if html_str:
                print("[INFO] Cartoon found via Nepali heading JS search")
                # Inject into a temp div so Playwright selectors work on it
                p.evaluate("""
                    (html) => {
                        let tmp = document.getElementById('__cartoon_tmp__');
                        if (!tmp) {
                            tmp = document.createElement('div');
                            tmp.id = '__cartoon_tmp__';
                            document.body.appendChild(tmp);
                        }
                        tmp.innerHTML = html;
                    }
                """, html_str)
                tmp = p.query_selector("#__cartoon_tmp__")
                if tmp:
                    return extract_from_section(tmp)
        except Exception as e:
            print(f"[WARN] JS heading search error: {e}")
        return None

    # ═══ Pass 1 & 2 — Homepage ═══════════════════════════════════
    print(f"\n[INFO] Cartoon Pass 1/2 - homepage: {BASE_URL}")
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    try:
        page.wait_for_load_state("networkidle", timeout=12_000)
    except PlaywrightTimeoutError:
        pass
    page.evaluate("window.scrollBy(0, 1200)")   # scroll sidebar into view
    time.sleep(1)

    result = try_css(page) or try_js_heading(page)
    if result:
        return result

    # ═══ Pass 3 — Dedicated cartoon listing page ═════════════════
    cartoon_url = f"{BASE_URL}/photo/cartoon"
    print(f"[INFO] Cartoon Pass 3 - dedicated page: {cartoon_url}")
    page.goto(cartoon_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    try:
        page.wait_for_load_state("networkidle", timeout=12_000)
    except PlaywrightTimeoutError:
        pass
    page.evaluate("window.scrollBy(0, 600)")
    time.sleep(0.8)

    result = try_css(page) or try_js_heading(page)
    if result:
        return result

    # ═══ Pass 3b — First card on cartoon listing page ════════════
    print("[INFO] Cartoon fallback - first card on cartoon listing")
    for sel in [".news-post", ".photo-item", ".news-item", "article", ".post-item"]:
        first = page.query_selector(sel)
        if first:
            r = extract_from_section(first)
            if r:
                print(f"[INFO] Cartoon extracted from first '{sel}' card")
                return r

    dump_dom_snapshot(page, "cartoon")
    print("[WARN] Cartoon of the day not found")
    return EMPTY


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  ekantipur.com Playwright Scraper  (v2 - fixed)")
    print("=" * 60)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="ne-NP",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            extra_http_headers={"Accept-Language": "ne,en-US;q=0.9,en;q=0.8"},
        )

        # Block fonts and analytics to speed things up
        context.route(
            re.compile(r"\.(woff2?|ttf|eot|otf)(\?.*)?$"),
            lambda route, _: route.abort(),
        )
        context.route(
            re.compile(
                r"(google-analytics\.com|googletagmanager\.com|"
                r"facebook\.net|doubleclick\.net|adservice\.google|"
                r"googlesyndication\.com|amazon-adsystem\.com)"
            ),
            lambda route, _: route.abort(),
        )

        page = context.new_page()
        page.on("dialog", lambda d: d.dismiss())

        result: dict = {"entertainment_news": [], "cartoon_of_the_day": {}}

        # Task 1
        try:
            result["entertainment_news"] = scrape_entertainment_news(page)
        except PlaywrightTimeoutError as exc:
            print(f"[ERROR] Timeout (entertainment): {exc}")
        except Exception as exc:
            print(f"[ERROR] Entertainment crashed: {exc}")
            import traceback; traceback.print_exc()

        # Task 2
        try:
            result["cartoon_of_the_day"] = scrape_cartoon_of_the_day(page)
        except PlaywrightTimeoutError as exc:
            print(f"[ERROR] Timeout (cartoon): {exc}")
        except Exception as exc:
            print(f"[ERROR] Cartoon crashed: {exc}")
            import traceback; traceback.print_exc()

        browser.close()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print(f"\n[DONE] Saved -> {OUTPUT_FILE}")
    print(f"       Entertainment articles : {len(result['entertainment_news'])}")
    cartoon_ok = bool(result["cartoon_of_the_day"].get("image_url"))
    print(f"       Cartoon of the day     : {'found' if cartoon_ok else 'not found'}")
    print("=" * 60)


if __name__ == "__main__":
    main()