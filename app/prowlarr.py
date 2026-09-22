"""Kleine client voor de Prowlarr API.

Prowlarr doet zelf het praten met alle torrent-indexers; wij vragen
alleen een gecombineerde zoekopdracht op via /api/v1/search.
"""
import re

import requests

# Torznab/Newznab-categorieën voor boeken:
#   7000 = Books, 7020 = Books/EBook, 7030 = Books/Comics, 7040 = Books/Magazines
DEFAULT_BOOK_CATEGORIES = [7000, 7020]

_EPUB_RE = re.compile(r"(?i)(?:\bepub\b|\.epub\b)")
_KINDLE_RE = re.compile(r"(?i)(?:\b(?:mobi|azw3?|kindle)\b|\.(?:mobi|azw3?)\b)")


def _haystack(result: dict) -> str:
    return " ".join(
        str(result.get(k) or "") for k in ("title", "fileName", "file_name")
    )


def looks_like_epub(result: dict) -> bool:
    return bool(_EPUB_RE.search(_haystack(result)))


def looks_like_kindle(result: dict) -> bool:
    return bool(_KINDLE_RE.search(_haystack(result)))


class ProwlarrClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self):
        return {"X-Api-Key": self.api_key}

    def test_connection(self):
        resp = requests.get(
            f"{self.base_url}/api/v1/system/status",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def search(self, query: str, categories=None):
        """Zoek via alle geconfigureerde indexers.

        categories: lijst van Torznab-categorie-ids. Leeg/None = alle categorieën.
        Geeft genormaliseerde resultaten terug, gesorteerd op seeders, met een
        extra veld `is_epub`.
        """
        params = [("query", query), ("type", "search")]
        for cat in (categories or []):
            params.append(("categories", str(cat)))

        resp = requests.get(
            f"{self.base_url}/api/v1/search",
            headers=self._headers(),
            params=params,
            timeout=90,
        )
        resp.raise_for_status()
        results = resp.json()

        simplified = []
        for r in results:
            item = {
                "title": r.get("title"),
                "indexer": r.get("indexer"),
                "size": r.get("size"),
                "seeders": r.get("seeders"),
                "leechers": r.get("leechers"),
                "publish_date": r.get("publishDate"),
                "download_url": r.get("downloadUrl") or r.get("magnetUrl"),
                "guid": r.get("guid"),
                "info_url": r.get("infoUrl"),
            }
            merged = {**r, **item}
            item["is_epub"] = looks_like_epub(merged)
            item["is_kindle"] = looks_like_kindle(merged)
            simplified.append(item)

        simplified.sort(key=lambda x: (x["seeders"] or 0), reverse=True)
        return simplified
