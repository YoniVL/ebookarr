import base64
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, db, logsetup, metadata, opds, scheduler, services, sources

logsetup.configure()
logging.getLogger("ebookarr").info("EbookArr start - versie %s", __version__)
db.init_db()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="EbookArr", lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------- Models ----------

class BookIn(BaseModel):
    title: str
    author: str = ""
    language: str | None = None
    isbn: str | None = None
    cover_url: str | None = None
    description: str | None = None
    year: int | None = None
    source_id: str | None = None
    format_preference: str = "epub"


class SettingsIn(BaseModel):
    prowlarr_url: str
    prowlarr_api_key: str = ""
    qbittorrent_url: str
    qbittorrent_username: str
    qbittorrent_password: str = ""
    search_interval_hours: int = 6
    auto_download: bool = True
    min_seeders: int = 1
    prowlarr_categories: str = "7000,7020"
    library_folder: str = ""
    reading_languages: str = "en,nl"
    stalled_hours: int = 24
    accept_kindle: bool = False
    calibre_path: str = ""
    embed_metadata: bool = True
    opds_auth: bool = False
    opds_user: str = "ebookarr"
    opds_password: str = ""
    src_gutenberg: bool = True
    src_standardebooks: bool = True
    src_libgen: bool = True
    src_zlibrary: bool = False
    libgen_mirror: str = ""
    zlib_domain: str = ""
    zlib_email: str = ""
    zlib_password: str = ""
    annas_key: str = ""


class MonitoredIn(BaseModel):
    monitored: bool


class BulkDeleteIn(BaseModel):
    ids: list[int]
    delete_files: bool = False


class FollowIn(BaseModel):
    type: str = "author"          # "author" of "series"
    name: str
    author: str = ""             # optioneel, om een reeks te verankeren


class PrefLangIn(BaseModel):
    pref_language: str            # "en", "nl" of "" (auto)


class SugBulkIn(BaseModel):
    ids: list[int]
    action: str                  # "accept" | "dismiss" | "restore"


# ---------- Frontend ----------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------- Settings ----------

_SECRET_PLACEHOLDER = "********"
_KNOWN_LANGS = ("en", "nl", "de", "fr", "es", "it")


def _lang_list(value: str):
    return [x.strip().lower() for x in (value or "").split(",")
            if x.strip().lower() in _KNOWN_LANGS]


def _clean_langs(value: str):
    langs = _lang_list(value)
    return ",".join(dict.fromkeys(langs)) or "en,nl"


@app.get("/api/settings")
def get_settings():
    s = db.get_all_settings()
    for k in ("qbittorrent_password", "prowlarr_api_key", "zlib_password",
              "zlib_email", "annas_key", "zlib_session", "opds_password"):
        s.pop(k, None)
    s["qbittorrent_password_set"] = bool(db.get_setting("qbittorrent_password"))
    s["prowlarr_api_key_set"] = bool(db.get_setting("prowlarr_api_key"))
    s["zlib_password_set"] = bool(db.get_setting("zlib_password"))
    s["zlib_email_set"] = bool(db.get_setting("zlib_email"))
    s["annas_key_set"] = bool(db.get_setting("annas_key"))
    s["opds_password_set"] = bool(db.get_setting("opds_password"))
    s["opds_url"] = f"http://{_lan_ip()}:8686/opds"
    s["zlib_quota"] = services.zlib_quota()
    s["calibre_found"] = services.find_ebook_convert()
    s["ebook_meta_found"] = services.find_ebook_meta()
    s["sources_enabled"] = [m.NAME for m in sources.enabled_in_order()]
    return s


def _lan_ip():
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("8.8.8.8", 80))
            return sk.getsockname()[0]
    except OSError:
        return "localhost"


@app.post("/api/settings")
def save_settings(payload: SettingsIn):
    db.set_setting("prowlarr_url", payload.prowlarr_url)
    db.set_setting("qbittorrent_url", payload.qbittorrent_url)
    db.set_setting("qbittorrent_username", payload.qbittorrent_username)
    db.set_setting("search_interval_hours", str(payload.search_interval_hours))
    db.set_setting("auto_download", "1" if payload.auto_download else "0")
    db.set_setting("min_seeders", str(payload.min_seeders))
    db.set_setting("prowlarr_categories", payload.prowlarr_categories.strip())
    db.set_setting("library_folder", payload.library_folder.strip())
    db.set_setting("reading_languages", _clean_langs(payload.reading_languages))
    db.set_setting("stalled_hours", str(payload.stalled_hours))
    db.set_setting("accept_kindle", "1" if payload.accept_kindle else "0")
    db.set_setting("calibre_path", payload.calibre_path.strip())
    db.set_setting("embed_metadata", "1" if payload.embed_metadata else "0")
    db.set_setting("opds_user", payload.opds_user.strip() or "ebookarr")
    for key in ("src_gutenberg", "src_standardebooks", "src_libgen", "src_zlibrary"):
        db.set_setting(key, "1" if getattr(payload, key) else "0")
    db.set_setting("libgen_mirror", payload.libgen_mirror.strip())
    zlib_changed = payload.zlib_domain.strip() != (db.get_setting("zlib_domain") or "")
    db.set_setting("zlib_domain", payload.zlib_domain.strip())

    # Lege / placeholder-secrets laten de opgeslagen waarde ongemoeid
    for key, val in (
        ("prowlarr_api_key", payload.prowlarr_api_key),
        ("qbittorrent_password", payload.qbittorrent_password),
        ("zlib_email", payload.zlib_email),
        ("zlib_password", payload.zlib_password),
        ("annas_key", payload.annas_key),
    ):
        if val and val != _SECRET_PLACEHOLDER:
            db.set_setting(key, val)
            if key in ("zlib_email", "zlib_password"):
                zlib_changed = True

    if not payload.opds_auth:
        db.set_setting("opds_password", "")
    elif payload.opds_password and payload.opds_password != _SECRET_PLACEHOLDER:
        db.set_setting("opds_password", payload.opds_password)

    if zlib_changed:  # gecachete Z-Library-sessie is nu mogelijk verkeerd
        db.set_setting("zlib_session", "")

    scheduler.apply_settings()
    return {"ok": True}


@app.post("/api/settings/test-prowlarr")
def test_prowlarr():
    try:
        status = services.get_prowlarr().test_connection()
        return {"ok": True, "version": status.get("version")}
    except services.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/settings/test-qbittorrent")
def test_qbittorrent():
    try:
        version = services.get_qbittorrent().test_connection()
        return {"ok": True, "version": version}
    except services.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/status")
def status():
    return {
        **services.health(),
        "version": __version__,
        "suggestions_pending": db.count_pending_suggestions(),
        "searching": services.search_progress(),
        "zlib_quota": services.zlib_quota(),
    }


@app.get("/api/activity")
def activity():
    return services.activity()


_BLANK_PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
             b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
             b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


@app.get("/api/cover")
def cover(isbn: str = "", title: str = "", author: str = "", u: str = ""):
    """Lost een werkende cover-URL op (met cache) en stuurt door; anders 1px-PNG
    zodat de <img> z'n onerror-placeholder toont."""
    url = services.resolve_cover(isbn=isbn, title=title, author=author, stored=u)
    if url:
        return RedirectResponse(url, status_code=302)
    return Response(_BLANK_PX, media_type="image/png", status_code=404,
                    headers={"Cache-Control": "public, max-age=86400"})


# ---------- OPDS-catalogus (voor leesapps) ----------

_OPDS_XML = "application/atom+xml;charset=utf-8"


def _opds_guard(request: Request):
    pw = db.get_setting("opds_password")
    if not pw:
        return
    user = db.get_setting("opds_user") or "ebookarr"
    hdr = request.headers.get("authorization", "")
    if hdr.startswith("Basic "):
        try:
            got_u, got_p = base64.b64decode(hdr[6:]).decode("utf-8", "replace").split(":", 1)
            if got_u == user and got_p == pw:
                return
        except (ValueError, UnicodeDecodeError):
            pass
    raise HTTPException(status_code=401, detail="OPDS-login vereist",
                        headers={"WWW-Authenticate": 'Basic realm="EbookArr"'})


def _opds_base(request: Request):
    return str(request.base_url).rstrip("/")


@app.get("/opds")
def opds_root(request: Request):
    _opds_guard(request)
    return Response(opds.root(_opds_base(request)), media_type=_OPDS_XML)


@app.get("/opds/all")
def opds_all(request: Request):
    _opds_guard(request)
    return Response(opds.all_books(_opds_base(request)), media_type=_OPDS_XML)


@app.get("/opds/recent")
def opds_recent(request: Request):
    _opds_guard(request)
    return Response(opds.recent(_opds_base(request)), media_type=_OPDS_XML)


@app.get("/opds/authors")
def opds_authors(request: Request):
    _opds_guard(request)
    return Response(opds.authors(_opds_base(request)), media_type=_OPDS_XML)


@app.get("/opds/author/{name}")
def opds_author(name: str, request: Request):
    _opds_guard(request)
    return Response(opds.by_author(_opds_base(request), name), media_type=_OPDS_XML)


@app.get("/opds/cover/{book_id}")
def opds_cover(book_id: int, request: Request):
    _opds_guard(request)
    with db.get_conn() as conn:
        row = conn.execute("SELECT isbn, title, author, cover_url FROM books WHERE id=?",
                           (book_id,)).fetchone()
    if not row:
        return Response(_BLANK_PX, media_type="image/png", status_code=404)
    url = services.resolve_cover(isbn=row["isbn"] or "", title=row["title"] or "",
                                 author=row["author"] or "", stored=row["cover_url"] or "")
    if url:
        return RedirectResponse(url, status_code=302)
    return Response(_BLANK_PX, media_type="image/png", status_code=404)


@app.get("/opds/download/{book_id}")
def opds_download(book_id: int, request: Request):
    _opds_guard(request)
    info = opds.book_file(book_id)
    if not info:
        raise HTTPException(status_code=404, detail="Boek niet gevonden.")
    path, title, author = info
    name = f"{author} - {title}".strip(" -") or path.stem
    fname = "".join(c for c in name if c not in '<>:"/\\|?*') + path.suffix
    return FileResponse(path, media_type="application/epub+zip", filename=fname)


# ---------- Metadata ----------

@app.get("/api/metadata/search")
def metadata_search(q: str, languages: str = "", limit: int = 12, deep: bool = False):
    langs = _lang_list(languages or db.get_setting("reading_languages") or "")
    try:
        return metadata.search(q, languages=langs, limit=max(1, min(limit, 80)), deep=deep)
    except metadata.MetadataUnavailable as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Metadata-zoeken mislukt: {e}") from e


# ---------- Books ----------

@app.get("/api/books")
def list_books():
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM books ORDER BY title COLLATE NOCASE, added_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/books")
def add_book(book: BookIn, force: bool = False):
    try:
        return services.create_book(book.model_dump(), allow_duplicate=force)
    except services.DuplicateBook as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/library/rescan")
def library_rescan():
    return services.rescan_library()


@app.get("/api/library/files")
def library_files():
    return services.list_library_files()


class LinkIn(BaseModel):
    path: str


@app.post("/api/books/{book_id}/link")
def link_book(book_id: int, payload: LinkIn):
    try:
        return services.link_book_to_file(book_id, payload.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ---------- Volgen (auteurs + reeksen) ----------

@app.get("/api/follows")
def list_follows():
    return db.list_follows()


@app.post("/api/follows")
def add_follow(payload: FollowIn):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Naam ontbreekt.")
    return {"id": db.add_follow(payload.type, payload.name, payload.author)}


@app.delete("/api/follows/{follow_id}")
def delete_follow(follow_id: int):
    db.delete_follow(follow_id)
    return {"ok": True}


@app.post("/api/follows/check")
def check_follows():
    return services.check_follows(trigger="handmatig")


# ---------- Suggesties ----------

@app.get("/api/suggestions")
def list_suggestions(state: str = "pending"):
    state = state if state in ("pending", "dismissed", "added") else "pending"
    return db.list_suggestions(state)


@app.post("/api/suggestions/{sug_id}/restore")
def restore_suggestion(sug_id: int):
    db.set_suggestion_state(sug_id, "pending")
    return {"ok": True}


@app.post("/api/suggestions/{sug_id}/accept")
def accept_suggestion(sug_id: int):
    try:
        return services.accept_suggestion(sug_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post("/api/suggestions/{sug_id}/dismiss")
def dismiss_suggestion(sug_id: int):
    db.set_suggestion_state(sug_id, "dismissed")
    return {"ok": True}


@app.post("/api/suggestions/{sug_id}/delete")
def delete_suggestion(sug_id: int):
    db.delete_suggestion(sug_id)
    return {"ok": True}


@app.post("/api/suggestions/bulk")
def suggestions_bulk(payload: SugBulkIn):
    n = 0
    for sid in payload.ids:
        if payload.action == "accept":
            try:
                services.accept_suggestion(sid)
                n += 1
            except Exception:  # noqa: BLE001
                pass
        elif payload.action == "dismiss":
            db.set_suggestion_state(sid, "dismissed")
            n += 1
        elif payload.action == "restore":
            db.set_suggestion_state(sid, "pending")
            n += 1
        elif payload.action == "delete":
            db.delete_suggestion(sid)
            n += 1
    return {"count": n}


@app.post("/api/books/delete")
def bulk_delete_books(payload: BulkDeleteIn):
    if not payload.ids:
        return {"deleted": 0, "removed_files": [], "errors": []}
    return services.delete_books(payload.ids, payload.delete_files)


@app.post("/api/books/{book_id}/monitored")
def set_monitored(book_id: int, payload: MonitoredIn):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE books SET monitored = ? WHERE id = ?",
            (1 if payload.monitored else 0, book_id),
        )
        conn.commit()
    return {"ok": True}


@app.post("/api/books/{book_id}/pref-language")
def set_pref_language(book_id: int, payload: PrefLangIn):
    val = payload.pref_language.lower().strip()
    val = val if val in ("en", "nl") else None
    with db.get_conn() as conn:
        conn.execute("UPDATE books SET pref_language = ? WHERE id = ?", (val, book_id))
        conn.commit()
    return {"ok": True, "pref_language": val}


@app.post("/api/books/{book_id}/grab-best")
def grab_best(book_id: int):
    book = _get_book_or_404(book_id)
    try:
        return services.grab_best(book)
    except services.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/books/{book_id}/reveal")
def reveal_book(book_id: int):
    book = _get_book_or_404(book_id)
    lp = book.get("library_path")
    if not lp:
        raise HTTPException(status_code=400, detail="Dit boek heeft geen bestand.")
    if not services.reveal_in_explorer(lp):
        raise HTTPException(status_code=400,
                            detail="Kon de bibliotheekmap niet openen op deze pc.")
    return {"ok": True}


@app.post("/api/books/{book_id}/search")
def book_search(book_id: int, all_formats: bool = Query(False, alias="all")):
    book = _get_book_or_404(book_id)
    try:
        results = services.find_all_candidates(book, epub_only=not all_formats)
    except services.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Zoeken mislukt: {e}") from e

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE books SET last_checked = CURRENT_TIMESTAMP WHERE id = ?", (book_id,)
        )
        conn.commit()
    return results


@app.post("/api/books/{book_id}/grab")
def grab_candidate(book_id: int, cand: dict):
    book = _get_book_or_404(book_id)
    if not cand.get("source") or not cand.get("kind"):
        raise HTTPException(status_code=400, detail="Ongeldige kandidaat.")
    try:
        services.grab_candidate(book, cand)
    except services.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Downloaden mislukt: {e}") from e
    return {"ok": True}


@app.post("/api/search-all")
def search_all():
    try:
        return services.auto_search_all(trigger="handmatig (alles)")
    except services.ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ---------- Helpers ----------

def _get_book_or_404(book_id: int):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Boek niet gevonden")
        return dict(row)
