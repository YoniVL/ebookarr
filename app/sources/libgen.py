"""Library Genesis (gratis, geen account).

LibGen heeft veel mirrors met wisselende HTML, dus dit is 'best effort':
we halen MD5-hashes uit de zoekpagina, filteren op epub waar mogelijk, en
lossen de download-URL op via de bekende mirror-pagina's.
Werkt de standaardlijst niet, zet dan een mirror vast bij Instellingen.
"""
import re
import time

import requests

from . import _common as c

NAME = "libgen"
LABEL = "Library Genesis"

MIRRORS = ("libgen.is", "libgen.st", "libgen.rs", "libgen.gs", "libgen.la",
           "libgen.li", "libgen.vg")

# LibGen-mirrors liggen vaak plat; hou het zoeken kort zodat de rest van de
# bronnen (en het 'Kies zelf'-scherm) niet minutenlang blijven hangen.
_REQ_TIMEOUT = 6
_SEARCH_BUDGET = 12  # seconden totaal over alle mirrors samen

_MD5_RE = re.compile(r"[a-fA-F0-9]{32}")
_ROW_RE = re.compile(r"<tr[ >].*?</tr>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_EXT_RE = re.compile(r"\b(epub|mobi|azw3|azw|pdf|fb2|djvu)\b", re.IGNORECASE)
_SIZE_RE = re.compile(r"([\d.]+)\s*(kb|mb|gb)", re.IGNORECASE)


def enabled():
    from .. import db
    return db.get_setting("src_libgen") == "1"


def _hosts():
    from .. import db
    m = (db.get_setting("libgen_mirror") or "").strip().replace("https://", "").strip("/")
    return [m] if m else list(MIRRORS)


def _rows_with_md5(page):
    """[(md5, plain-tekst van de rij)] uit een zoekresultaten-pagina."""
    out = []
    for row in _ROW_RE.findall(page):
        m = _MD5_RE.search(row)
        if not m:
            continue
        out.append((m.group(0).lower(), re.sub(r"\s+", " ", _TAG_RE.sub(" ", row)).strip()))
    # dedupe op md5, eerste voorkomen wint
    seen, uniq = set(), []
    for md5, txt in out:
        if md5 not in seen:
            seen.add(md5)
            uniq.append((md5, txt))
    return uniq


def _size_bytes(text):
    m = _SIZE_RE.search(text)
    if not m:
        return None
    n = float(m.group(1))
    return int(n * {"kb": 1024, "mb": 1024**2, "gb": 1024**3}[m.group(2).lower()])


def search(book: dict):
    q = " ".join(p for p in (book.get("title"), book.get("author")) if p).strip()
    if not q:
        return []
    s = c.http()
    deadline = time.monotonic() + _SEARCH_BUDGET
    for host in _hosts():
        if time.monotonic() > deadline:
            c.log.info("LibGen: tijdsbudget op, gestopt na %s", host)
            break
        try:
            cands = _search_host(s, host, book, q, deadline)
        except requests.RequestException as e:
            c.log.info("LibGen %s onbereikbaar: %s", host, e)
            continue
        if cands:
            return cands
    return []


def _search_host(s, host, book, q, deadline):
    pages = []
    for url, params in (
        (f"https://{host}/fiction/", {"q": q}),
        (f"https://{host}/index.php", {"req": q, "res": 100}),
        (f"https://{host}/search.php", {"req": q, "res": 100, "column": "def"}),
    ):
        if time.monotonic() > deadline:
            break
        try:
            r = s.get(url, params=params, timeout=_REQ_TIMEOUT)
            if r.ok and "md5" in r.text.lower():
                pages.append(r.text)
        except requests.RequestException:
            continue
    want_title = book.get("title") or ""
    want_author = book.get("author") or ""
    out, seen = [], set()
    for page in pages:
        for md5, text in _rows_with_md5(page):
            if md5 in seen:
                continue
            ext_m = _EXT_RE.search(text)
            ext = ext_m.group(1).lower() if ext_m else "epub"
            if ext in ("pdf", "fb2", "djvu"):
                continue
            # de rij bevat meestal 'titel ... auteur'; losjes checken
            if want_title and not c.title_matches(want_title, text):
                continue
            if want_author and not c.author_matches(want_author, text):
                continue
            seen.add(md5)
            out.append(c.candidate(
                NAME, title=want_title, author=want_author, fmt=ext,
                size=_size_bytes(text), detail=f"LibGen ({host})",
                download={"md5": md5, "host": host},
            ))
            if len(out) >= 12:
                return out
    return out


def _resolve_url(s, md5, host):
    for url in (
        f"https://libgen.li/ads.php?md5={md5}",
        f"https://{host}/ads.php?md5={md5}",
        f"http://library.lol/main/{md5}",
        f"http://libgen.li/main/{md5}",
    ):
        try:
            r = s.get(url, timeout=12)
        except requests.RequestException:
            continue
        if not r.ok:
            continue
        # zoek een concrete download-link op de pagina
        for m in re.finditer(r'href="([^"]+)"[^>]*>\s*(?:GET|Download|Cloudflare|IPFS)', r.text, re.I):
            link = m.group(1)
            if link.startswith("/"):
                link = f"https://{url.split('/')[2]}{link}"
            elif not link.startswith("http"):
                link = f"https://{url.split('/')[2]}/{link}"
            return link
    # laatste poging: sommige mirrors serveren direct
    return f"https://libgen.li/get.php?md5={md5}"


def download(cand: dict, dest_dir):
    s = c.http()
    d = cand["download"]
    url = _resolve_url(s, d["md5"], d.get("host", "libgen.li"))
    return c.download_file(url, dest_dir, session=s,
                           ext=cand.get("format", "epub"), timeout=180)
