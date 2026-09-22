"""Torrents via Prowlarr — de bestaande zoek/grab-flow, verpakt als 'bron'.
Staat altijd als laatste in de volgorde en kan niet uitgezet worden (val-terug)."""

NAME = "prowlarr"
LABEL = "Torrents (Prowlarr)"


def enabled():
    from .. import db
    return bool(db.get_setting("prowlarr_url") and db.get_setting("prowlarr_api_key"))


def search(book: dict):
    from .. import services

    out = []
    for r in services.search_book(book, epub_only=True):
        out.append({
            "source": NAME,
            "kind": "torrent",
            "title": r.get("title") or "",
            "author": book.get("author") or "",
            "format": "epub" if r.get("is_epub") else ("mobi" if r.get("is_kindle") else "epub"),
            "language": None,
            "size": r.get("size"),
            "year": None,
            "detail": f"{r.get('indexer') or 'indexer'} · {r.get('seeders') or 0} seeders",
            "seeders": r.get("seeders") or 0,
            "sane": r.get("sane", True),
            "sane_reason": r.get("sane_reason", ""),
            "is_epub": r.get("is_epub", False),
            "is_kindle": r.get("is_kindle", False),
            "download": {
                "download_url": r.get("download_url"),
                "guid": r.get("guid"),
                "indexer": r.get("indexer"),
                "seeders": r.get("seeders"),
                "title": r.get("title"),
            },
        })
    return out


def download(cand: dict, dest_dir):  # noqa: ARG001
    """Torrents worden niet direct gedownload; zie services.grab_release."""
    raise RuntimeError("prowlarr-bron gebruikt de torrent-flow, niet download()")


IS_TORRENT = True
