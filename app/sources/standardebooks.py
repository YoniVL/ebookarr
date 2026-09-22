"""Standard Ebooks (gratis, publiek domein, mooi opgemaakt, geen account).

De OPDS-feed vereist tegenwoordig een account, dus we scrapen de (publieke)
zoekpagina en bouwen de download-URL uit het boekpad.
"""
import html
import re

from . import _common as c

NAME = "standardebooks"
LABEL = "Standard Ebooks"
BASE = "https://standardebooks.org"

# <a href="/ebooks/<author>/<book>"> ... <span property="schema:name">Titel</span>
#   ... <p class="author">... <a ...>Auteur</a>
_ENTRY_RE = re.compile(
    r'<a[^>]+href="(/ebooks/[a-z0-9-]+/[a-z0-9-]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_NAME_RE = re.compile(r'property="schema:name"[^>]*>([^<]+)<', re.IGNORECASE)
_AUTHOR_RE = re.compile(r'property="schema:author"[^>]*>([^<]+)<', re.IGNORECASE)


def enabled():
    from .. import db
    return db.get_setting("src_standardebooks") == "1"


def search(book: dict):
    q = " ".join(p for p in (book.get("title"), book.get("author")) if p).strip()
    if not q:
        return []
    s = c.http()
    r = s.get(f"{BASE}/ebooks", params={"query": q}, timeout=15)
    r.raise_for_status()
    page = r.text

    seen, out = set(), []
    for m in _ENTRY_RE.finditer(page):
        path, inner = m.group(1), m.group(2)
        if path in seen:
            continue
        seen.add(path)
        nm = _NAME_RE.search(inner)
        au = _AUTHOR_RE.search(inner)
        title = (html.unescape(nm.group(1).strip()) if nm
                 else path.split("/")[-1].replace("-", " ").title())
        author = (html.unescape(au.group(1).strip()) if au
                  else path.split("/")[-2].replace("-", " ").title())
        if not c.title_matches(book.get("title") or "", title):
            continue
        if not c.author_matches(book.get("author") or "", author):
            continue
        out.append(c.candidate(
            NAME, title=title, author=author, fmt="epub", language="en",
            detail="Standard Ebooks", download={"page": f"{BASE}{path}"},
        ))
        if len(out) >= 10:
            break
    return out


# de "gewone" .epub (niet .kepub.epub, niet _advanced.epub)
_EPUB_HREF_RE = re.compile(r'href="(/ebooks/[^"]+?/downloads/[^"]+?[^.]\.epub)"', re.IGNORECASE)
_REFRESH_RE = re.compile(r'http-equiv="refresh"[^>]*url=([^"\']+)', re.IGNORECASE)


def download(cand: dict, dest_dir):
    s = c.http()
    page = cand["download"]["page"]
    r = s.get(page, timeout=15)
    r.raise_for_status()
    m = _EPUB_HREF_RE.search(r.text) or re.search(r'href="([^"]+\.epub)"', r.text, re.I)
    if not m:
        raise RuntimeError("geen epub-downloadlink op de Standard Ebooks-pagina")
    url = m.group(1)
    if url.startswith("/"):
        url = BASE + url
    # SE toont eerst een "Your download has started"-tussenpagina; ?source=download
    # slaat die over. Lukt dat toch niet, volg dan de meta-refresh.
    sep = "&" if "?" in url else "?"
    r = s.get(f"{url}{sep}source=download", timeout=30, stream=True)
    r.raise_for_status()
    if "text/html" in r.headers.get("content-type", "").lower():
        ref = _REFRESH_RE.search(r.text)
        if not ref:
            raise RuntimeError("Standard Ebooks gaf een webpagina i.p.v. het epub-bestand")
        nxt = ref.group(1).strip()
        nxt = BASE + nxt if nxt.startswith("/") else nxt
        return c.download_file(nxt, dest_dir, session=s, ext="epub")
    return c.save_response(r, dest_dir, ext="epub")
