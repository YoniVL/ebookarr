"""Project Gutenberg via Gutendex (gratis, publiek domein, geen account).
API: https://gutendex.com/"""
from . import _common as c

NAME = "gutenberg"
LABEL = "Project Gutenberg"
API = "https://gutendex.com/books/"


def enabled():
    from .. import db
    return db.get_setting("src_gutenberg") == "1"


def search(book: dict):
    q = " ".join(p for p in (book.get("title"), book.get("author")) if p).strip()
    if not q:
        return []
    s = c.http()
    # gutendex is soms traag/plat; kort timeout zodat de rest van de bronnen niet wacht
    r = s.get(API, params={"search": q}, timeout=8)
    r.raise_for_status()
    out = []
    for item in r.json().get("results", [])[:20]:
        title = item.get("title") or ""
        authors = ", ".join(a.get("name", "") for a in item.get("authors", []))
        if not c.title_matches(book.get("title") or "", title):
            continue
        if not c.author_matches(book.get("author") or "", authors):
            continue
        fmts = item.get("formats", {}) or {}
        epub_url = (fmts.get("application/epub+zip")
                    or fmts.get("application/epub"))
        if not epub_url:
            continue
        langs = item.get("languages") or []
        out.append(c.candidate(
            NAME, title=title, author=authors, fmt="epub",
            language=langs[0] if langs else None,
            detail="Project Gutenberg",
            download={"url": epub_url},
        ))
    return out


def download(cand: dict, dest_dir):
    return c.download_file(cand["download"]["url"], dest_dir, ext="epub")
