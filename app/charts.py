"""Bestsellerlijsten om nieuwe boeken te ontdekken.

Bronnen (allemaal zonder account, behalve NYT: gratis API-sleutel):
  - Amazon Charts (VS): wekelijks, meest verkocht / meest gelezen, fictie + non-fictie
  - NYT Best Sellers (VS): officiële API
  - Apple Books (VK): top betaald + fictie + non-fictie
  - Bestseller 60 van de CPNB (NL): fictie, non-fictie, spanning
  - Open Library "trending" (wereldwijd, Engels)
Resultaten worden gecachet; beschrijvingen worden per boek apart (lui) opgehaald.
"""
import html as _html
import json
import logging
import re
import time

import requests

from . import db

log = logging.getLogger("ebookarr.charts")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept-Language": "en,nl;q=0.8"}
TIMEOUT = 20
LIST_TTL = 6 * 3600
NYT_TTL = 12 * 3600


class ChartError(Exception):
    pass


# ---------- catalogus ----------

REGIONS = [
    ("us", "Engels · VS"),
    ("uk", "Engels · VK"),
    ("nl", "Nederlands"),
    ("world", "Trending"),
]

# id, regio, bron, label, taal
LISTS = [
    ("amz-sold-fiction", "us", "Amazon Charts", "Meest verkocht · Fictie", "en"),
    ("amz-sold-nonfiction", "us", "Amazon Charts", "Meest verkocht · Non-fictie", "en"),
    ("amz-read-fiction", "us", "Amazon Charts", "Meest gelezen · Fictie", "en"),
    ("amz-read-nonfiction", "us", "Amazon Charts", "Meest gelezen · Non-fictie", "en"),
    ("nyt-hardcover-fiction", "us", "New York Times", "Hardcover · Fictie", "en"),
    ("nyt-hardcover-nonfiction", "us", "New York Times", "Hardcover · Non-fictie", "en"),
    ("nyt-combined-fiction", "us", "New York Times", "Print & e-book · Fictie", "en"),
    ("nyt-combined-nonfiction", "us", "New York Times", "Print & e-book · Non-fictie", "en"),
    ("apple-gb-top", "uk", "Apple Books VK", "Top betaald · Alles", "en"),
    ("apple-gb-fiction", "uk", "Apple Books VK", "Fictie", "en"),
    ("apple-gb-nonfiction", "uk", "Apple Books VK", "Non-fictie", "en"),
    ("b60-fictie", "nl", "Bestseller 60", "Fictie", "nl"),
    ("b60-non-fictie", "nl", "Bestseller 60", "Non-fictie", "nl"),
    ("b60-spannend", "nl", "Bestseller 60", "Spanning", "nl"),
    ("b60-all", "nl", "Bestseller 60", "Totaal top 60", "nl"),
    ("ol-weekly", "world", "Open Library", "Trending deze week", "en"),
    ("ol-daily", "world", "Open Library", "Trending vandaag", "en"),
]
_BY_ID = {l[0]: l for l in LISTS}


def catalog():
    key = bool(db.get_setting("nyt_api_key"))
    return {
        "regions": [{"id": r, "label": n} for r, n in REGIONS],
        "lists": [{"id": i, "region": reg, "source": src, "label": lab, "language": lang,
                   "needs_key": i.startswith("nyt-") and not key}
                  for i, reg, src, lab, lang in LISTS],
    }


def _get(url, **kw):
    headers = kw.pop("headers", UA)
    last = None
    for attempt in range(4):
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT, **kw)
        except requests.RequestException as e:
            last = ChartError(f"Kon de lijst niet ophalen: {e}")
            time.sleep(1.5)
            continue
        if r.status_code == 429:
            raise ChartError("De bron zegt 'te veel verzoeken' — probeer het over een paar minuten opnieuw.")
        if r.status_code >= 500:
            last = ChartError(f"De bron gaf een fout ({r.status_code}).")
            time.sleep(1.5)
            continue
        if r.status_code >= 400:
            raise ChartError(f"De bron gaf een fout ({r.status_code}).")
        return r
    raise last


def _clean(s):
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


# ---------- Amazon Charts ----------

def _amazon_date():
    hit = db.cache_get("charts|amz-date")
    if hit:
        return hit
    h = _get("https://www.amazon.com/charts/").text
    dates = re.findall(r"/charts/(\d{4}-\d{2}-\d{2})/(?:mostsold|mostread)/", h)
    if not dates:
        raise ChartError("Kon de datum van de Amazon Charts niet bepalen.")
    d = sorted(set(dates))[-1]
    # de pagina verwijst soms naar de vorige week als 'terug'; mostsold-links wijzen naar de nieuwste
    sold = sorted(set(re.findall(r"/charts/(\d{4}-\d{2}-\d{2})/mostsold/", h)))
    d = sold[-1] if sold else d
    db.cache_set("charts|amz-date", d, LIST_TTL)
    return d


def _amazon(kind, genre):
    date = _amazon_date()
    url = f"https://www.amazon.com/charts/{date}/{kind}/{genre}"
    h = _get(url).text
    items, seen = [], set()
    for chunk in h.split('data-rank="')[1:]:
        m = re.match(r"(\d+)", chunk)
        if not m:
            continue
        rank = int(m.group(1))
        if rank in seen:
            continue
        alt = re.search(r'alt="Cover image of (.+?) by (.+?)"', chunk)
        title = _clean(re.search(r'kc-rank-card-title[^>]*>(.*?)</div>', chunk, re.S).group(1)) \
            if re.search(r'kc-rank-card-title', chunk) else (_clean(alt.group(1)) if alt else "")
        author = _clean(re.search(r'kc-rank-card-author[^>]*>(.*?)</div>', chunk, re.S).group(1)) \
            if re.search(r'kc-rank-card-author', chunk) else (_clean(alt.group(2)) if alt else "")
        author = re.sub(r"^by\s+", "", author, flags=re.I)
        if not title:
            continue
        img = re.search(r'<img src="(https://[^"]+)"', chunk)
        wk = re.search(r"(\d+)\s+weeks?\s+on\s+the\s+list", chunk)
        seen.add(rank)
        items.append({"rank": rank, "title": title, "author": author,
                      "cover_url": img.group(1) if img else None,
                      "weeks": int(wk.group(1)) if wk else None})
        if len(items) >= 20:
            break
    if not items:
        raise ChartError("De Amazon-pagina had een andere opbouw dan verwacht.")
    return items, f"week van {date}", url


# ---------- NYT ----------

def _nyt(list_name):
    key = db.get_setting("nyt_api_key")
    if not key:
        raise ChartError("Voor de NYT-lijsten heb je een gratis API-sleutel nodig (Instellingen → Bibliotheek).")
    r = _get(f"https://api.nytimes.com/svc/books/v3/lists/current/{list_name}.json",
             params={"api-key": key})
    d = r.json().get("results", {})
    items = []
    for b in d.get("books", []):
        items.append({
            "rank": b.get("rank"), "title": (b.get("title") or "").title(),
            "author": re.sub(r"^by\s+", "", b.get("author") or "", flags=re.I),
            "isbn": b.get("primary_isbn13") or None, "cover_url": b.get("book_image"),
            "description": (b.get("description") or "").strip() or None,
            "weeks": b.get("weeks_on_list") or None,
        })
    return items, f"week van {d.get('published_date', '')}", f"https://www.nytimes.com/books/best-sellers/"


# ---------- Apple Books ----------

def _apple_rss(cc, kind):
    r = _get(f"https://rss.applemarketingtools.com/api/v2/{cc}/books/{kind}/50/books.json")
    res = r.json().get("feed", {}).get("results", [])
    items = []
    for i, b in enumerate(res, 1):
        art = b.get("artworkUrl100") or ""
        items.append({"rank": i, "title": b.get("name") or "", "author": b.get("artistName") or "",
                      "cover_url": re.sub(r"/\d+x\d+(bb)?\.(jpg|png)$", r"/400x400bb.\2", art) or None,
                      "apple_id": b.get("id"), "country": cc})
    return items, "bijgewerkt vandaag", f"https://books.apple.com/{cc}/charts"


def _apple_genre(cc, slug, genre_id):
    url = f"https://books.apple.com/{cc}/charts/{slug}/{genre_id}"
    h = _get(url).text
    items = []
    for blk in re.findall(r'<script[^>]*ld\+json[^>]*>(.*?)</script>', h, re.S):
        try:
            d = json.loads(blk)
        except ValueError:
            continue
        for el in (d.get("itemListElement") if isinstance(d, dict) else None) or []:
            m = re.search(r"/id(\d+)", el.get("url") or "")
            au = el.get("author")
            au = au.get("name") if isinstance(au, dict) else (au[0].get("name") if isinstance(au, list) and au else "")
            items.append({"rank": el.get("position") or len(items) + 1, "title": el.get("name") or "",
                          "author": au or "", "apple_id": m.group(1) if m else None, "country": cc})
        if items:
            break
    if not items:
        raise ChartError("De Apple Books-pagina had een andere opbouw dan verwacht.")
    return items, "bijgewerkt vandaag", url


# ---------- Bestseller 60 (CPNB) ----------

def _b60(slug):
    url = "https://www.debestseller60.nl/" + slug
    h = _get(url).text
    wk = re.search(r"Week\s*(\d+)\s*-\s*(\d{4})", h)
    items = []
    for chunk in h.split('class="card__position')[1:]:
        m = re.search(r">\s*(\d+)\s*<", chunk)
        title = re.search(r'class="card__title[^"]*"[^>]*title="([^"]*)"', chunk)
        author = re.search(r'class="card__author">\s*<a[^>]*>(.*?)</a>', chunk, re.S)
        summ = re.search(r'class="card__summary">(.*?)</div>', chunk, re.S)
        isbn = re.search(r"ISBN\s*(97[89]\d{10})", chunk)
        img = re.search(r'class="card__cover__img"[^>]*src="([^"]+)"', chunk)
        if not (m and title):
            continue
        items.append({"rank": int(m.group(1)), "title": _clean(title.group(1)),
                      "author": _clean(author.group(1)) if author else "",
                      "isbn": isbn.group(1) if isbn else None,
                      "cover_url": img.group(1) if img else None,
                      "description": _clean(summ.group(1)) if summ else None})
    if not items:
        raise ChartError("De Bestseller 60-pagina had een andere opbouw dan verwacht.")
    return items, (f"week {wk.group(1)} - {wk.group(2)}" if wk else ""), url


# ---------- Open Library ----------

def _ol(period):
    r = _get(f"https://openlibrary.org/trending/{period}.json", params={"limit": 50},
             headers={"User-Agent": "EbookArr/1.0 (+local)"})
    items = []
    for i, w in enumerate(r.json().get("works", []), 1):
        cid = w.get("cover_i")
        items.append({"rank": i, "title": w.get("title") or "",
                      "author": (w.get("author_name") or [""])[0],
                      "year": w.get("first_publish_year"),
                      "cover_url": f"https://covers.openlibrary.org/b/id/{cid}-L.jpg?default=false" if cid else None})
    return items, "meest bekeken boeken", "https://openlibrary.org/trending"


# ---------- publiek ----------

def get_list(list_id, refresh=False):
    meta = _BY_ID.get(list_id)
    if not meta:
        raise ChartError("Onbekende lijst.")
    ck = f"charts|list|{list_id}"
    if not refresh:
        hit = db.cache_get(ck)
        if hit:
            return hit
    try:
        return _fetch_list(list_id, meta)
    except ChartError:
        stale = db.cache_get("charts|stale|" + list_id)
        if stale:
            stale = {**stale, "when": (stale.get("when") or "") + " (oudere kopie: bron nu niet bereikbaar)"}
            return stale
        raise


def _fetch_list(list_id, meta):
    lang = meta[4]
    if list_id.startswith("amz-"):
        _, kind, genre = list_id.split("-")
        items, when, url = _amazon("mostsold" if kind == "sold" else "mostread", genre)
    elif list_id.startswith("nyt-"):
        _, kind, genre = list_id.split("-")
        name = {"hardcover": "hardcover", "combined": "combined-print-and-e-book"}[kind] + "-" + genre
        items, when, url = _nyt(name)
    elif list_id == "apple-gb-top":
        items, when, url = _apple_rss("gb", "top-paid")
    elif list_id == "apple-gb-fiction":
        items, when, url = _apple_genre("gb", "fiction-and-literature", 9031)
    elif list_id == "apple-gb-nonfiction":
        items, when, url = _apple_genre("gb", "non-fiction", 9002)
    elif list_id == "b60-all":
        items, when, url = _b60("")
    elif list_id.startswith("b60-"):
        items, when, url = _b60(list_id[4:])
    elif list_id == "ol-weekly":
        items, when, url = _ol("weekly")
    else:
        items, when, url = _ol("daily")
    for it in items:
        it["language"] = lang
    out = {"id": list_id, "source": meta[2], "label": meta[3], "when": when, "url": url, "items": items}
    db.cache_set("charts|list|" + list_id, out, NYT_TTL if list_id.startswith("nyt-") else LIST_TTL)
    db.cache_set("charts|stale|" + list_id, out, 30 * 86400)
    return out


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def describe(title, author="", isbn="", apple_id="", country="gb", language="en"):
    """Beschrijving (+ ISBN/jaar/cover) van één boek; gecachet."""
    ck = "charts|desc|" + "|".join(_norm(x) for x in (title, author, isbn, str(apple_id or "")))
    hit = db.cache_get(ck)
    if hit is not None:
        return hit
    out = {}
    if apple_id:
        try:
            d = requests.get("https://itunes.apple.com/lookup",
                             params={"id": apple_id, "country": country, "entity": "ebook"},
                             headers=UA, timeout=10).json()
            r = (d.get("results") or [{}])[0]
            if r.get("description"):
                out["description"] = _clean(r["description"])
            if r.get("releaseDate"):
                out["year"] = int(r["releaseDate"][:4])
            if r.get("artworkUrl100"):
                out["cover_url"] = re.sub(r"/\d+x\d+bb\.", "/400x400bb.", r["artworkUrl100"])
        except (requests.RequestException, ValueError):
            pass
    if "description" not in out:
        from . import metadata
        q = f"{title} {author}".strip()
        try:
            res = metadata.search(q, languages=[language], limit=6)
        except Exception:  # noqa: BLE001
            res = []
        nt = _norm(title)
        for r in res:
            rt = _norm(r.get("title"))
            if rt and (rt == nt or rt.startswith(nt) or nt.startswith(rt)):
                if r.get("description"):
                    out["description"] = _clean(r["description"])
                if r.get("isbn"):
                    out["isbn"] = r["isbn"]
                if r.get("year"):
                    out["year"] = r["year"]
                if r.get("cover_url") and "cover_url" not in out:
                    out["cover_url"] = r["cover_url"]
                if out.get("description"):
                    break
    if out.get("description"):
        out["description"] = out["description"][:700]
    db.cache_set(ck, out, 30 * 86400 if out else 86400)
    return out
