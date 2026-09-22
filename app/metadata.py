"""Boek-metadata opzoeken via Google Books + Open Library.

Beide bronnen zijn gratis en zonder API-key. Google Books heeft de beste
dekking voor Nederlandstalige boeken; Open Library vult aan voor Engelstalige
en oudere titels en levert betrouwbare covers via ISBN.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("ebookarr.metadata")

GOOGLE_URL = "https://www.googleapis.com/books/v1/volumes"
OPENLIB_URL = "https://openlibrary.org/search.json"
HTTP_TIMEOUT = 7

# UI-taalcode -> (google langRestrict, openlibrary MARC-code)
_LANG_MAP = {"nl": ("nl", "dut"), "en": ("en", "eng")}

# Simpele in-process cache (query -> (tijd, resultaat)); de zware API-calls
# hebben daarnaast een eigen cache op schijf (zie _disk_cached)
_cache: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 3600
# Na een 429 van Google even niet meer bij Google aankloppen
_google_cooldown_until = 0.0


class MetadataUnavailable(RuntimeError):
    """Geen van de metadata-bronnen was bereikbaar."""


def _normalize_isbn(q):
    """Geeft de kale ISBN terug (10 of 13 cijfers) als de zoekterm een ISBN is,
    anders None."""
    s = re.sub(r"[\s-]", "", (q or "").strip())
    if re.fullmatch(r"\d{13}", s) or re.fullmatch(r"\d{9}[\dxX]", s):
        return s.upper()
    return None


def _language_from_isbn(isbn):
    """De ISBN-13 'registration group' zegt in welke taalregio de editie is
    uitgegeven — betrouwbaarder dan de (soms foute) taal-tag van Google/OpenLibrary.
    978-0 / 978-1 = Engelstalig, 978-3 = Duits, 978-90/978-94 = Nederlands, enz."""
    s = re.sub(r"[^0-9]", "", isbn or "")
    if len(s) != 13:
        return None
    if s.startswith(("9780", "9781")):
        return "en"
    if s.startswith("9783"):
        return "de"
    if s.startswith(("97890", "97894")):
        return "nl"
    if s.startswith("9782"):
        return "fr"
    if s.startswith("97888"):
        return "it"
    if s.startswith("97884"):
        return "es"
    return None


def _norm_lang(code):
    if not code:
        return None
    # epub's dc:language gebruikt vaak een BCP-47-achtige vorm ("en-US", "nl_BE");
    # alleen de primaire subtag telt
    code = code.lower().replace("_", "-").split("-")[0]
    if code in ("nl", "nld", "dut"):
        return "nl"
    if code in ("en", "eng"):
        return "en"
    return code


def _norm_title(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def main_title(s):
    """Hoofdtitel zonder ondertitel/reeks-aanhangsel, voor het ontdubbelen.
    'Death's Domain: A Discworld Mapp' en 'Mort (Discworld #4)' -> 'Death's Domain' / 'Mort'."""
    s = (s or "").strip()
    s = re.split(r"\s*[:(\[]", s, maxsplit=1)[0]
    s = re.sub(r"\s*[-–—]\s*(a\s+)?discworld.*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*[-–—]\s*(the\s+)?\w+\s+(novel|book|saga|series)\s*$", "", s, flags=re.IGNORECASE)
    return _norm_title(s)


_REL_STOP = {"the", "a", "an", "of", "and", "to", "de", "het", "een", "en",
             "van", "in", "on", "or", "for"}

# "over dit boek"-uitgaven i.p.v. het boek zelf
_COMPANION_RE = re.compile(
    r"(?i)\b(sparknotes|cliffs?notes|study guide|reader'?s guide|summary|analysis|"
    r"companion|a guide to|guide to|adaptation|abridged|notes on|casebook|"
    r"critical (?:guide|edition|companion)|bright notes|coles notes|"
    r"colou?ring book|activity book|sticker book|quiz book)\b")
_POSSESSIVE_RE = re.compile(r"(?i)^[a-z.\-' ]+'s\s+\S")  # "George Orwell's 1984"


def _rel_words(s):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in _REL_STOP and len(w) > 1]


def _rank_by_relevance(items, query):
    """Sorteer op hoe goed titel+auteur bij de zoekterm passen, en gooi
    resultaten weg die er niets mee te maken hebben ('Go Ask Alice' bij 'dune')."""
    qwords = _rel_words(query)
    if not qwords:                       # alleen stopwoorden -> API-volgorde houden
        return items
    qnorm = _norm_title(query)
    qset = set(qwords)
    scored = []
    for it in items:
        title = it.get("title") or ""
        tnorm = _norm_title(title)
        twords = set(_rel_words(title))
        awords = set(_rel_words(it.get("author")))
        title_hits = len(qset & twords)
        author_hits = len(qset & awords)
        if title_hits + author_hits == 0:
            continue                     # geen enkel zoekwoord terug -> weg
        score = (title_hits + author_hits) / len(qset)
        if qnorm and qnorm == tnorm:
            score += 2                    # exacte titel
        elif qnorm and (tnorm.startswith(qnorm) or qnorm in tnorm):
            score += 1
        if title_hits and author_hits:
            score += 1                    # "titel auteur" gezocht en beide kloppen
        if title_hits == len(qset):
            score += 0.5
        companion = bool(_COMPANION_RE.search(title) or _POSSESSIVE_RE.match(title))
        scored.append((score, companion, it))
    if not scored:
        return items                     # niets haalde het -> liever iets dan niets
    scored.sort(key=lambda t: t[0], reverse=True)
    # is er een fatsoenlijke "echte" treffer? dan studiegidsen/"X's Y" helemaal weg
    best_real = max((s for s, comp, _ in scored if not comp), default=0)
    if best_real > 0.5:
        scored = [t for t in scored if not t[1]]
    return [it for _, _, it in scored]


def _ol_cover(*, cover_id=None, isbn=None):
    """Open Library-cover-URL. ?default=false -> ontbrekende cover geeft 404
    (i.p.v. een leeg plaatje), zodat de UI de nette placeholder toont."""
    if cover_id:
        return f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg?default=false"
    if isbn:
        return f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false"
    return ""


def _dedupe(items):
    seen_isbn, seen_ta, out = set(), set(), []
    for it in items:
        isbn = (it.get("isbn") or "").replace("-", "")
        key = (main_title(it.get("title")), _norm_title((it.get("author") or "").split(",")[0]))
        if isbn and isbn in seen_isbn:
            continue
        if key[0] and key in seen_ta:
            continue
        if isbn:
            seen_isbn.add(isbn)
        if key[0]:
            seen_ta.add(key)
        out.append(it)
    return out


_DISK_TTL = 7 * 24 * 3600  # metadata verandert nauwelijks; een week bewaren


def _disk_cached(prefix, key_parts, producer, ttl=_DISK_TTL):
    """Haal `producer()` op, maar sla het resultaat een week op in de DB.
    Bij een fout: val terug op een (verlopen) DB-kopie als die er is."""
    from . import db
    key = prefix + "|" + "|".join(str(p) for p in key_parts)
    hit = db.cache_get(key)
    if hit is not None:
        return hit
    try:
        result = producer()
    except Exception:
        stale = db.cache_get(key + "|stale")
        if stale is not None:
            log.info("metadata: %s onbereikbaar, oude kopie gebruikt", prefix)
            return stale
        raise
    db.cache_set(key, result, ttl)
    db.cache_set(key + "|stale", result, ttl * 8)  # langere noodkopie
    return result


def _google(query, language, limit, start_index=0):
    return _disk_cached(
        "google", (query.lower(), language, limit, start_index),
        lambda: _google_raw(query, language, limit, start_index))


def _google_raw(query, language, limit, start_index=0):
    global _google_cooldown_until
    if time.time() < _google_cooldown_until:
        raise RuntimeError("Google Books tijdelijk overgeslagen (rate limit)")

    isbn = _normalize_isbn(query)
    q = f"isbn:{isbn}" if isbn else query
    params = {
        "q": q, "maxResults": min(limit, 40), "startIndex": start_index,
        "printType": "books", "country": "NL",
    }
    if language in _LANG_MAP and not isbn:
        params["langRestrict"] = _LANG_MAP[language][0]
    resp = requests.get(GOOGLE_URL, params=params, timeout=HTTP_TIMEOUT)
    if resp.status_code == 429:
        # oplopende afkoeltijd: 10 min -> 30 -> 60 (blijf niet bonzen)
        prev = max(_google_cooldown_until - time.time(), 0)
        _google_cooldown_until = time.time() + min(max(prev * 3, 600), 3600)
        raise RuntimeError("Google Books rate limit (429)")
    resp.raise_for_status()
    data = resp.json()

    out = []
    for item in data.get("items", []):
        vi = item.get("volumeInfo", {})
        isbn = ""
        for ident in vi.get("industryIdentifiers", []):
            if ident.get("type") == "ISBN_13":
                isbn = ident.get("identifier", "")
                break
            if ident.get("type") == "ISBN_10" and not isbn:
                isbn = ident.get("identifier", "")
        year = None
        m = re.match(r"(\d{4})", vi.get("publishedDate", "") or "")
        if m:
            year = int(m.group(1))
        cover = (vi.get("imageLinks") or {}).get("thumbnail", "")
        cover = cover.replace("http://", "https://").replace("&edge=curl", "") if cover else ""
        if not cover:
            cover = _ol_cover(isbn=isbn)
        out.append({
            "source_id": "googlebooks:" + item.get("id", ""),
            "title": vi.get("title", "").strip(),
            "author": ", ".join(vi.get("authors", []) or []),
            "year": year,
            "isbn": isbn,
            "language": _language_from_isbn(isbn) or _norm_lang(vi.get("language")),
            "cover_url": cover,
            "description": (vi.get("description") or "")[:2000],
        })
    return out


def _openlibrary(query, language, limit):
    return _disk_cached(
        "openlib", (query.lower(), language, limit),
        lambda: _openlibrary_raw(query, language, limit))


def _openlibrary_raw(query, language, limit):
    isbn = _normalize_isbn(query)
    if isbn:
        q = isbn
    elif language in _LANG_MAP:
        q = f"{query} language:{_LANG_MAP[language][1]}"
    else:
        q = query
    params = {
        "q": q,
        "limit": min(limit, 20),
        "fields": "key,title,author_name,first_publish_year,isbn,cover_i,language",
    }
    resp = requests.get(OPENLIB_URL, params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    out = []
    for doc in data.get("docs", []):
        isbn = (doc.get("isbn") or [""])[0]
        cover = _ol_cover(cover_id=doc.get("cover_i"), isbn=isbn)
        langs = doc.get("language") or []
        out.append({
            "source_id": "openlibrary:" + doc.get("key", "").split("/")[-1],
            "title": (doc.get("title") or "").strip(),
            "author": ", ".join(doc.get("author_name", []) or []),
            "year": doc.get("first_publish_year"),
            "isbn": isbn,
            "language": _language_from_isbn(isbn) or (_norm_lang(langs[0]) if langs else None),
            "cover_url": cover,
            "description": "",
        })
    return out


def _google_paged(query, language, want):
    """Meerdere pagina's Google Books ophalen (max ~200)."""
    out = []
    for start in range(0, min(want, 200), 40):
        page = _google(query, language, 40, start_index=start)
        if not page:
            break
        out += page
        if len(page) < 40:
            break
    return out


def _ol_by_author(name, cap=250):
    return _disk_cached("ol_author", (name.lower(), cap),
                        lambda: _ol_by_author_raw(name, cap))


def _ol_by_author_raw(name, cap=250):
    """Werken van een auteur via search.json (mét taal-info per werk)."""
    surname = _norm_title(name.split()[-1]) if name.split() else _norm_title(name)
    out, page = [], 1
    while len(out) < cap:
        resp = requests.get(OPENLIB_URL, params={
            "author": name, "limit": 100, "page": page,
            "fields": "key,title,author_name,first_publish_year,isbn,cover_i,language",
        }, timeout=HTTP_TIMEOUT + 4)
        resp.raise_for_status()
        docs = resp.json().get("docs", [])
        if not docs:
            break
        for doc in docs:
            authors = " ".join(doc.get("author_name") or [])
            if surname and surname not in _norm_title(authors):
                continue
            isbn = (doc.get("isbn") or [""])[0]
            langs = doc.get("language") or []
            out.append({
                "source_id": "openlibrary:" + doc.get("key", "").split("/")[-1],
                "title": (doc.get("title") or "").strip(),
                "author": authors or name,
                "year": doc.get("first_publish_year"),
                "isbn": isbn,
                "language": _norm_lang(langs[0]) if langs else None,
                "cover_url": _ol_cover(cover_id=doc.get("cover_i"), isbn=isbn),
                "description": "",
            })
        if len(docs) < 100:
            break
        page += 1
    return out


def _ol_by_series(series, author, cap=150):
    return _disk_cached("ol_series", (series.lower(), author.lower(), cap),
                        lambda: _ol_by_series_raw(series, author, cap))


def _ol_by_series_raw(series, author, cap=150):
    """Open Library heeft géén betrouwbaar `series`-veld in zoekresultaten, maar
    `series:"X"` én `subject:"X"` als query werken wél — subject is vaak veel
    completer (Discworld: series->24, subject->~70). We voegen beide samen en
    filteren op de achternaam van de auteur."""
    surname = _norm_title(author.split()[-1]) if author.split() else ""
    variants = [series]
    m = re.match(r"(?i)^(the|de|het)\s+(.+)$", series)
    if m:
        variants.append(m.group(2))

    out, seen = [], set()
    for field in ("series", "subject"):
        for variant in variants:
            try:
                resp = requests.get(OPENLIB_URL, params={
                    "q": f'{field}:"{variant}"', "limit": 100,
                    "fields": "key,title,author_name,first_publish_year,isbn,cover_i,language",
                }, timeout=HTTP_TIMEOUT + 4)
                resp.raise_for_status()
                docs = resp.json().get("docs", [])
            except Exception:  # noqa: BLE001
                continue
            for doc in docs:
                key = doc.get("key")
                if key in seen:
                    continue
                authors = " ".join(doc.get("author_name") or [])
                if surname and surname not in _norm_title(authors):
                    continue
                seen.add(key)
                isbn = (doc.get("isbn") or [""])[0]
                langs = doc.get("language") or []
                out.append({
                    "source_id": "openlibrary:" + (key or "").split("/")[-1],
                    "title": (doc.get("title") or "").strip(),
                    "author": authors or author,
                    "year": doc.get("first_publish_year"),
                    "isbn": isbn,
                    "language": _norm_lang(langs[0]) if langs else None,
                    "cover_url": _ol_cover(cover_id=doc.get("cover_i"), isbn=isbn),
                    "description": "",
                })
                if len(out) >= cap:
                    return out
    return out


_BOXSET_RE = re.compile(
    r"\b(\d+\s*books?\s*(set|collection|bundle|box)|box(ed)?\s*set|"
    r"book\s*bundle|complete\s*(series|collection)|\bomnibus\b)\b",
    re.IGNORECASE,
)


def by_series(series: str, author: str = "", languages=None, limit: int = 120):
    """Boeken uit één reeks. Primair via Open Library's `series:`-zoek (vindt ook
    delen zónder de reeksnaam in de titel, zoals 'Guards! Guards!' in Discworld),
    aangevuld met een titel-zoektocht op de reeksnaam."""
    series = (series or "").strip()
    author = (author or "").strip()
    if not series:
        return []
    n_series = _norm_title(series)
    surname = _norm_title(author.split()[-1]) if author.split() else ""
    combined, errors = [], []

    try:
        combined += _ol_by_series(series, author)
    except Exception as e:  # noqa: BLE001
        errors.append("openlibrary")
        log.warning("Open Library reeks-zoeken mislukt (%s): %s", series, e)

    # Aanvulling: gewone titel-zoektocht, alleen treffers waar de reeksnaam in de
    # titel staat (vangt bv. companions/spin-offs die OL niet aan de reeks koppelt).
    try:
        q = f"{series} {author}".strip()
        extra = _google_paged(q, None, 40) + _openlibrary(q, None, 40)
        for r in extra:
            if n_series in _norm_title(r.get("title")):
                combined.append(r)
    except Exception:  # noqa: BLE001
        errors.append("titelzoek")

    if not combined and len(errors) >= 2:
        raise MetadataUnavailable("Kon de metadata-bronnen niet bereiken.")

    cleaned = []
    for c in combined:
        title = (c.get("title") or "").strip()
        if not title or not _mostly_latin(title):
            continue
        if _BOXSET_RE.search(title) or _FOREIGN_EDITION_RE.search(title):
            continue
        if surname and surname not in _norm_title(c.get("author")):
            continue
        cleaned.append(c)
    combined = _dedupe(cleaned)

    # Een reeks heeft weinig delen: geen taalfilter (edities worden soms fout
    # gelabeld), maar wel de auteur-taalregel als stempel.
    en = sum(1 for c in combined if c.get("language") == "en")
    nl = sum(1 for c in combined if c.get("language") == "nl")
    stamp = "en" if en >= nl else "nl"
    for c in combined:
        c["language"] = stamp

    return combined[:limit]


def by_author(name: str, languages=None, limit: int = 200):
    """Zo volledig mogelijke bibliografie van één auteur: gepagineerde
    Open Library (author-zoek) + gepagineerde Google Books."""
    name = (name or "").strip()
    if not name:
        return []
    combined, errors = [], []

    try:
        combined += _ol_by_author(name)
    except Exception as e:  # noqa: BLE001
        errors.append("openlibrary")
        log.warning("Open Library auteur-zoeken mislukt (%s): %s", name, e)

    try:
        combined += _google_paged(f'inauthor:"{name}"', None, 120)
    except Exception as e:  # noqa: BLE001
        errors.append("google")
        log.warning("Google Books auteur-zoeken mislukt (%s): %s", name, e)

    if not combined and len(errors) == 2:
        raise MetadataUnavailable(
            "Kon Google Books noch Open Library bereiken. Probeer het zo nog eens."
        )

    n_author = _norm_title(name)
    cleaned = []
    for c in combined:
        title = (c.get("title") or "").strip()
        if not title or not _mostly_latin(title):
            continue
        if _norm_title(title) == n_author:                       # "titel" = auteursnaam
            continue
        if _FOREIGN_EDITION_RE.search(title):                    # "(German Edition)" e.d.
            continue
        if c.get("language") and c["language"] not in ("en", "nl"):  # vertaalde editie
            continue
        cleaned.append(c)
    combined = _dedupe(cleaned)

    # Schrijft deze auteur in het Engels of niet? (losse edities worden soms als
    # vertaling gelabeld) -> stamp één taal op elk boek van de auteur.
    en = sum(1 for c in combined if c.get("language") == "en")
    nl = sum(1 for c in combined if c.get("language") == "nl")
    if en or nl:
        stamp = "en" if en >= nl else "nl"
        for c in combined:
            c["language"] = stamp

    return _apply_language_filter(combined, languages)[:limit]


_FOREIGN_EDITION_RE = re.compile(
    r"\(\s*(?:german|deutsch|spanish|español|espanol|french|fran[çc]ais|italian|"
    r"italiano|portuguese|russian|turkish|polish|japanese|chinese|korean|"
    r"niederländische?)\s*(?:edition|ausgabe|edici[óo]n|[ée]dition)?\s*\)",
    re.IGNORECASE,
)


def _mostly_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    latin = sum(1 for c in letters if c.isascii() or c in "áàâäéèêëíìîïóòôöúùûüñçøåæœ")
    return latin / len(letters) >= 0.6


def _apply_language_filter(items, languages):
    """Houd edities in een taal die de gebruiker leest bovenaan; edities in een
    duidelijk ándere taal vallen weg (tenzij er dan niets overblijft).

    `items` is al op relevantie gesorteerd (zie `_rank_by_relevance`). De
    best passende titel mag nooit wegvallen puur omdat een bron (vaak Open
    Library) toevallig een verkeerde of niet-representatieve taal-tag geeft
    aan de editie die in de zoekresultaten staat."""
    if not languages:
        return items
    langs = set(languages)
    preferred = [it for it in items if it.get("language") in langs]
    unknown = [it for it in items if not it.get("language")]
    other = [it for it in items if it.get("language") and it.get("language") not in langs]
    kept = preferred + unknown
    if items and items[0] in other:
        kept = [items[0]] + kept
    return kept or (preferred + unknown + other)


_NAME_RE = re.compile(r"^[^\d]+$")


_TITLE_WORDS = {"the", "a", "an", "of", "and", "de", "het", "een", "van"}


def _looks_like_person(query: str) -> bool:
    parts = query.split()
    if not (2 <= len(parts) <= 4) or not _NAME_RE.match(query):
        return False
    # een bezittelijke vorm ("Barney's Version", "Sophie's Choice") is een titel,
    # nooit iemands naam
    if _POSSESSIVE_RE.match(query):
        return False
    # titels bevatten vaak lidwoorden/voorzetsels; namen zelden
    return not any(p.lower() in _TITLE_WORDS for p in parts)


def search(query: str, languages=None, limit: int = 12, deep: bool = False):
    query = (query or "").strip()
    if not query:
        return []
    languages = [x for x in (languages or []) if x] or None

    cache_key = f"{'d' if deep else 's'}|{','.join(languages) if languages else 'all'}|{query.lower()}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    # Diepe zoekopdracht op wat een naam lijkt -> volledige bibliografie van die
    # auteur (Terry Pratchett = ~70 boeken), en niets anders erdoorheen.
    if deep and _normalize_isbn(query) is None and _looks_like_person(query):
        try:
            result = by_author(query, languages, limit)
            _cache[cache_key] = (time.time(), result)
            return result
        except MetadataUnavailable:
            pass  # val terug op de gewone zoektocht

    # We halen ALLE talen op (geen langRestrict) en filteren daarna zelf, zodat
    # "en of nl" allebei kan en de juiste editie bovenaan komt.
    combined, errors = [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            "google": pool.submit(_google, query, None, limit + 8),
            "openlibrary": pool.submit(_openlibrary, query, None, limit + 8),
        }
        for name, fut in futures.items():
            try:
                combined += fut.result()
            except MetadataUnavailable:
                errors.append(name)
            except Exception as e:  # noqa: BLE001
                errors.append(name)
                log.warning("%s zoeken mislukt: %s", name, e)

    if not combined and "google" in errors and "openlibrary" in errors:
        raise MetadataUnavailable(
            "Kon Google Books noch Open Library bereiken. Probeer het zo nog eens, "
            "of gebruik 'Handmatig toevoegen'."
        )

    combined = [c for c in combined if c["title"]]
    combined = _dedupe(combined)

    isbn = _normalize_isbn(query)
    if isbn:
        # Exacte editie gevraagd: geen taalfilter, en de editie waarvan de ISBN
        # exact klopt vooraan.
        combined.sort(key=lambda c: (c.get("isbn") or "").replace("-", "") != isbn)
        result = combined[:limit]
    else:
        combined = _rank_by_relevance(combined, query)
        result = _apply_language_filter(combined, languages)[:limit]

    _cache[cache_key] = (time.time(), result)
    return result


