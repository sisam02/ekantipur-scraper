# ekantipur-scraper (Playwright)

A Python scraper for [ekantipur.com](https://ekantipur.com) using **Playwright**.  
It extracts:

1. Top 5 **Entertainment (मनोरञ्जन)** news articles.
2. **Cartoon of the Day (व्यंग्यचित्र)**.

---

## Features (v2)

- **Dynamic card selector detection**: automatically finds the correct article card selector on the page.
- **Lazy-load and CDN image handling**: resolves `data-src`, `data-lazy-src`, `data-original`, `srcset`, and inline CSS background images.
- **Cartoon extraction**: uses CSS selectors and JavaScript evaluation to locate Nepali heading text.
- **DOM snapshot helper**: writes HTML snapshots for debugging selector issues.
- **Network idle waits**: handles ad-heavy or slow-loading pages.
- **Scroll-triggered lazy loading**: scrolls pages to load images.
- **Author normalization**: supports Devanagari punctuation and whitespace handling.
- Robust fallbacks:
  - Extract links from page if card selector fails.
  - Parse `ld+json` structured data if needed.
  - Extract individual article details from article pages.

---

## Requirements

- Python **3.13+**
- [Playwright](https://playwright.dev/python/)
- `uv` command-line task runner

---

## Installation

1. Clone the repository:

```bash
git clone <your-repo-url>
cd ekantipur-scraper

```
2. Create and activate a virtual environment:
```
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS/Linux
python -m venv .venv
source .venv/bin/activate
```
3. Install dependencies:
```
pip install playwright
```
4. Install Chromium browser for Playwright:
```
python -m playwright install chromium
```
5. Run the scraper:
```
uv run python scraper.py

```
## File Structure
```
ekantipur-scraper/
├─ scraper.py               # main scraper script
├─ output.json              # generated after scraper runs
├─ debug_snapshot_*.html    # debugging snapshots
└─ README.md
```
## Example Output
```
{
  "entertainment_news": [
    {
      "title": "Article Title",
      "image_url": "https://...",
      "category": "मनोरञ्जन",
      "author": "Author Name"
    }
  ],
  "cartoon_of_the_day": {
    "title": "Cartoon Title",
    "image_url": "https://...",
    "author": "Cartoonist Name"
  }
}