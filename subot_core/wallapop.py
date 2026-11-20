"""Fetch Wallapop search results through the public JSON API.

Wallapop's search page is a client-side application without embedded
catalogue data, so the search URL is translated into a call to the public
JSON API used by the site itself (``api.wallapop.com/api/v3/search``).
Besides the Chrome TLS impersonation shared with the other marketplaces,
the API requires the ``X-DeviceOS`` header: a plain HTTP client receives
403.

The ``keywords`` and ``order_by`` query parameters of a web search URL are
forwarded to the API verbatim. Wallapop's web interface does not carry
price filters in the URL, so the optional ``priceMin`` and ``priceMax``
parameters are accepted as a subot extension and mapped to the API's
``min_sale_price`` and ``max_sale_price``.

Only the first result page is fetched per poll, matching the behaviour of
the Subito and Vinted parsers; newly listed items surface on subsequent
polls.
"""

import json
import math
import re

from urllib.parse import parse_qs, urlsplit

from curl_cffi import requests


# Wallapop operates country-specific subdomains; the API call itself is
# always the same host, so a URL from any of these markets works.
HOSTS = frozenset((
    "wallapop.com",
    "www.wallapop.com",
    "es.wallapop.com",
    "it.wallapop.com",
    "pt.wallapop.com",
))

_API_URL = "https://api.wallapop.com/api/v3/search"
# Item URLs are built from server-provided slugs, which are shared across
# the country marketplaces; the Italian host is used as the canonical
# locale for every search market.
_ITEM_URL = "https://it.wallapop.com/item/{}"
_SEARCH_PATHS = frozenset(("/search", "/app/search"))
_ORDER_BY_VALUES = frozenset((
    "most_relevance", "newest", "price_low_to_high", "price_high_to_low",
))
_WEB_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HEADERS = {
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "X-DeviceOS": "0",
}


def fetch_page(url):
    """Fetch one Wallapop search through the JSON API and return its body."""
    params = _search_params(url)
    response = requests.get(
        _API_URL,
        params=params,
        headers=_HEADERS,
        timeout=30,
        impersonate="chrome",
    )
    response.raise_for_status()
    return response.text


def extract_items(body):
    """Extract listings and turn excessive nesting into a parse error."""

    try:
        return _extract_items(body)
    except RecursionError as error:
        raise ValueError("Wallapop response is too deeply nested") from error


def _extract_items(body):
    """Return the deduplicated raw items of one API response body."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError("Wallapop response does not contain valid JSON") from error

    items = data
    for key in ("data", "section", "payload", "items"):
        items = items.get(key) if isinstance(items, dict) else None
    if not isinstance(items, list):
        raise ValueError("Wallapop response does not contain search results")

    out = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        identity = item.get("id")
        if not isinstance(identity, str) or not identity or identity in seen:
            continue
        seen.add(identity)
        out.append(item)
    return out


def parse_item(item):
    """Normalize one raw Wallapop item into the shared listing payload."""
    if not isinstance(item, dict):
        return None
    identity = item.get("id")
    if not identity:
        return None
    web_slug = str(item.get("web_slug") or "")
    if not _WEB_SLUG_RE.fullmatch(web_slug):
        return None
    location = item.get("location")
    if not isinstance(location, dict):
        location = {}
    return {
        "id": f"wallapop:{identity}",
        "subject": _text_value(item.get("title")),
        "price": _coerce_price(item.get("price")),
        "url": _ITEM_URL.format(web_slug),
        # Wallapop locations have no separate town level: the municipality
        # goes to ``town`` (like Subito's) and the province to ``city``.
        "town": _text_value(location.get("city")),
        "city": _text_value(location.get("region2")),
    }


def _text_value(value):
    """Return a stripped string, or an empty one for anything non-textual."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _coerce_price(price):
    """Return the item price as a float, or None when missing or not positive."""
    if not isinstance(price, dict):
        return None
    amount = price.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    parsed = float(amount)
    # json.loads also accepts Infinity and NaN literals; a non-finite or
    # non-positive price would poison the notification queue downstream.
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _search_params(url):
    """Translate one web search URL into public API query parameters."""
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme not in ("http", "https")
        or not hostname
        or hostname not in HOSTS
        or parsed.path not in _SEARCH_PATHS
    ):
        raise ValueError("not a supported Wallapop search URL")

    query = parse_qs(parsed.query)
    keywords = query.get("keywords", [""])[0].strip()
    # The API itself serves a default feed without keywords; the guard
    # exists because subot watches specific searches, not the firehose.
    if not keywords:
        raise ValueError("Wallapop search URL must include a keywords parameter")

    params = {
        "source": "side_bar_filters",
        "keywords": keywords,
    }
    order_by = query.get("order_by", [""])[0]
    if order_by in _ORDER_BY_VALUES:
        params["order_by"] = order_by
    for source_key, api_key in (
        ("priceMin", "min_sale_price"),
        ("priceMax", "max_sale_price"),
    ):
        raw = query.get(source_key, [""])[0]
        if not raw:
            continue
        try:
            value = float(raw.replace(",", "."))
        except ValueError as error:
            message = f"Wallapop {source_key} parameter is not a number: {raw}"
            raise ValueError(message) from error
        if not math.isfinite(value) or value <= 0:
            message = (
                f"Wallapop {source_key} parameter is not a positive "
                f"number: {raw}"
            )
            raise ValueError(message)
        params[api_key] = value
    return params
