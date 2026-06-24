"""
utils/pitchfork_client.py
─────────────────────────
Scrapes Pitchfork's Best New Albums section to retrieve the latest featured
album review.

We use requests + BeautifulSoup with multiple selector fallbacks because
Pitchfork occasionally redesigns their site and CSS class names change without
notice. The scraper tries to read structured data from Next.js's __NEXT_DATA__
JSON blob first (more reliable than HTML class names), then falls back to HTML
parsing with a set of common selectors.

If the scraper cannot extract the required fields, it raises
PitchforkScrapingError with a descriptive message so the caller can post a
user-facing alert in Discord rather than failing silently.

Public functions
----------------
get_latest_best_new_album()  → dict
    Scrape the Best New Albums listing page and return data for the most
    recently featured album: artist, title, score, review text, Pitchfork
    URL, and cover art URL.
"""

import json
import logging
import re

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_BASE_URL = "https://pitchfork.com"
_BEST_NEW_ALBUMS_URL = "https://pitchfork.com/reviews/best/albums/"

# Seconds to wait for a response before giving up.
_REQUEST_TIMEOUT = 15

# Maximum characters of review text to pass to Claude. Pitchfork reviews can
# be several thousand words; we cap this to keep token costs reasonable while
# still giving Claude enough context to write a meaningful summary.
_REVIEW_TEXT_MAX_CHARS = 3000

# Pretend to be a browser so Pitchfork's CDN doesn't block the request.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


class PitchforkScrapingError(Exception):
    """
    Raised when the scraper cannot extract the required data from Pitchfork.

    This usually means Pitchfork has updated their site structure. The caller
    (AlbumReviewsCog) catches this and posts an alert in the Discord channel
    so the server admin knows something needs attention.
    """
    pass


def _fetch(url: str) -> BeautifulSoup:
    """
    Fetch a URL and return a parsed BeautifulSoup object.

    Uses html.parser (Python stdlib) so no extra C extension is needed.

    Raises
    ------
    PitchforkScrapingError
        If the HTTP request fails or returns a non-2xx status code.
    """
    try:
        response = requests.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise PitchforkScrapingError(f"HTTP request failed for {url}: {exc}") from exc

    return BeautifulSoup(response.text, "html.parser")


def _try_next_data(soup: BeautifulSoup) -> dict | None:
    """
    Try to read structured page data from Next.js's __NEXT_DATA__ script tag.

    Pitchfork is built on Next.js, which embeds all server-side props as a
    JSON blob in a <script id="__NEXT_DATA__"> tag. This is far more reliable
    than scraping HTML class names that change with every redesign.

    Returns the parsed dict, or None if the tag is absent or the JSON is
    malformed.
    """
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if not tag or not tag.string:
        return None
    try:
        return json.loads(tag.string)
    except json.JSONDecodeError:
        logger.warning("__NEXT_DATA__ found but failed to parse as JSON")
        return None


def _extract_review_links(soup: BeautifulSoup) -> list[str]:
    """
    Find all review page URLs on the Best New Albums listing page.

    Matches any <a> tag whose href contains '/reviews/albums/' — this URL
    pattern has been consistent across multiple Pitchfork site redesigns and
    is more reliable than any CSS class name.

    Returns a de-duplicated list of absolute URLs in page order (most recent
    album is first on the page).
    """
    links: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if "/reviews/albums/" in href:
            # Convert relative paths ("/reviews/albums/...") to absolute.
            if href.startswith("/"):
                href = _BASE_URL + href
            if href not in links:
                links.append(href)
    return links


def _parse_review_page(soup: BeautifulSoup, url: str) -> dict:
    """
    Extract album metadata and review text from a Pitchfork review page.

    Attempts extraction in two phases:
      1. Parse __NEXT_DATA__ JSON (preferred — survives HTML redesigns).
      2. Fall back to a set of HTML CSS selectors tried in priority order.

    Parameters
    ----------
    soup : BeautifulSoup   Parsed HTML of the review page.
    url  : str             The review URL, used for error messages and the
                           returned dict.

    Returns
    -------
    dict with keys:
        artist        – artist name (str)
        album         – album title (str)
        score         – Pitchfork score as a float, e.g. 8.5
        review_text   – first several paragraphs of the review body (str)
        pitchfork_url – full URL to the review page (str)
        image_url     – cover art URL (str or None)

    Raises
    ------
    PitchforkScrapingError
        If artist, album title, or score cannot be determined.
    """
    artist: str | None = None
    album: str | None = None
    score: float | None = None
    review_text: str = ""
    image_url: str | None = None

    # ── Phase 1: __NEXT_DATA__ JSON ───────────────────────────────────────────
    next_data = _try_next_data(soup)
    if next_data:
        try:
            page_props = next_data.get("props", {}).get("pageProps", {})

            # Pitchfork has used several key names across site versions.
            # Try them all and take the first non-empty result.
            album_data: dict = (
                page_props.get("album")
                or page_props.get("review")
                or page_props.get("data", {}).get("album")
                or page_props.get("data", {}).get("review")
                or {}
            )

            if album_data:
                # Artist — stored as a list of dicts or a plain string depending
                # on the site version.
                artists_raw = album_data.get("artists") or album_data.get("artist") or []
                if isinstance(artists_raw, list) and artists_raw:
                    artist = ", ".join(
                        a.get("name", str(a)) if isinstance(a, dict) else str(a)
                        for a in artists_raw
                    )
                elif isinstance(artists_raw, str):
                    artist = artists_raw

                album = album_data.get("album") or album_data.get("title")

                score_raw = album_data.get("score") or album_data.get("rating")
                if score_raw is not None:
                    score = float(score_raw)

                # Review body may be an HTML string or a list of content blocks.
                body = album_data.get("body") or album_data.get("reviewBody") or ""
                if body:
                    # Strip any embedded HTML tags to get plain text.
                    body_soup = BeautifulSoup(body, "html.parser")
                    review_text = body_soup.get_text(separator="\n").strip()

                # Cover art — stored as list of dicts or a plain string.
                images_raw = album_data.get("image") or album_data.get("images") or []
                if isinstance(images_raw, list) and images_raw:
                    first = images_raw[0]
                    image_url = (
                        first.get("src") or first.get("url")
                        if isinstance(first, dict) else first
                    )
                elif isinstance(images_raw, str):
                    image_url = images_raw

        except (KeyError, TypeError, ValueError) as exc:
            # Non-fatal — we'll try HTML parsing next.
            logger.debug("__NEXT_DATA__ parse attempt failed for %s: %s", url, exc)

    # ── Phase 2: HTML selector fallbacks ─────────────────────────────────────
    # Only run for fields that __NEXT_DATA__ didn't fill in.

    if not artist:
        for selector in [
            "[class*='ArtistName']",
            "[class*='artist-name']",
            "[class*='artist_name']",
            "h2.artist",
            "[data-testid='artists']",
        ]:
            el = soup.select_one(selector)
            if el:
                artist = el.get_text(separator=", ").strip()
                break

    if not album:
        for selector in [
            "[class*='AlbumTitle']",
            "[class*='album-title']",
            "[class*='album_title']",
            "h1.title",
            "[data-testid='album-title']",
        ]:
            el = soup.select_one(selector)
            if el:
                album = el.get_text().strip()
                break

        # Last resort: parse artist and album from the HTML <title> tag.
        # Pitchfork's <title> is typically "Artist: Album | Pitchfork".
        if not album:
            title_tag = soup.find("title")
            if title_tag:
                raw_title = title_tag.get_text().split("|")[0].strip()
                if ":" in raw_title:
                    artist_part, album_part = raw_title.split(":", 1)
                    if not artist:
                        artist = artist_part.strip()
                    album = album_part.strip()

    if score is None:
        for selector in [
            "[class*='Rating']",
            "[class*='score']",
            "[class*='Score']",
            "span.score",
            "[data-testid='score']",
        ]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text().strip()
                # Pitchfork scores are decimals between 0.0 and 10.0.
                match = re.search(r"\b(10(?:\.0)?|[0-9](?:\.[0-9])?)\b", text)
                if match:
                    score = float(match.group(1))
                    break

    if not review_text:
        for selector in [
            "[class*='body__inner']",
            "[class*='review-body']",
            "[class*='ReviewBody']",
            "[class*='article-body']",
            "article .body",
            "article",
        ]:
            container = soup.select_one(selector)
            if container:
                paragraphs = [
                    p.get_text().strip()
                    for p in container.find_all("p")
                    if p.get_text().strip()
                ]
                if paragraphs:
                    review_text = "\n\n".join(paragraphs)
                    break

    if not image_url:
        # og:image is a near-universal fallback present on virtually all
        # modern web pages regardless of internal class name changes.
        og_image = soup.find("meta", property="og:image")
        if og_image:
            image_url = og_image.get("content")

    # ── Validate required fields ──────────────────────────────────────────────
    missing = []
    if not artist:
        missing.append("artist")
    if not album:
        missing.append("album title")
    if score is None:
        missing.append("score")

    if missing:
        raise PitchforkScrapingError(
            f"Could not extract {', '.join(missing)} from {url}. "
            "Pitchfork may have updated their site structure."
        )

    # Cap review text length before handing to Claude to control token usage.
    if len(review_text) > _REVIEW_TEXT_MAX_CHARS:
        review_text = review_text[:_REVIEW_TEXT_MAX_CHARS] + "…"

    return {
        "artist": artist,
        "album": album,
        "score": score,
        "review_text": review_text,
        "pitchfork_url": url,
        "image_url": image_url,
    }


def get_latest_best_new_album() -> dict:
    """
    Scrape the Pitchfork Best New Albums page and return data for the most
    recently featured album.

    Fetches the listing page, collects all review links (in page order, most
    recent first), then tries each of the first five until one parses cleanly.
    Returns the first successfully parsed result.

    Returns
    -------
    dict with keys:
        artist        – artist name
        album         – album title
        score         – float, e.g. 8.5
        review_text   – excerpt of the review body for Claude to summarize
        pitchfork_url – full URL to the Pitchfork review
        image_url     – cover art URL, or None

    Raises
    ------
    PitchforkScrapingError
        If the listing page cannot be fetched, no review links are found, or
        none of the review pages can be parsed successfully.
    """
    logger.info("Fetching Pitchfork Best New Albums: %s", _BEST_NEW_ALBUMS_URL)
    listing_soup = _fetch(_BEST_NEW_ALBUMS_URL)

    review_links = _extract_review_links(listing_soup)
    if not review_links:
        raise PitchforkScrapingError(
            "No review links found on the Best New Albums page. "
            "Pitchfork may have updated their site structure."
        )

    logger.debug("Found %d review links on listing page", len(review_links))

    # Try the first few links until one succeeds. We try up to 5 in case the
    # topmost link is a non-album page or a special feature with unusual markup.
    last_error: Exception | None = None
    for url in review_links[:5]:
        try:
            logger.info("Fetching review page: %s", url)
            review_soup = _fetch(url)
            data = _parse_review_page(review_soup, url)
            logger.info(
                "Parsed Pitchfork review: %s — %s (%.1f)",
                data["artist"], data["album"], data["score"],
            )
            return data
        except PitchforkScrapingError as exc:
            logger.warning("Could not parse review at %s: %s", url, exc)
            last_error = exc
            continue

    raise PitchforkScrapingError(
        f"Could not parse any of the top {min(5, len(review_links))} review pages. "
        f"Last error: {last_error}"
    )
