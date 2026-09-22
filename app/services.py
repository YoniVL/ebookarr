"""Gedeelde logica voor de routes én de achtergrond-scheduler:
zoeken, releases doorsturen naar qBittorrent, downloads volgen en importeren.
"""
import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import db, metadata, sources
from .prowlarr import DEFAULT_BOOK_CATEGORIES, ProwlarrClient
from .qbittorrent import QBittorrentClient

log = logging.getLogger("ebookarr.services")

# Verberg het console-venster van Calibre-tools (de app draait zonder console)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_hidden(args, **kw):
    """subprocess.run zonder dat er een cmd/PowerShell-venster opflitst."""
    kw.setdefault("check", False)
    si = None
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # SW_HIDE
    return subprocess.run(args, startupinfo=si, creationflags=_NO_WINDOW, **kw)


# Voorkomt dat een handmatige actie en een scheduler-ronde elkaar overlappen
_search_lock = threading.Lock()
_poll_lock = threading.Lock()

_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Aannemelijke grenzen voor één ebook (of kleine bundel). Alles daarbuiten is
# vrijwel zeker geen los ebook (audioboek, videocursus, dump van 100 boeken).
MIN_EPUB_BYTES = 15 * 1024
MAX_EPUB_BYTES = 80 * 1024 * 1024

# Volgorde van voorkeur voor het importeren; epub eerst (geen conversie nodig).
EBOOK_EXTS_ORDER = (".epub", ".azw3", ".mobi", ".azw")

_CALIBRE_CANDIDATES = (
    r"C:\Program Files\Calibre2\ebook-convert.exe",
    r"C:\Program Files (x86)\Calibre2\ebook-convert.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\calibre\ebook-convert.exe"),
    os.path.expandvars(r"%PROGRAMFILES%\Calibre2\ebook-convert.exe"),
)


def _find_calibre_tool(name):
    """Pad naar een Calibre-CLI-tool (`ebook-convert`, `ebook-meta`), of None."""
    manual = db.get_setting("calibre_path")
    if manual:
        p = Path(manual)
        cand = p.with_name(f"{name}{p.suffix}") if p.suffix else p / f"{name}.exe"
        if cand.is_file():
            return str(cand)
    found = shutil.which(name)
    if found:
        return found
    for cand in _CALIBRE_CANDIDATES:
        c = Path(cand).with_name(f"{name}.exe") if cand else None
        if c and c.is_file():
            return str(c)
    return None


def find_ebook_convert():
    return _find_calibre_tool("ebook-convert")


def find_ebook_meta():
    return _find_calibre_tool("ebook-meta")


def kindle_enabled():
    """Kindle-formaten accepteren? Alleen als de instelling aan staat én er een
    converter is (anders zou je een niet-epub binnenhalen die je niet kunt lezen)."""
    return bool_setting("accept_kindle", False) and find_ebook_convert() is not None


class ConfigError(RuntimeError):
    """Prowlarr/qBittorrent is nog niet (volledig) ingesteld."""


class DuplicateBook(RuntimeError):
    """Er staat al een boek met deze titel/auteur/ISBN op de lijst."""


# ---------- Clients ----------

def get_prowlarr() -> ProwlarrClient:
    url = db.get_setting("prowlarr_url")
    key = db.get_setting("prowlarr_api_key")
    if not url or not key:
        raise ConfigError("Prowlarr is nog niet geconfigureerd (ga naar Instellingen).")
    return ProwlarrClient(url, key)


def get_qbittorrent() -> QBittorrentClient:
    url = db.get_setting("qbittorrent_url")
    user = db.get_setting("qbittorrent_username")
    pw = db.get_setting("qbittorrent_password")
    if not url or not user:
        raise ConfigError("qBittorrent is nog niet geconfigureerd (ga naar Instellingen).")
    return QBittorrentClient(url, user, pw)


# ---------- Instellingen-helpers ----------

def get_categories():
    raw = db.get_setting("prowlarr_categories") or ""
    cats = [int(x) for x in re.split(r"[,\s]+", raw) if x.strip().isdigit()]
    return cats or list(DEFAULT_BOOK_CATEGORIES)


def bool_setting(key, default=False):
    v = db.get_setting(key)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "ja")


def int_setting(key, default):
    try:
        return int(float(db.get_setting(key)))
    except (TypeError, ValueError):
        return default


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _tag(book_id):
    return f"ebookarr-{book_id}"


# ---------- Zoek-voortgang (in-memory; getoond in de UI) ----------

_progress_lock = threading.Lock()
_search_progress = {}  # book_id -> {"phase", "since", "trigger"}


def _progress_set(book_id, phase, trigger="zoeken"):
    with _progress_lock:
        cur = _search_progress.get(book_id) or {}
        _search_progress[book_id] = {
            "phase": phase,
            "since": cur.get("since") or _now_iso(),
            "phase_since": _now_iso(),
            "trigger": trigger,
        }


def _progress_clear(book_id):
    with _progress_lock:
        _search_progress.pop(book_id, None)


@contextlib.contextmanager
def _searching(book_id, trigger="zoeken"):
    _progress_set(book_id, "wachtrij", trigger)
    try:
        yield
    finally:
        _progress_clear(book_id)


def search_progress():
    """Lijst van boeken waar nu actief naar gezocht/gedownload wordt."""
    with _progress_lock:
        items = [{"book_id": bid, **info} for bid, info in _search_progress.items()]
    if not items:
        return []
    ids = [i["book_id"] for i in items]
    placeholders = ",".join("?" * len(ids))
    with db.get_conn() as conn:
        titles = {r["id"]: r["title"] for r in conn.execute(
            f"SELECT id, title FROM books WHERE id IN ({placeholders})", ids)}
    for i in items:
        i["title"] = titles.get(i["book_id"], f"boek {i['book_id']}")
    return items


# ---------- Zoeken ----------

# Woorden die publieke torrent-indexers vaak laten stranden ("0 resultaten")
_STOPWORDS = {"the", "a", "an", "and", "of", "to", "de", "het", "een", "en", "van"}


def _words(text: str):
    text = re.sub(r"[.'’`]", "", text or "")
    text = re.sub(r"[^\w\s-]", " ", text)
    return [w for w in text.split() if w]


def _norm(text: str) -> str:
    """Alleen kleine letters + cijfers — voor het losjes vergelijken van titels."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _title_words(title: str, n: int):
    words = _words(title)
    sig = [w for w in words if w.lower() not in _STOPWORDS] or words
    return " ".join(sig[:n])


# Sterke aanwijzingen dat een release in een bepaalde taal is
_LANG_MARKERS = {
    "es": ("mundodisco", "saga de la muerte", "castellano", "español", "espanol",
           "spanish", "traduccion"),
    "fr": ("français", "francais", "french", "traduit"),
    "de": ("deutsch", "german", "übersetzt"),
    "it": ("italiano", "italian"),
    "nl": ("nederlands", "dutch", "vertaald"),
    "en": ("english",),
}


def get_reading_languages():
    raw = db.get_setting("reading_languages") or "en,nl"
    langs = [x.strip().lower() for x in raw.split(",") if x.strip()]
    return langs or ["en", "nl"]


def preferred_language(book: dict) -> str:
    """Regel van de gebruiker: oorspronkelijk Engelstalig boek -> Engels,
    elke andere taal (ook Nederlands) -> Nederlands.
    Een handmatig gezette pref_language wint altijd."""
    p = (book.get("pref_language") or "").lower()
    if p in ("en", "nl"):
        return p
    return "en" if (book.get("language") or "").lower() == "en" else "nl"


def _language_rank(title: str, want_langs):
    """+1 als de release-titel een taal signaleert die de gebruiker leest,
    -1 als hij duidelijk een taal signaleert die de gebruiker NIET leest,
    anders 0. Alleen gebruikt om te sorteren."""
    want = set(want_langs or [])
    if not want:
        return 0
    low = (title or "").lower()
    for lang, markers in _LANG_MARKERS.items():
        if any(m in low for m in markers):
            return 1 if lang in want else -1
    return 0


def release_check(release: dict, book: dict):
    """(ok, reden). Simpele sanity-checks tegen verkeerde auto-downloads."""
    size = release.get("size") or 0
    if size and size < MIN_EPUB_BYTES:
        return False, f"verdacht klein ({size // 1024} KB)"
    if size and size > MAX_EPUB_BYTES:
        mb = size / (1024 * 1024)
        return False, f"veel te groot voor een epub ({mb:.0f} MB), vermoedelijk een bundel of audioboek"

    _, surname = _author_bits(book.get("author") or "")
    if surname and _norm(surname) not in _norm(release.get("title") or ""):
        return False, f"auteur '{surname}' komt niet voor in de release-titel"
    return True, ""


def _author_bits(author: str):
    """(volledige auteur zonder initialen, achternaam)."""
    words = [w for w in _words(author) if len(w) > 1]
    if not words:
        return "", ""
    return " ".join(words), words[-1]


def _build_queries(book: dict, epub_only: bool):
    """Zoekvarianten van (redelijk) specifiek naar breed.

    Publieke torrent-indexers doen strikte AND-matching: hoe meer woorden,
    hoe groter de kans op 0 resultaten omdat torrent-namen zelden de volledige
    ondertitel bevatten. Daarom korte queries met de kernwoorden.

    Als er een auteur bekend is, zit die in ELKE variant — we vallen nooit
    terug op alleen de titel (anders krijg je bij korte titels als "Mort"
    volledig verkeerde boeken).
    """
    title = (book.get("title") or "").strip()
    author_full, surname = _author_bits(book.get("author") or "")
    suffix = " epub" if epub_only else ""
    t4, t3, t2 = _title_words(title, 4), _title_words(title, 3), _title_words(title, 2)

    raw = []
    if surname:
        raw = [
            f"{t3} {surname}{suffix}",
            f"{t3} {surname}",           # geen 'epub' in query -> is_epub-filter
            f"{t2} {surname}{suffix}",
        ]
        if author_full != surname:
            raw.append(f"{t4} {author_full}{suffix}")
    else:
        raw = [f"{t4}{suffix}", f"{t3}{suffix}", t4]

    seen, out = set(), []
    for variant in raw:
        q = " ".join(variant.split())
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    # nooit meer dan 3 zoekopdrachten per boek (elke duurt ~30-60s)
    return out[:3]


def search_book(book: dict, epub_only: bool = True):
    client = get_prowlarr()
    cats = get_categories()
    # Voorkeurstaal: Engels als het boek oorspronkelijk Engelstalig is, anders
    # Nederlands (zie preferred_language). Handmatige keuze wint.
    want_langs = [preferred_language(book)]
    results = []
    last_error = None
    for query in _build_queries(book, epub_only):
        # Bevat de query al "epub"? Dan doet dat woord + de is_epub-filter het
        # narrow-werk, en zijn categorieën alleen maar hinderlijk (publieke
        # indexers categoriseren ebooks vaak niet). Zonder "epub" in de query
        # helpen de boekcategorieën juist tegen ruis (films e.d.).
        use_cats = None if "epub" in query.lower() else cats
        try:
            found = client.search(query, categories=use_cats)
        except requests.RequestException as e:
            # Eén trage/haperende indexer mag de hele zoekopdracht niet slopen;
            # probeer de volgende variant.
            log.warning("Boek %s: query %r mislukt (%s), volgende variant",
                        book.get("id"), query, e)
            last_error = e
            continue
        if epub_only:
            kindle_ok = kindle_enabled()
            found = [
                r for r in found
                if r.get("is_epub") or (kindle_ok and r.get("is_kindle"))
            ]
        for r in found:
            ok, reason = release_check(r, book)
            r["sane"] = ok
            r["sane_reason"] = reason
        if found:
            # epub eerst (geen conversie nodig), dan sane, dan taal, dan seeders
            found.sort(
                key=lambda r: (r.get("is_epub", False),
                               r.get("sane", True),
                               _language_rank(r.get("title"), want_langs),
                               r.get("seeders") or 0),
                reverse=True,
            )
            log.info("Boek %s: %d ebook-resultaten voor query %r",
                     book.get("id"), len(found), query)
            return found
        results = found
    if not results and last_error is not None:
        raise RuntimeError(
            f"Prowlarr reageerde niet op tijd ({last_error}). Vaak is één indexer "
            "traag/offline — probeer het zo nog eens."
        )
    return results


# ---------- Bronnen: zoeken over alle bronnen + pakken ----------

def _accepted_format(fmt):
    if fmt == "epub":
        return True
    return fmt in ("mobi", "azw3", "azw") and kindle_enabled()


def _candidate_ok(book, cand, pref, reading):
    if not _accepted_format(cand.get("format", "epub")):
        return False
    size = cand.get("size") or 0
    if size and (size < MIN_EPUB_BYTES or size > MAX_EPUB_BYTES):
        return False
    if cand["kind"] == "torrent":
        if not cand.get("sane", True):
            return False
        if (cand.get("seeders") or 0) < int_setting("min_seeders", 1):
            return False
        guid = (cand.get("download") or {}).get("guid")
        return not (guid and db.already_grabbed(book["id"], guid))
    # directe bron: taalcheck (onbekende taal blijft toegestaan)
    lang = cand.get("language")
    return not (lang and lang != pref and lang not in reading)


def _sane_size_score(size):
    """Een gewone roman-epub is ~0,15-4 MB. Geef die de voorkeur boven mini-fragmenten
    en boven opgeblazen (gescande / beeld-zware) bestanden."""
    if not size:
        return 1          # onbekend: neutraal
    mb = size / (1024 * 1024)
    if 0.12 <= mb <= 4:
        return 2
    if mb <= 8:
        return 1
    return 0              # > 8 MB of < 120 KB: verdacht


def _rank_candidates(cands, pref, book=None):
    want = _norm((book or {}).get("title", ""))

    def key(cand):
        got = _norm(cand.get("title", ""))
        exact_title = bool(want) and (got == want)
        return (
            cand.get("format") == "epub",
            cand.get("language") == pref,
            exact_title,
            _sane_size_score(cand.get("size")),
            (cand.get("seeders") or 0) if cand["kind"] == "torrent" else 0,
            min(cand.get("size") or 0, 4 * 1024 * 1024),  # binnen 'sane' groter = vollediger
        )
    return sorted(cands, key=key, reverse=True)


def find_from_sources(book: dict):
    """Doorloop de ingeschakelde bronnen op volgorde; geef (bronnaam, bruikbare
    kandidaten, opmerking) van de eerste bron met resultaten (torrents laatst).

    Als een directe bron (geen torrent) een fout gaf én de enige treffers van
    torrents zouden komen, pakken we niets: `opmerking` legt uit waarom, en het
    boek blijft 'wanted' zodat de volgende zoekronde het opnieuw probeert."""
    pref = preferred_language(book)
    reading = get_reading_languages()
    trigger = (_search_progress.get(book["id"]) or {}).get("trigger", "zoeken")
    mods = sources.enabled_in_order()
    direct = [m for m in mods if not getattr(m, "IS_TORRENT", False)]
    torrents = [m for m in mods if getattr(m, "IS_TORRENT", False)]

    def _usable(mod):
        cands = mod.search(book) or []
        return _rank_candidates(
            [x for x in cands if _candidate_ok(book, x, pref, reading)], pref, book)

    # --- directe bronnen: parallel doorzoeken, daarna op prioriteit kiezen ---
    direct_errors, hits = [], {}
    if direct:
        _progress_set(book["id"],
                      "zoekt bij " + ", ".join(sources.label(m.NAME) for m in direct),
                      trigger)
        with ThreadPoolExecutor(max_workers=len(direct)) as pool:
            futs = {pool.submit(_usable, m): m for m in direct}
            for fut in as_completed(futs):
                mod = futs[fut]
                try:
                    usable = fut.result()
                except ConfigError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("Bron %s: zoeken mislukt: %s", mod.NAME, e)
                    direct_errors.append(sources.label(mod.NAME))
                    continue
                if usable:
                    hits[mod.NAME] = usable
    for mod in direct:  # prioriteitsvolgorde uit REGISTRY
        if mod.NAME not in hits:
            continue
        if mod.NAME == "zlibrary":
            left = sources.zlibrary.quota_left()
            if left == 0:
                note = ("Z-Library-daglimiet bereikt — dit boek staat op je Z-Library "
                        "en wordt automatisch opgehaald zodra de limiet reset.")
                log.info("Boek %s: %s", book["id"], note)
                return None, [], note
        log.info("Boek %s: %d bruikbaar via bron '%s'",
                 book["id"], len(hits[mod.NAME]), mod.NAME)
        return mod.NAME, hits[mod.NAME], None

    # --- geen directe treffer: torrents (laatste redmiddel) ---
    for mod in torrents:
        _progress_set(book["id"], f"zoekt bij {sources.label(mod.NAME)}", trigger)
        try:
            usable = _usable(mod)
        except ConfigError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Bron %s: zoeken mislukt: %s", mod.NAME, e)
            continue
        if not usable:
            continue
        if direct_errors:
            note = (f"{', '.join(direct_errors)} gaf een fout; alleen torrents "
                    "gevonden - die pak ik niet automatisch. Nieuwe poging bij "
                    "de volgende zoekronde, of gebruik 'Kies zelf'.")
            log.warning("Boek %s: torrents overgeslagen (%s)", book["id"], note)
            return None, [], note
        log.info("Boek %s: %d bruikbaar via bron '%s'",
                 book["id"], len(usable), mod.NAME)
        return mod.NAME, usable, None
    return None, [], None


def find_all_candidates(book: dict, epub_only: bool = True):
    """Kandidaten van ÁLLE ingeschakelde bronnen (voor het handmatige 'Kies zelf'-
    scherm). Gesorteerd: bruikbare eerst, dan op bronvolgorde."""
    pref = preferred_language(book)
    reading = get_reading_languages()
    priority = {m.NAME: i for i, m in enumerate(sources.REGISTRY)}
    mods = sources.enabled_in_order()
    out = []

    def _run(mod):
        return mod.NAME, (mod.search(book) or [])

    pool = ThreadPoolExecutor(max_workers=max(1, len(mods)))
    futs = {pool.submit(_run, m): m for m in mods}
    try:
        done = list(as_completed(futs, timeout=50))
    except TimeoutError:
        done = [f for f in futs if f.done()]
    for fut in done:
        name = futs[fut].NAME
        try:
            _, cands = fut.result()
        except Exception as e:  # noqa: BLE001
            log.warning("Bron %s: zoeken mislukt: %s", name, e)
            continue
        for cand in cands:
            if epub_only and not _accepted_format(cand.get("format", "epub")):
                continue
            cand["usable"] = _candidate_ok(book, cand, pref, reading)
            out.append(cand)
    for fut, m in futs.items():
        if not fut.done():
            log.warning("Bron %s: te traag, overgeslagen in 'Kies zelf'", m.NAME)
    # niet blokkeren op trage bronnen; ze lopen af dankzij hun eigen timeouts
    pool.shutdown(wait=False, cancel_futures=True)
    out.sort(key=lambda c: (
        not c.get("usable", False),
        priority.get(c["source"], 99),
        -(c.get("seeders") or 0),
    ))
    return out


def grab_candidate(book: dict, cand: dict):
    """Pak één kandidaat: torrent -> qBittorrent; directe bron -> download + import."""
    if cand["kind"] == "torrent":
        grab_release(book, cand["download"])
        with db.get_conn() as conn:
            conn.execute("UPDATE books SET source=? WHERE id=?",
                         (cand["source"], book["id"]))
            conn.commit()
        return
    _download_direct(book, cand)


def _download_direct(book: dict, cand: dict):
    mod = sources.get(cand["source"])
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE books SET status='downloading', dl_state=?, progress=0, "
            "grabbed_release=?, grabbed_at=CURRENT_TIMESTAMP, source=?, "
            "last_error=NULL WHERE id=?",
            (f"{cand['source']}-download", cand.get("title"),
             cand["source"], book["id"]),
        )
        conn.commit()
    db.add_history(book["id"], "grabbed", cand.get("title"),
                   detail=f"bron={cand['source']}")

    tmp = Path(tempfile.mkdtemp(prefix="ebookarr-"))
    try:
        src_path = mod.download(cand, tmp)
        dest, error = _place_in_library(book, src_path, src_path.suffix)
    except Exception as e:  # noqa: BLE001
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE books SET status='wanted', dl_state=NULL, progress=0, "
                "last_error=? WHERE id=?",
                (f"{cand['source']}: {e}", book["id"]),
            )
            conn.commit()
        db.add_history(book["id"], "failed", cand.get("title"),
                       detail=f"bron {cand['source']}: {e}")
        log.warning("Boek %s: directe download via %s mislukt: %s",
                    book["id"], cand["source"], e)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    _finish_import(book, str(dest) if dest else None, error)
    db.add_history(book["id"], "import", cand.get("title"),
                   detail=(error or f"gedownload via {cand['source']} -> {dest}"))
    log.info("Boek %s: geïmporteerd via %s -> %s", book["id"], cand["source"], dest)


def grab_best(book: dict):
    """Zoek nu bij alle bronnen en pak meteen de beste kandidaat."""
    _set_last_checked(book["id"])
    with _searching(book["id"], "nu zoeken"):
        src, usable, note = find_from_sources(book)
        if not usable:
            _set_error(book["id"], note)
            return {"grabbed": False,
                    "reason": note or "niets bruikbaars gevonden bij de ingeschakelde bronnen"}
        _progress_set(book["id"], f"downloaden via {sources.label(src)}", "nu zoeken")
        grab_candidate(book, usable[0])
    return {"grabbed": True, "source": src, "release": usable[0].get("title")}


def _set_last_checked(book_id):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE books SET last_checked = CURRENT_TIMESTAMP WHERE id = ?",
            (book_id,),
        )
        conn.commit()


# ---------- Release doorsturen ----------

def grab_release(book: dict, release: dict):
    guid = release.get("guid")
    if guid and db.already_grabbed(book["id"], guid):
        raise RuntimeError("Deze release is al eerder gepakt.")
    if not release.get("download_url"):
        raise RuntimeError("Deze release heeft geen bruikbare download-link.")

    qbt = get_qbittorrent()
    tag = _tag(book["id"])
    try:
        before = {t.get("hash") for t in qbt.torrents_info()}
    except Exception:
        before = set()
    qbt.add_torrent(release["download_url"], tag=tag)
    torrent_hash = _resolve_hash(qbt, tag, before)

    with db.get_conn() as conn:
        conn.execute(
            """UPDATE books SET status='downloading', grabbed_release=?, grabbed_guid=?,
                   grabbed_at=CURRENT_TIMESTAMP, download_hash=?, progress=0,
                   dl_state='queued', last_error=NULL
               WHERE id=?""",
            (release.get("title"), guid, torrent_hash, book["id"]),
        )
        conn.commit()
    db.add_history(
        book["id"], "grabbed", release.get("title"), guid,
        f"indexer={release.get('indexer')} seeders={release.get('seeders')}",
    )
    log.info("Boek %s: release gepakt %r (hash=%s)", book["id"], release.get("title"), torrent_hash)
    return torrent_hash


def _resolve_hash(qbt: QBittorrentClient, tag: str, before: set | None = None):
    """qBittorrent geeft bij 'add' geen hash terug. We zoeken de torrent die er
    NA de add bij is gekomen (diff met `before`); pas als dat niks oplevert
    vallen we terug op de nieuwste met onze tag (kan mis gaan als er nog een
    oude torrent met dezelfde tag in qBittorrent staat)."""
    before = before or set()
    for _ in range(16):
        try:
            all_infos = qbt.torrents_info()
        except Exception:
            all_infos = []
        new_hashes = [t for t in all_infos if t.get("hash") not in before]
        if new_hashes:
            new_hashes.sort(key=lambda t: t.get("added_on", 0), reverse=True)
            return new_hashes[0].get("hash")
        time.sleep(0.5)

    try:
        tagged = qbt.torrents_info(tag=tag)
        tagged.sort(key=lambda t: t.get("added_on", 0), reverse=True)
        return tagged[0].get("hash") if tagged else None
    except Exception:
        return None


# ---------- Auto-zoekronde ----------

def auto_search_all(trigger="handmatig"):
    if not _search_lock.acquire(blocking=False):
        log.info("Zoekronde overgeslagen: er loopt er al een.")
        return {"skipped": True}
    try:
        auto = bool_setting("auto_download", True)
        with db.get_conn() as conn:
            books = [dict(r) for r in conn.execute(
                "SELECT * FROM books WHERE status IN ('wanted','failed') "
                "AND COALESCE(monitored, 1) = 1"
            )]

        summary = {"searched": 0, "grabbed": 0, "errors": 0, "candidates": 0}
        for book in books:
            try:
                with _searching(book["id"], "automatisch"):
                    src, usable, note = find_from_sources(book)
                    summary["searched"] += 1
                    _set_last_checked(book["id"])
                    summary["candidates"] += len(usable)
                    db.add_history(
                        book["id"], "search",
                        detail=(f"{len(usable)} bruikbaar via {src}" if usable
                                else (note or "niets gevonden bij de bronnen")),
                    )
                    if usable and auto:
                        _progress_set(book["id"],
                                      f"downloaden via {sources.label(src)}", "automatisch")
                        grab_candidate(book, usable[0])
                        summary["grabbed"] += 1
                    elif not usable:
                        _set_error(book["id"], note)
            except ConfigError:
                raise
            except Exception as e:  # noqa: BLE001
                summary["errors"] += 1
                log.warning("Zoeken voor boek %s mislukt: %s", book["id"], e)
                _set_error(book["id"], str(e))

        db.set_setting("last_search_run", _now_iso())
        log.info("Auto-zoekronde (%s): %s", trigger, summary)
        return summary
    finally:
        _search_lock.release()


def _set_error(book_id, message):
    with db.get_conn() as conn:
        conn.execute("UPDATE books SET last_error=? WHERE id=?", (message, book_id))
        conn.commit()


# ---------- Downloads volgen + importeren ----------

_DONE_STATES = {"uploading", "stalledUP", "forcedUP", "pausedUP", "queuedUP", "checkingUP"}


def poll_downloads(trigger="scheduler"):
    if not _poll_lock.acquire(blocking=False):
        return
    try:
        with db.get_conn() as conn:
            books = [dict(r) for r in conn.execute(
                "SELECT * FROM books WHERE status='downloading'"
            )]
        if not books:
            return

        qbt = get_qbittorrent()
        stalled_hours = int_setting("stalled_hours", 24)

        for book in books:
            try:
                torrent_hash = book.get("download_hash")
                if not torrent_hash:
                    # Hash ontbrak bij het grabben (qBittorrent was traag):
                    # alsnog ophalen via onze tag.
                    tagged = qbt.torrents_info(tag=_tag(book["id"]))
                    tagged.sort(key=lambda x: x.get("added_on", 0), reverse=True)
                    torrent_hash = tagged[0].get("hash") if tagged else None
                    if not torrent_hash:
                        continue
                    with db.get_conn() as conn:
                        conn.execute(
                            "UPDATE books SET download_hash=? WHERE id=?",
                            (torrent_hash, book["id"]),
                        )
                        conn.commit()
                    book["download_hash"] = torrent_hash
                infos = qbt.torrents_info(hashes=torrent_hash)
                if not infos:
                    continue
                t = infos[0]
                progress = float(t.get("progress") or 0.0)
                state = t.get("state") or ""
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE books SET progress=?, dl_state=? WHERE id=?",
                        (progress, state, book["id"]),
                    )
                    conn.commit()

                if progress >= 0.999 or state in _DONE_STATES:
                    _import_book(book, t, qbt)
                elif _is_stalled(book, t, stalled_hours):
                    _mark_failed(book)
            except ConfigError:
                raise
            except Exception as e:
                log.warning("Download volgen voor boek %s mislukt: %s", book["id"], e)
    finally:
        _poll_lock.release()


def _is_stalled(book, t, stalled_hours):
    if any((t.get(k) or 0) > 0 for k in ("num_seeds", "num_complete", "progress")):
        return False
    grabbed_at = book.get("grabbed_at")
    if not grabbed_at:
        return False
    try:
        dt = datetime.fromisoformat(str(grabbed_at).replace("Z", ""))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt > timedelta(hours=stalled_hours)


def _mark_failed(book):
    db.add_history(
        book["id"], "failed", book.get("grabbed_release"), book.get("grabbed_guid"),
        "download vastgelopen (0 seeders)",
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE books SET status='failed', dl_state='stalled', last_error=? WHERE id=?",
            ("Vorige download liep vast (0 seeders); volgende zoekronde "
             "probeert een andere release.",
             book["id"]),
        )
        conn.commit()
    log.info("Boek %s: download vastgelopen, gemarkeerd als failed", book["id"])


def _sanitize(s):
    s = _BAD_FILENAME.sub("", s or "").strip().rstrip(". ")
    return s or "boek"


def _filename_for(book, suffix=".epub"):
    author = _sanitize(book.get("author") or "")
    title = _sanitize(book.get("title") or "boek")
    base = f"{author} - {title}" if author else title
    return base[:180] + (suffix or ".epub")


def _resolve_file_path(t, rel_name):
    save_path = t.get("save_path") or ""
    cand = Path(save_path) / rel_name
    if cand.exists():
        return cand
    content_path = Path(t.get("content_path") or "")
    if content_path.is_file():
        return content_path
    if content_path.is_dir():
        alt = content_path / Path(rel_name).name
        if alt.exists():
            return alt
    return cand


def _pick_ebook_file(files):
    """Kies het beste ebook-bestand uit een torrent (epub > azw3 > mobi > azw,
    daarbinnen het grootste)."""
    best = None
    for f in files:
        ext = Path(str(f.get("name", ""))).suffix.lower()
        if ext not in EBOOK_EXTS_ORDER:
            continue
        rank = (EBOOK_EXTS_ORDER.index(ext), -(f.get("size") or 0))
        if best is None or rank < best[0]:
            best = (rank, f, ext)
    return (best[1], best[2]) if best else (None, None)


def _convert_to_epub(src: Path, dest: Path):
    """Zet src (mobi/azw3/...) om naar epub met Calibre. Geeft (ok, melding)."""
    tool = find_ebook_convert()
    if not tool:
        return False, "geen Calibre gevonden om te converteren"
    try:
        proc = _run_hidden(
            [tool, str(src), str(dest)],
            capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"conversie mislukt: {e}"
    if proc.returncode != 0 or not dest.exists():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
        return False, f"conversie mislukt: {tail[0]}"
    return True, ""


def _place_in_library(book, src: Path, ext: str):
    """Kopieer (of converteer) `src` naar de bibliotheekmap als epub.
    Geeft (dest_path | None, foutmelding | None)."""
    ext = (ext or src.suffix).lstrip(".").lower() or "epub"
    library = db.get_setting("library_folder")
    if not library:
        return None, "Geen bibliotheekmap ingesteld; bestand niet geïmporteerd."
    if not src.exists():
        return None, f"Bestand niet gevonden op schijf: {src}"

    dest_dir = Path(library)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _filename_for(book, ".epub")
    if dest.exists():
        dest = dest.with_stem(f"{dest.stem} ({book['id']})")

    if ext == "epub":
        shutil.copy2(src, dest)
        _embed_metadata(book, dest)
        return dest, None

    ok, msg = _convert_to_epub(src, dest)
    if ok:
        log.info("Boek %s: %s -> epub geconverteerd", book["id"], ext)
        _embed_metadata(book, dest)
        return dest, None
    # conversie lukte niet: origineel bestand toch bewaren
    dest = dest.with_suffix(f".{ext}")
    shutil.copy2(src, dest)
    log.warning("Boek %s: conversie mislukt (%s)", book["id"], msg)
    return dest, f"Kon niet naar epub converteren ({msg}); origineel {ext}-bestand bewaard."


_LANG3 = {"nl": "nld", "en": "eng", "de": "deu", "fr": "fra", "es": "spa", "it": "ita"}


def _embed_metadata(book, epub_path: Path):
    """Schrijf titel/auteur/taal/cover ín het epub met Calibre's ebook-meta,
    zodat het boek er in elke leesapp correct uitziet. Best effort."""
    if not bool_setting("embed_metadata", True):
        return
    tool = find_ebook_meta()
    if not tool:
        return
    args = [tool, str(epub_path)]
    if book.get("title"):
        args += ["--title", book["title"]]
    if book.get("author"):
        args += ["--authors", re.sub(r"\s*,\s*", " & ", book["author"])]
    lang = book.get("language")
    if lang:
        args += ["--language", _LANG3.get(lang, lang)]

    cover_tmp = None
    try:
        url = resolve_cover(isbn=book.get("isbn") or "", title=book.get("title") or "",
                            author=book.get("author") or "", stored=book.get("cover_url") or "")
        if url:
            r = requests.get(url, timeout=10)
            if r.ok and r.headers.get("content-type", "").startswith("image/"):
                cover_tmp = epub_path.with_suffix(".cover.jpg")
                cover_tmp.write_bytes(r.content)
                args += ["--cover", str(cover_tmp)]
    except requests.RequestException:
        pass

    try:
        _run_hidden(args, capture_output=True, timeout=120, check=False)
        log.info("Boek %s: metadata in epub geschreven", book["id"])
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("Boek %s: metadata schrijven mislukt: %s", book["id"], e)
    finally:
        if cover_tmp:
            cover_tmp.unlink(missing_ok=True)


def _import_book(book, t, qbt):
    files = qbt.torrent_files(book["download_hash"])
    chosen, ext = _pick_ebook_file(files)
    if not chosen:
        _finish_import(book, None, "Download klaar, maar geen ebook-bestand in de torrent gevonden.")
        db.add_history(book["id"], "import", detail="geen ebook in torrent")
        return

    src = _resolve_file_path(t, chosen["name"])
    dest, error = _place_in_library(book, src, ext)
    _finish_import(book, str(dest) if dest else None, error)
    db.add_history(book["id"], "import", book.get("grabbed_release"),
                   detail=(error or f"geïmporteerd -> {dest}"))


def _finish_import(book, library_path, error):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE books SET status='downloaded', progress=1.0, library_path=?, "
            "last_error=? WHERE id=?",
            (library_path, error, book["id"]),
        )
        conn.commit()


# ---------- Boek toevoegen (met duplicaat- en bibliotheek-check) ----------

_EBOOK_EXTS = (".epub", ".mobi", ".azw3", ".azw", ".pdf")


def _iter_library_files():
    folder = db.get_setting("library_folder")
    if not folder:
        return
    root = Path(folder)
    if not root.is_dir():
        return
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in _EBOOK_EXTS:
            yield p


_AUTHOR_STOP = {"the", "and", "van", "der", "den", "von", "de", "of", "jr", "sr"}


def _author_words(author: str):
    """Losse betekenisvolle woorden uit een auteur-/bijdrager-veld. Het veld kan
    meerdere namen bevatten (auteur + illustrator + vertaler), dus we matchen
    losjes: één woord dat terugkomt volstaat."""
    return [w for w in re.findall(r"[a-z0-9]{3,}", (author or "").lower())
            if w not in _AUTHOR_STOP]


def find_in_library(title: str, author: str = ""):
    """Bestaand bestand in de bibliotheekmap dat bij dit boek hoort, of None."""
    nt = _norm(title)
    if len(nt) < 3:
        return None
    awords = _author_words(author)
    for p in _iter_library_files():
        stem = _norm(p.stem)
        if nt not in stem:
            continue
        if not awords or any(w in stem for w in awords):
            return p
    return None


def _find_duplicate(title, author, isbn):
    ni, nt, na = _norm(isbn), _norm(title), _norm(author)
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, author, isbn, status, library_path FROM books")]
    for r in rows:
        if ni and _norm(r.get("isbn")) == ni:
            return r
        if nt and _norm(r.get("title")) == nt:
            rna = _norm(r.get("author"))
            if not na or not rna or na == rna:
                return r
    return None


def create_book(data: dict, allow_duplicate: bool = False):
    title = (data.get("title") or "").strip()
    author = (data.get("author") or "").strip()
    if not title:
        raise ValueError("Titel ontbreekt.")

    if not allow_duplicate:
        dup = _find_duplicate(title, author, data.get("isbn") or "")
        if dup:
            who = f" ({dup['author']})" if dup.get("author") else ""
            raise DuplicateBook(f"'{dup['title']}'{who} staat al op je lijst.")

    existing = find_in_library(title, author)
    status = "downloaded" if existing else "wanted"
    pref_lang = preferred_language({"language": data.get("language")})

    with db.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO books
                   (title, author, language, isbn, cover_url, description, year,
                    source_id, format_preference, status, library_path, progress,
                    pref_language)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, author, data.get("language"), data.get("isbn"),
             data.get("cover_url"), data.get("description"), data.get("year"),
             data.get("source_id"), data.get("format_preference") or "epub",
             status, str(existing) if existing else None,
             1.0 if existing else 0, pref_lang),
        )
        conn.commit()
        book_id = cur.lastrowid

    note = None
    if existing:
        db.add_history(book_id, "import", detail=f"al aanwezig in bibliotheek: {existing}")
        note = f"Al in je bibliotheek gevonden ({existing.name}) — op 'Binnengehaald' gezet."
        log.info("Boek %s toegevoegd, al aanwezig: %s", book_id, existing)
    return {"id": book_id, "status": status, "note": note}


_ISBN_IN_TEXT_RE = re.compile(r"(\d{13}|\d{9}[\dXx])")


def _parse_epub_meta(path: Path):
    """Titel/auteur/taal/jaar/isbn uit de opf-metadata van een epub lezen —
    gewoon zip + Dublin Core-XML, geen Calibre nodig. Geeft {} als het niet lukt
    (versleuteld/beschadigd bestand, geen geldig epub, ...)."""
    try:
        with zipfile.ZipFile(path) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            rootfile = container.find(
                ".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
            opf_path = rootfile.get("full-path") if rootfile is not None else None
            if not opf_path:
                return {}
            opf = ET.fromstring(z.read(opf_path))
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile):
        return {}

    ns = {"dc": "http://purl.org/dc/elements/1.1/"}

    def text(tag):
        el = opf.find(f".//dc:{tag}", ns)
        return (el.text or "").strip() if el is not None and el.text else ""

    creators = [c.text.strip() for c in opf.findall(".//dc:creator", ns)
                if c.text and c.text.strip()]
    year = None
    m = re.match(r"(\d{4})", text("date"))
    if m:
        year = int(m.group(1))
    isbn = ""
    for ident in opf.findall(".//dc:identifier", ns):
        m2 = _ISBN_IN_TEXT_RE.search(re.sub(r"[\s-]", "", ident.text or ""))
        if m2:
            isbn = m2.group(1)
            break
    lang_raw = text("language")
    return {
        "title": text("title"),
        "author": ", ".join(creators),
        "language": metadata._norm_lang(lang_raw) if lang_raw else None,
        "year": year,
        "isbn": isbn,
        "description": text("description")[:2000],
    }


def _title_author_from_filename(path: Path):
    """Val terug op de bestandsnaam als er geen (bruikbare) metadata te lezen
    is: onze eigen naamgeving is 'Auteur - Titel.ext' (zie `_filename_for`)."""
    stem = path.stem
    if " - " in stem:
        author, title = stem.split(" - ", 1)
        return title.strip() or stem, author.strip()
    return stem, ""


def _resolved_library_paths(conn):
    paths = set()
    for r in conn.execute("SELECT library_path FROM books WHERE library_path IS NOT NULL"):
        try:
            paths.add(str(Path(r["library_path"]).resolve()).lower())
        except OSError:
            paths.add(str(r["library_path"]).lower())
    return paths


def _import_orphan_files(known_paths):
    """Bestanden in de bibliotheekmap die aan geen enkel boek hangen (bv. zelf
    in de map gezet): metadata lezen en als nieuw boek toevoegen, of koppelen
    aan een bestaand boek waarvan de titel exact overeenkomt maar dat nog geen
    bestand had."""
    added, linked = [], []
    for p in _iter_library_files():
        try:
            key = str(p.resolve()).lower()
        except OSError:
            key = str(p).lower()
        if key in known_paths:
            continue

        meta = _parse_epub_meta(p) if p.suffix.lower() == ".epub" else {}
        title = (meta.get("title") or "").strip()
        author = (meta.get("author") or "").strip()
        if not title:
            title, fallback_author = _title_author_from_filename(p)
            author = author or fallback_author
        title = title.strip()
        if not title:
            known_paths.add(key)
            continue

        dup = _find_duplicate(title, author, meta.get("isbn") or "")
        if dup and not dup.get("library_path"):
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE books SET status='downloaded', progress=1.0, library_path=?, "
                    "last_error=NULL WHERE id=?", (str(p), dup["id"]))
                conn.commit()
            db.add_history(dup["id"], "import", detail=f"gevonden bij herscan: {p.name}")
            linked.append(dup["title"])
            known_paths.add(key)
            continue
        if dup:
            known_paths.add(key)
            continue  # al aan een ander bestand gekoppeld -> niet nog eens toevoegen

        pref_lang = preferred_language({"language": meta.get("language")})
        with db.get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO books
                       (title, author, language, isbn, year, description, status,
                        library_path, progress, pref_language, source)
                   VALUES (?, ?, ?, ?, ?, ?, 'downloaded', ?, 1.0, ?, 'handmatig')""",
                (title, author, meta.get("language"), meta.get("isbn") or None,
                 meta.get("year"), meta.get("description") or None, str(p), pref_lang),
            )
            conn.commit()
            book_id = cur.lastrowid
        db.add_history(book_id, "import", detail=f"gevonden bij herscan: {p.name}")
        added.append(title)
        known_paths.add(key)
    return added, linked


def rescan_library():
    """Herscan de bibliotheekmap:
    - 'wanted' boeken -> 'downloaded' als het bestand er (al) staat;
    - 'downloaded' boeken -> 'wanted' als hun bestand verdwenen is;
    - bestanden die aan geen enkel boek hangen (bv. zelf in de map gezet) ->
      als nieuw boek toevoegen, met metadata uit het epub zelf."""
    with db.get_conn() as conn:
        books = [dict(r) for r in conn.execute(
            "SELECT id, title, author, status, library_path FROM books")]

    found, lost = [], []
    for b in books:
        if b["status"] == "wanted":
            f = find_in_library(b["title"], b["author"])
            if not f:
                continue
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE books SET status='downloaded', progress=1.0, library_path=?, "
                    "last_error=NULL WHERE id=?", (str(f), b["id"]))
                conn.commit()
            db.add_history(b["id"], "import", detail=f"al aanwezig in bibliotheek: {f}")
            found.append(b["title"])

        elif b["status"] == "downloaded":
            lp = b.get("library_path")
            if lp and Path(lp).exists():
                continue
            # bestand weg? kijk of het onder een andere naam terug te vinden is
            alt = find_in_library(b["title"], b["author"])
            with db.get_conn() as conn:
                if alt:
                    conn.execute("UPDATE books SET library_path=? WHERE id=?",
                                 (str(alt), b["id"]))
                else:
                    conn.execute(
                        "UPDATE books SET status='wanted', progress=0, library_path=NULL, "
                        "source=NULL, last_error='Bestand niet meer in de bibliotheekmap.' "
                        "WHERE id=?", (b["id"],))
                conn.commit()
            if not alt:
                db.add_history(b["id"], "failed", detail="bestand verdwenen uit bibliotheekmap")
                lost.append(b["title"])

    with db.get_conn() as conn:
        known_paths = _resolved_library_paths(conn)
    added, relinked = _import_orphan_files(known_paths)

    log.info(
        "Bibliotheek-herscan: %d gevonden, %d weer op 'gewenst', %d nieuw, %d gekoppeld",
        len(found), len(lost), len(added), len(relinked))
    return {"updated": found, "lost": lost, "added": added, "relinked": relinked}


# ---------- Handmatig een bestand aan een boek koppelen ----------

def list_library_files():
    """Alle ebook-bestanden in de bibliotheekmap, met welk boek eraan hangt."""
    folder = db.get_setting("library_folder")
    if not folder or not Path(folder).is_dir():
        return {"folder": folder or "", "files": []}
    with db.get_conn() as conn:
        by_path = {}
        for r in conn.execute(
                "SELECT id, title, library_path FROM books WHERE library_path IS NOT NULL"):
            try:
                by_path[str(Path(r["library_path"]).resolve()).lower()] = r["title"]
            except OSError:
                pass
    out = []
    for p in sorted(_iter_library_files(), key=lambda x: x.name.lower()):
        try:
            size = p.stat().st_size
        except OSError:
            size = None
        out.append({
            "name": p.name,
            "path": str(p),
            "size": size,
            "linked_to": by_path.get(str(p.resolve()).lower()),
        })
    return {"folder": folder, "files": out}


def link_book_to_file(book_id: int, path: str):
    """Koppel handmatig een bestand aan een boek en zet het op 'downloaded'."""
    folder = db.get_setting("library_folder")
    p = Path(path)
    if not folder:
        raise ValueError("Er is nog geen bibliotheekmap ingesteld.")
    try:
        p.resolve().relative_to(Path(folder).resolve())
    except (ValueError, OSError) as e:
        raise ValueError("Het bestand moet in de bibliotheekmap staan.") from e
    if not p.is_file():
        raise ValueError("Bestand niet gevonden.")

    freed = []
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if not row:
            raise ValueError("Boek niet gevonden.")
        book = dict(row)
        # een bestand hoort bij één boek: eventuele andere koppeling losmaken
        for other in conn.execute(
                "SELECT id, title FROM books WHERE id != ? AND library_path = ?",
                (book_id, str(p))):
            conn.execute(
                "UPDATE books SET status='wanted', progress=0, library_path=NULL, "
                "last_error='Bestand handmatig aan een ander boek gekoppeld.' WHERE id=?",
                (other["id"],))
            freed.append(other["title"])
        conn.execute(
            "UPDATE books SET status='downloaded', progress=1.0, library_path=?, "
            "source=COALESCE(source,'handmatig'), last_error=NULL WHERE id=?",
            (str(p), book_id),
        )
        conn.commit()
    for t in freed:
        log.info("Bestand overgezet: '%s' verloor z'n koppeling", t)
    db.add_history(book_id, "import", detail=f"handmatig gekoppeld aan {p.name}")
    log.info("Boek %s handmatig gekoppeld aan %s", book_id, p)
    try:
        _embed_metadata({**book, "library_path": str(p)}, p)
    except Exception as e:  # noqa: BLE001
        log.warning("Boek %s: metadata schrijven na koppelen mislukt: %s", book_id, e)
    return {"id": book_id, "library_path": str(p), "freed": freed}


# ---------- Volgen (auteurs + reeksen) -> suggesties ----------

def _already_have(title, author, isbn):
    """Staat dit boek al als boek op de lijst, of is het al eens gesuggereerd?"""
    if _find_duplicate(title, author, isbn):
        return True
    return db.suggestion_exists(title, author, isbn)


def check_follows(trigger="scheduler"):
    from . import metadata

    follows = db.list_follows()
    if not follows:
        return {"checked": 0, "suggested": 0}
    langs = get_reading_languages()
    suggested_total = 0

    for f in follows:
        name, ftype = f["name"], f["type"]
        f_author = (f.get("author") or "").strip()
        label = f"{'reeks' if ftype == 'series' else 'auteur'}: {name}"
        try:
            if ftype == "series":
                results = metadata.by_series(name, f_author, languages=langs, limit=150)
            else:
                results = metadata.by_author(name, languages=langs, limit=300)
        except metadata.MetadataUnavailable:
            db.touch_follow(f["id"], "metadata-bron onbereikbaar")
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("%s: zoeken mislukt: %s", label, e)
            db.touch_follow(f["id"], f"fout: {e}")
            continue

        # nieuwste boeken eerst; veiligheidsplafond tegen extreme gevallen
        results = sorted(results, key=lambda r: r.get("year") or 0, reverse=True)[:200]

        new_here = 0
        for r in results:
            if _already_have(r["title"], r.get("author") or f_author or name, r.get("isbn") or ""):
                continue
            db.add_suggestion({
                "title": r["title"],
                "author": r.get("author") or f_author or name,
                "language": r.get("language"),
                "isbn": r.get("isbn"),
                "cover_url": r.get("cover_url"),
                "description": r.get("description"),
                "year": r.get("year"),
                "source_id": r.get("source_id"),
            }, found_via=label)
            new_here += 1
            suggested_total += 1

        db.touch_follow(
            f["id"],
            f"{new_here} nieuwe suggestie(s)" if new_here else "niets nieuws gevonden",
        )
        if new_here:
            log.info("%s: %d nieuwe suggesties", label, new_here)
    return {"checked": len(follows), "suggested": suggested_total}


def accept_suggestion(sug_id: int):
    s = db.get_suggestion(sug_id)
    if not s:
        raise ValueError("Suggestie niet gevonden.")
    try:
        res = create_book({
            "title": s["title"], "author": s["author"], "language": s["language"],
            "isbn": s["isbn"], "cover_url": s["cover_url"], "description": s["description"],
            "year": s["year"], "source_id": s["source_id"],
        })
    except DuplicateBook:
        db.set_suggestion_state(sug_id, "added")
        return {"id": None, "note": "stond al op je lijst"}
    db.set_suggestion_state(sug_id, "added")
    return res


# ---------- Activity ----------

def activity():
    """Overzicht voor de Activity-tab: lopende downloads met live-gegevens
    uit qBittorrent, plus recente gebeurtenissen."""
    with db.get_conn() as conn:
        books = [dict(r) for r in conn.execute(
            "SELECT id, title, author, status, grabbed_release, grabbed_at, "
            "download_hash, progress, dl_state, last_error "
            "FROM books WHERE status IN ('downloading', 'failed') "
            "ORDER BY grabbed_at DESC"
        )]
        recent = [dict(r) for r in conn.execute(
            "SELECT h.created_at, h.event, h.release_title, h.detail, "
            "b.title AS book_title "
            "FROM history h LEFT JOIN books b ON b.id = h.book_id "
            "WHERE h.event IN ('grabbed', 'import', 'failed') "
            "ORDER BY h.created_at DESC LIMIT 25"
        )]

    by_hash = {}
    try:
        qbt = get_qbittorrent()
        for t in qbt.torrents_info(tag=None):
            by_hash[t.get("hash")] = t
    except Exception as e:
        log.debug("activity: kon qBittorrent niet bevragen: %s", e)

    downloads = []
    for b in books:
        t = by_hash.get(b.get("download_hash"), {})
        eta = t.get("eta")
        if eta in (None, 8640000) or (eta or 0) > 8639999:
            eta = None
        downloads.append({
            "book_id": b["id"],
            "title": b["title"],
            "author": b["author"],
            "status": b["status"],
            "release": b.get("grabbed_release"),
            "progress": t.get("progress", b.get("progress") or 0),
            "state": t.get("state") or b.get("dl_state"),
            "dlspeed": t.get("dlspeed"),
            "eta": eta,
            "num_seeds": t.get("num_seeds"),
            "num_leechs": t.get("num_leechs"),
            "size": t.get("size"),
            "last_error": b.get("last_error"),
        })
    return {"downloads": downloads, "recent": recent, "searching": search_progress()}


# ---------- Verwijderen ----------

def delete_books(ids, delete_files=False):
    ids = [int(i) for i in ids if i is not None]
    if not ids:
        return {"deleted": 0, "removed_files": [], "errors": []}

    placeholders = ",".join("?" * len(ids))
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT id, title, library_path FROM books WHERE id IN ({placeholders})",
            ids,
        )]

    removed_files, errors = [], []
    if delete_files:
        for r in rows:
            lp = r.get("library_path")
            if not lp:
                continue
            p = Path(lp)
            try:
                if p.is_file():
                    p.unlink()
                    removed_files.append(str(p))
                    log.info("Boek %s: bestand verwijderd %s", r["id"], p)
            except OSError as e:
                errors.append(f"{r['title']}: {e}")
                log.warning("Kon bestand niet verwijderen (%s): %s", p, e)

    with db.get_conn() as conn:
        conn.execute(f"DELETE FROM books WHERE id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM history WHERE book_id IN ({placeholders})", ids)
        conn.commit()

    return {"deleted": len(ids), "removed_files": removed_files, "errors": errors}


# ---------- Status ----------

def health():
    out = {
        "prowlarr": "unconfigured",
        "qbittorrent": "unconfigured",
        "last_search_run": db.get_setting("last_search_run"),
    }
    try:
        get_prowlarr().test_connection()
        out["prowlarr"] = "ok"
    except ConfigError:
        pass
    except Exception as e:
        out["prowlarr"] = f"error: {e}"
    try:
        get_qbittorrent().test_connection()
        out["qbittorrent"] = "ok"
    except ConfigError:
        pass
    except Exception as e:
        out["qbittorrent"] = f"error: {e}"

    try:
        st, detail = sources.zlibrary.status()
        out["zlibrary"] = f"error: {detail}" if st == "error" else st
    except Exception as e:  # noqa: BLE001
        out["zlibrary"] = f"error: {e}"
    return out


# ---------- Covers ----------

_COVER_UA = {"User-Agent": "EbookArr/1.0 (+local)"}


def _cover_ok(url):
    try:
        r = requests.get(url, headers=_COVER_UA, timeout=6, stream=True)
        ok = r.status_code == 200 and r.headers.get("content-type", "").lower().startswith("image/")
        clen = r.headers.get("content-length")
        if ok and clen is not None and int(clen) < 900:
            ok = False        # OL "blank" plaatje (meestal met ?default=false al een 404)
        if ok and clen is None:
            ok = len(next(r.iter_content(1200), b"")) >= 900
        r.close()
        return ok
    except (requests.RequestException, ValueError):
        return False


def resolve_cover(isbn="", title="", author="", stored=""):
    """Geef een werkende cover-URL of None. Resultaat wordt gecachet."""
    key = "cover|" + "|".join(x.strip().lower() for x in (isbn, title, author, stored))
    hit = db.cache_get(key)
    if hit is not None:
        return hit or None

    candidates = []
    if stored:
        candidates.append(stored)
    if isbn:
        digits = re.sub(r"[^0-9Xx]", "", isbn)
        if digits:
            candidates.append(
                f"https://covers.openlibrary.org/b/isbn/{digits}-L.jpg?default=false")
    if title:
        try:
            q = {"title": title, "limit": 1, "fields": "cover_i,isbn"}
            if author:
                q["author"] = author
            docs = requests.get("https://openlibrary.org/search.json", params=q,
                                headers=_COVER_UA, timeout=7).json().get("docs", [])
            if docs and docs[0].get("cover_i"):
                candidates.append(
                    f"https://covers.openlibrary.org/b/id/{docs[0]['cover_i']}-L.jpg?default=false")
        except (requests.RequestException, ValueError):
            pass

    found = next((u for u in dict.fromkeys(candidates) if _cover_ok(u)), None)
    db.cache_set(key, found or "", 30 * 24 * 3600)
    return found


def zlib_quota():
    """Z-Library-daglimiet voor de UI, of None."""
    try:
        if not sources.zlibrary.enabled():
            return None
        return sources.zlibrary.quota()
    except Exception:  # noqa: BLE001
        return None


def _force_explorer_foreground(folder: Path):
    """Zoek het Verkenner-venster van deze map en breng het écht naar voren
    (ook als er al een venster van open stond). Draait vertraagd in een thread."""
    import ctypes
    from ctypes import wintypes

    time.sleep(0.4)  # het venster even de tijd geven om te verschijnen
    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.BringWindowToTop.argtypes = [wintypes.HWND]
        u.SetActiveWindow.argtypes = [wintypes.HWND]

        want = folder.name.lower()
        hits = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lp):
            if not u.IsWindowVisible(hwnd):
                return True
            cbuf = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(hwnd, cbuf, 64)
            if cbuf.value not in ("CabinetWClass", "ExploreWClass"):
                return True
            tbuf = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(hwnd, tbuf, 512)
            if want in tbuf.value.lower():
                hits.append(hwnd)
            return True

        u.EnumWindows(_cb, 0)
        if not hits:
            return
        hwnd = hits[-1]
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
        our = k.GetCurrentThreadId()
        fg = u.GetForegroundWindow()
        tids = {u.GetWindowThreadProcessId(fg, None),
                u.GetWindowThreadProcessId(hwnd, None)} - {our, 0}
        for tid in tids:
            u.AttachThreadInput(our, tid, True)
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        u.SetActiveWindow(hwnd)
        for tid in tids:
            u.AttachThreadInput(our, tid, False)
    except Exception as e:  # noqa: BLE001 - puur cosmetisch, nooit laten crashen
        log.debug("kon Verkenner-venster niet naar voren halen: %s", e)


def _select_in_explorer(file_path: Path) -> bool:
    """Open Verkenner op de map, selecteer het bestand en breng het venster naar
    voren via de Windows-shell-API. Betrouwbaarder dan `explorer /select,`."""
    import ctypes

    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoInitialize(None)
        pidl = shell32.ILCreateFromPathW(str(file_path))
        if not pidl:
            return False
        try:
            hr = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
        finally:
            shell32.ILFree(pidl)
        return hr == 0
    except Exception as e:  # noqa: BLE001 - fallback pakt het op
        log.warning("SHOpenFolderAndSelectItems faalde: %s", e)
        return False


def reveal_in_explorer(path):
    """Open de bibliotheekmap in Verkenner (Windows), selecteer het bestand en
    breng het venster naar de voorgrond."""
    if not path:
        return False
    p = Path(path)
    folder = p.parent
    if not folder.is_dir():
        return False

    try:  # de achtergrond-app mag het nieuwe venster naar voren brengen
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except (OSError, AttributeError):
        pass

    opened = (_select_in_explorer(p) if p.exists() else False)
    if not opened:
        try:
            os.startfile(str(folder))  # noqa: S606
        except OSError as e:
            log.warning("Kon de bibliotheekmap niet openen: %s", e)
            return False

    threading.Thread(target=_force_explorer_foreground, args=(folder,),
                     daemon=True).start()
    return True
