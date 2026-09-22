"""OPDS 1.2-catalogus zodat leesapps (KOReader, Thorium, Moon+ Reader, Librera,
Marvin, ...) rechtstreeks in je EbookArr-bibliotheek kunnen bladeren en downloaden.

Simpele, afhankelijkheidsloze XML. Alleen boeken met status 'downloaded' en een
bestaand bestand worden aangeboden.
"""
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from . import db

NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _e(s):
    return escape(str(s or ""))


def _downloaded_books():
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, author, language, isbn, cover_url, description, year, "
            "library_path, added_at FROM books "
            "WHERE status='downloaded' AND library_path IS NOT NULL "
            "ORDER BY title COLLATE NOCASE"
        )]
    return [b for b in rows if b["library_path"] and Path(b["library_path"]).is_file()]


def book_file(book_id):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT title, author, library_path FROM books WHERE id=? AND status='downloaded'",
            (book_id,),
        ).fetchone()
    if not row or not row["library_path"]:
        return None
    p = Path(row["library_path"])
    return (p, row["title"], row["author"]) if p.is_file() else None


def _feed(base, self_path, title, feed_id, body, kind=ACQ):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:dc="http://purl.org/dc/terms/" '
        'xmlns:opds="http://opds-spec.org/2010/catalog">\n'
        f"  <id>{_e(feed_id)}</id>\n"
        f"  <title>{_e(title)}</title>\n"
        f"  <updated>{_now()}</updated>\n"
        f'  <link rel="self" href="{_e(base + self_path)}" type="{kind}"/>\n'
        f'  <link rel="start" href="{_e(base)}/opds" type="{NAV}"/>\n'
        f"{body}"
        "</feed>\n"
    )


def _nav_entry(base, path, title, content, kind=NAV):
    return (
        "  <entry>\n"
        f"    <id>{_e(base + path)}</id>\n"
        f"    <title>{_e(title)}</title>\n"
        f"    <updated>{_now()}</updated>\n"
        f"    <content type=\"text\">{_e(content)}</content>\n"
        f'    <link rel="subsection" href="{_e(base + path)}" type="{kind}"/>\n'
        "  </entry>\n"
    )


def _book_entry(base, b):
    bid = b["id"]
    lang = b.get("language") or ""
    parts = [
        "  <entry>\n",
        f"    <id>urn:ebookarr:book:{bid}</id>\n",
        f"    <title>{_e(b['title'])}</title>\n",
        f"    <updated>{_now()}</updated>\n",
    ]
    if b.get("author"):
        parts.append(f"    <author><name>{_e(b['author'])}</name></author>\n")
    if lang:
        parts.append(f"    <dc:language>{_e(lang)}</dc:language>\n")
    if b.get("year"):
        parts.append(f"    <dc:issued>{_e(b['year'])}</dc:issued>\n")
    if b.get("description"):
        parts.append(f'    <summary type="text">{_e(b["description"][:1500])}</summary>\n')
    cover = f"{base}/opds/cover/{bid}"
    parts.append(f'    <link rel="http://opds-spec.org/image" href="{_e(cover)}" type="image/jpeg"/>\n')
    parts.append(f'    <link rel="http://opds-spec.org/image/thumbnail" href="{_e(cover)}" type="image/jpeg"/>\n')
    parts.append(
        f'    <link rel="http://opds-spec.org/acquisition" '
        f'href="{_e(base)}/opds/download/{bid}" type="application/epub+zip"/>\n')
    parts.append("  </entry>\n")
    return "".join(parts)


def root(base):
    n = len(_downloaded_books())
    body = (
        _nav_entry(base, "/opds/all", "Alle boeken", f"{n} boeken in je bibliotheek", ACQ)
        + _nav_entry(base, "/opds/recent", "Recent toegevoegd", "De laatste 30", ACQ)
        + _nav_entry(base, "/opds/authors", "Op auteur", "Blader per auteur", NAV)
    )
    return _feed(base, "/opds", "EbookArr", f"{base}/opds", body, NAV)


def all_books(base):
    body = "".join(_book_entry(base, b) for b in _downloaded_books())
    return _feed(base, "/opds/all", "Alle boeken", f"{base}/opds/all", body)


def recent(base):
    books = sorted(_downloaded_books(), key=lambda b: b.get("added_at") or "", reverse=True)[:30]
    body = "".join(_book_entry(base, b) for b in books)
    return _feed(base, "/opds/recent", "Recent toegevoegd", f"{base}/opds/recent", body)


def authors(base):
    counts = {}
    for b in _downloaded_books():
        a = (b.get("author") or "Onbekend").strip()
        counts[a] = counts.get(a, 0) + 1
    body = "".join(
        _nav_entry(base, f"/opds/author/{_url(a)}", a, f"{c} boek{'en' if c != 1 else ''}", ACQ)
        for a, c in sorted(counts.items(), key=lambda kv: kv[0].lower())
    )
    return _feed(base, "/opds/authors", "Op auteur", f"{base}/opds/authors", body, NAV)


def by_author(base, name):
    want = (name or "").strip().lower()
    books = [b for b in _downloaded_books() if (b.get("author") or "").strip().lower() == want]
    body = "".join(_book_entry(base, b) for b in books)
    return _feed(base, f"/opds/author/{_url(name)}", name or "Auteur",
                 f"{base}/opds/author/{_url(name)}", body)


def _url(s):
    from urllib.parse import quote
    return quote((s or "").strip(), safe="")
