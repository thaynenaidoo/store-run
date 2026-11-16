import json
import os
import re
import ssl
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener, urlopen

import certifi
import duckdb
from rapidfuzz import fuzz, process


PUBLIC_BASE = "https://assets-prod.grocer.nz/public"
CATALOG_URL = f"{PUBLIC_BASE}/base_v3.duckdb.br"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {"User-Agent": USER_AGENT, "Referer": "https://grocer.nz/"}


class GrocerError(Exception):
    pass


class RetailerPageError(Exception):
    pass


RETAILER_DOMAINS = {
    "pak n save": "paknsave.co.nz",
    "chemist warehouse": "chemistwarehouse.co.nz",
    "woolworths": "woolworths.co.nz",
    "new world": "newworld.co.nz",
    "the warehouse": "thewarehouse.co.nz",
    "nespresso": "nespresso.com",
    "bostocks organic": "bostocksorganic.co.nz",
    "bostock s organic": "bostocksorganic.co.nz",
    "bostock brothers": "bostocksorganic.co.nz",
}


class _RetailerRedirectHandler(HTTPRedirectHandler):
    def __init__(self, retailer_domain):
        super().__init__()
        self.retailer_domain = retailer_domain

    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urlparse(new_url)
        if parsed.scheme != "https" or not _is_retailer_host(parsed.hostname, self.retailer_domain):
            raise RetailerPageError("The retailer page redirected outside its retailer domain.")
        return super().redirect_request(request, response, code, message, headers, new_url)


class _RetailerMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.json_ld = []
        self._json_ld_depth = 0
        self._json_ld_parts = []

    def handle_starttag(self, tag, attrs):
        attributes = {key.lower(): value for key, value in attrs if value is not None}
        if tag.lower() == "meta":
            key = attributes.get("property") or attributes.get("name") or attributes.get("itemprop")
            value = attributes.get("content")
            if key and value:
                self.meta[key.lower()] = value.strip()
        if tag.lower() == "script" and attributes.get("type", "").lower().split(";")[0] == "application/ld+json":
            self._json_ld_depth = 1
            self._json_ld_parts = []
        elif self._json_ld_depth:
            self._json_ld_depth += 1

    def handle_endtag(self, tag):
        if self._json_ld_depth:
            self._json_ld_depth -= 1
            if self._json_ld_depth == 0:
                self.json_ld.append("".join(self._json_ld_parts))
                self._json_ld_parts = []

    def handle_data(self, data):
        if self._json_ld_depth:
            self._json_ld_parts.append(data)


def _is_retailer_host(hostname, domain):
    hostname = (hostname or "").lower().rstrip(".")
    return hostname == domain or hostname.endswith(f".{domain}")


def grocer_product_id_from_url(url):
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname not in {"grocer.nz", "www.grocer.nz"}:
        return None
    match = re.match(r"^/product/(\d+)(?:/|$)", parsed.path)
    return int(match.group(1)) if match else None


def _offer_prices(value):
    prices = []
    if isinstance(value, dict):
        kind = value.get("@type", "")
        if kind == "Offer" or ("price" in value and "priceCurrency" in value):
            currency = str(value.get("priceCurrency", "NZD")).upper()
            if currency == "NZD":
                prices.append(value.get("price"))
        for nested in value.values():
            prices.extend(_offer_prices(nested))
    elif isinstance(value, list):
        for nested in value:
            prices.extend(_offer_prices(nested))
    return prices


def _valid_retail_price(value):
    try:
        price = float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if 0 < price <= 100000:
        return round(price, 2)
    return None


def fetch_retailer_product_price(url, retailer_name):
    parsed = urlparse(str(url or ""))
    retailer_key = re.sub(r"[^a-z0-9]+", " ", str(retailer_name or "").lower()).strip()
    domain = RETAILER_DOMAINS.get(retailer_key)
    if not domain:
        raise RetailerPageError(f"Automatic retailer lookup is not configured for {retailer_name or 'this store'}.")
    if parsed.scheme != "https" or parsed.port not in {None, 443} or not _is_retailer_host(parsed.hostname, domain):
        raise RetailerPageError(f"Retailer lookup only supports HTTPS {retailer_name} product links.")
    request = Request(url, headers={**REQUEST_HEADERS, "Accept": "text/html,application/xhtml+xml"})
    context = ssl.create_default_context(cafile=certifi.where())
    opener = build_opener(_RetailerRedirectHandler(domain), HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=20) as response:
            final_url = urlparse(response.geturl())
            if final_url.scheme != "https" or not _is_retailer_host(final_url.hostname, domain):
                raise RetailerPageError("The retailer page resolved outside its retailer domain.")
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise RetailerPageError("The retailer product page is too large to inspect.")
            last_modified = response.headers.get("Last-Modified", "")
    except RetailerPageError:
        raise
    except HTTPError as error:
        if error.code in {401, 403}:
            raise RetailerPageError(
                f"{retailer_name} blocked automated price lookup (HTTP {error.code}). "
                "Open the retailer product page to check the current price."
            ) from error
        raise RetailerPageError(
            f"Could not read the {retailer_name} product page (HTTP {error.code})."
        ) from error
    except Exception as error:
        raise RetailerPageError(f"Could not read the {retailer_name} product page.") from error

    parser = _RetailerMetadataParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
    except Exception as error:
        raise RetailerPageError(f"Could not parse the {retailer_name} product page.") from error

    candidate_values = []
    for key in ("product:price:amount", "og:price:amount", "price"):
        if key in parser.meta:
            currency = parser.meta.get("product:price:currency") or parser.meta.get("og:price:currency") or "NZD"
            if currency.upper() == "NZD":
                candidate_values.append(parser.meta[key])
    for document in parser.json_ld:
        try:
            candidate_values.extend(_offer_prices(json.loads(document)))
        except (json.JSONDecodeError, TypeError):
            continue
    for candidate in candidate_values:
        price = _valid_retail_price(candidate)
        if price is not None:
            return {"price": price, "source_modified": last_modified, "url": final_url.geturl()}
    raise RetailerPageError(f"No NZD product price was found in the {retailer_name} page metadata.")


def choose_product_match(item_name, products, minimum_score=97, minimum_margin=8):
    if not products:
        return None
    choices = [product["name"] for product in products]
    matches = process.extract(item_name, choices, scorer=fuzz.WRatio, limit=2)
    if not matches or matches[0][1] < minimum_score:
        return None
    if len(matches) > 1 and matches[0][1] - matches[1][1] < minimum_margin:
        return None
    return products[matches[0][2]]


def grocer_product_url(product_id, name):
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return f"https://grocer.nz/product/{int(product_id)}/{slug}"


class GrocerPriceClient:
    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.catalog_path = self.cache_dir / "grocer_catalog.duckdb"
        self._products = None
        self._catalog_modified = None

    def stores_for_vendor(self, vendor_name):
        catalog_modified = self._ensure_catalog()
        with duckdb.connect(str(self.catalog_path), read_only=True) as connection:
            rows = connection.execute(
                """SELECT s.id, s.name
                   FROM public_stores s
                   JOIN public_vendors v ON v.id = s.vendor_id
                   WHERE s.is_enabled AND lower(v.name) = lower(?)
                   ORDER BY s.name COLLATE NOCASE""",
                [vendor_name],
            ).fetchall()
        return [{"id": row[0], "name": row[1]} for row in rows], catalog_modified

    def products(self):
        catalog_modified = self._ensure_catalog()
        if self._products is None or self._catalog_modified != catalog_modified:
            with duckdb.connect(str(self.catalog_path), read_only=True) as connection:
                rows = connection.execute(
                    """SELECT id, name, brand, size
                       FROM public_products WHERE redirected_to IS NULL"""
                ).fetchall()
            self._products = [
                {"id": row[0], "name": row[1], "brand": row[2], "size": row[3]}
                for row in rows
            ]
            self._catalog_modified = catalog_modified
        return self._products

    def search_products(self, query, limit=8):
        products = self.products()
        matches = process.extract(
            query,
            [product["name"] for product in products],
            scorer=fuzz.WRatio,
            limit=limit,
        )
        return [{**products[index], "score": round(score)} for _name, score, index in matches]

    def prices_for_store(self, store_id):
        try:
            store_id = int(store_id)
        except (TypeError, ValueError) as error:
            raise GrocerError("Choose a valid Grocer store.") from error
        url = f"{PUBLIC_BASE}/prices_per_store_v3/public_prices_{store_id}.parquet"
        path = self.cache_dir / f"grocer_prices_{store_id}.parquet"
        source_modified = self._cache_public_file(url, path)
        try:
            with duckdb.connect() as connection:
                rows = connection.execute(
                    """SELECT product_id, sale_price_cent, original_price_cent,
                              online_price_cent
                       FROM read_parquet(?)""",
                    [str(path)],
                ).fetchall()
        except Exception as error:
            raise GrocerError("Could not read the selected store's price data.") from error
        return {
            row[0]: next((price for price in row[1:] if price is not None), None)
            for row in rows
        }, source_modified

    def _ensure_catalog(self):
        modified = self._cache_public_file(CATALOG_URL, self.catalog_path)
        return modified

    def _cache_public_file(self, url, path):
        marker = path.with_suffix(path.suffix + ".last-modified")
        try:
            remote_modified = self._fetch(url, method="HEAD").get("last-modified", "")
            cached_modified = marker.read_text(encoding="utf-8") if marker.exists() else ""
            if path.exists() and cached_modified == remote_modified:
                return remote_modified
            headers, body = self._fetch(url)
            remote_modified = headers.get("last-modified", remote_modified)
            if path == self.catalog_path and b"DUCK" not in body[:64]:
                raise GrocerError("The Grocer catalog download was not a DuckDB file.")
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.cache_dir, delete=False) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    temporary_file.write(body)
                os.replace(temporary_path, path)
                marker.write_text(remote_modified, encoding="utf-8")
            finally:
                if temporary_path and temporary_path.exists():
                    temporary_path.unlink()
            return remote_modified
        except GrocerError:
            raise
        except Exception as error:
            if path.exists():
                return marker.read_text(encoding="utf-8") if marker.exists() else ""
            raise GrocerError("Could not reach Grocer's public pricing data.") from error

    @staticmethod
    def _fetch(url, method="GET"):
        request = Request(url, headers=REQUEST_HEADERS, method=method)
        context = ssl.create_default_context(cafile=certifi.where())
        try:
            with urlopen(request, context=context, timeout=45) as response:
                headers = {key.lower(): value for key, value in response.headers.items()}
                return headers if method == "HEAD" else (headers, response.read())
        except Exception as error:
            raise GrocerError(f"Grocer data request failed: {error}") from error