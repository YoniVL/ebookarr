import json
import logging
import re
import sqlite3
from contextlib import closing, contextmanager, suppress
from datetime import datetime
from pathlib import Path

from . import obfuscate

log = logging.getLogger("ebookarr.db")

DB_PATH = Path(__file__).resolve().parent.parent / "ebookarr.db"
BACKUP_DIR = DB_PATH.parent / "backups"

# Instellingen die versleuteld opgeslagen worden (zie obfuscate.py)
SECRET_KEYS = {"qbittorrent_password", "prowlarr_api_key",
               "zlib_password", "zlib_email", "annas_key", "zlib_session",
               "opds_password"}

# Standaardwaarden voor nieuwe instellingen
SETTING_DEFAULTS = {
    "search_interval_hours": "6",
    "auto_download": "1",
    "min_seeders": "1",
    "prowlarr_categories": "7000,7020",
    "library_folder": "",
    "reading_languages": "en,nl",
    "stalled_hours": "24",
    "accept_kindle": "0",       # ook mobi/azw3 accepteren en naar epub converteren
    "calibre_path": "",         # pad naar ebook-convert.exe (leeg = automatisch zoeken)
    "embed_metadata": "1",      # titel/auteur/taal/cover in het epub schrijven (Calibre)
    # Downloadbronnen (vaste volgorde; torrents/Prowlarr altijd als laatste)
    "src_gutenberg": "1",
    "src_standardebooks": "1",
    "src_libgen": "0",       # veel ISP's (o.a. in BE) blokkeren LibGen-domeinen -> standaard uit
    "src_zlibrary": "0",
    "libgen_mirror": "",        # bv. libgen.is (leeg = automatisch proberen)
    "zlib_domain": "",          # bv. z-lib.io (leeg = standaardlijst proberen)
    "zlib_email": "",           # secret
    "zlib_password": "",        # secret
    "annas_key": "",            # secret; leeg = Anna's Archive uit
    "zlib_session": "",         # secret; gecachete Z-Library-login (domein + cookies)
    "opds_user": "ebookarr",    # login voor de OPDS-catalogus
    "opds_password": "",        # secret; leeg = OPDS zonder login (alleen LAN)
}


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


# ---------- Migraties ----------

def _m001_initial(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT,
            format_preference TEXT DEFAULT 'epub',
            status TEXT DEFAULT 'wanted',
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_checked TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)


def _add_column(conn, table, coldef):
    col = coldef.split()[0]
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")


def _m002_book_metadata(conn):
    for coldef in [
        "language TEXT",
        "isbn TEXT",
        "cover_url TEXT",
        "description TEXT",
        "year INTEGER",
        "source_id TEXT",
        "monitored INTEGER DEFAULT 1",
        "grabbed_release TEXT",
        "grabbed_guid TEXT",
        "grabbed_at TEXT",
        "download_hash TEXT",
        "progress REAL DEFAULT 0",
        "dl_state TEXT",
        "library_path TEXT",
        "last_error TEXT",
    ]:
        _add_column(conn, "books", coldef)


def _m003_history(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER,
            event TEXT,
            release_title TEXT,
            guid TEXT,
            detail TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _m004_encrypt_existing_secrets(conn):
    for key in SECRET_KEYS:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row and row["value"] and not obfuscate.is_encrypted(row["value"]):
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = ?",
                (obfuscate.encrypt(row["value"]), key),
            )


def _m005_authors(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS authors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_checked TEXT,
            last_result TEXT
        )
    """)


def _m006_follows_and_suggestions(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS follows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL DEFAULT 'author',   -- 'author' of 'series'
            name TEXT NOT NULL,
            author TEXT,                            -- optioneel, om een reeks te verankeren
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_checked TEXT,
            last_result TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT,
            language TEXT,
            isbn TEXT,
            cover_url TEXT,
            year INTEGER,
            source_id TEXT,
            description TEXT,
            found_via TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            state TEXT DEFAULT 'pending'            -- pending | added | dismissed
        )
    """)
    # bestaande gevolgde auteurs meenemen
    for row in conn.execute("SELECT name, added_at, last_checked, last_result FROM authors"):
        conn.execute(
            "INSERT INTO follows (type, name, added_at, last_checked, last_result) "
            "VALUES ('author', ?, ?, ?, ?)",
            (row["name"], row["added_at"], row["last_checked"], row["last_result"]),
        )


def _m007_pref_language(conn):
    _add_column(conn, "books", "pref_language TEXT")


def _m008_book_source(conn):
    # Via welke bron (gutenberg/libgen/zlibrary/prowlarr/...) is dit boek gepakt
    _add_column(conn, "books", "source TEXT")


def _m009_kv_cache(conn):
    # Persistente cache voor metadata-API-antwoorden (Google Books 429't vaak)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kv_cache ("
        "  k TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL)"
    )


MIGRATIONS = [
    _m001_initial,
    _m002_book_metadata,
    _m003_history,
    _m004_encrypt_existing_secrets,
    _m005_authors,
    _m006_follows_and_suggestions,
    _m007_pref_language,
    _m008_book_source,
    _m009_kv_cache,
]


def init_db():
    with get_conn() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, migrate in enumerate(MIGRATIONS, start=1):
            if i > version:
                migrate(conn)
                conn.execute(f"PRAGMA user_version = {i}")
                conn.commit()

        # Standaardinstellingen aanvullen (overschrijft bestaande niet)
        for key, val in SETTING_DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, val),
            )
        conn.commit()


def backup(keep: int = 7):
    """Consistente kopie van de database naar backups/ebookarr-YYYYMMDD.db.
    Houdt de laatste `keep` kopieën."""
    BACKUP_DIR.mkdir(exist_ok=True)
    dest = BACKUP_DIR / f"ebookarr-{datetime.now():%Y%m%d}.db"
    try:
        with closing(sqlite3.connect(DB_PATH)) as src, \
             closing(sqlite3.connect(dest)) as dst:
            src.backup(dst)
    except sqlite3.Error as e:
        log.warning("Back-up mislukt: %s", e)
        return None

    for old in sorted(BACKUP_DIR.glob("ebookarr-*.db"))[:-keep]:
        with suppress(OSError):
            old.unlink()
    log.info("Database-back-up: %s", dest.name)
    return dest


# ---------- Instellingen ----------

def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return default
    value = row["value"]
    if key in SECRET_KEYS and value:
        try:
            return obfuscate.decrypt(value)
        except ValueError:
            return default
    return value


def set_setting(key: str, value: str):
    if key in SECRET_KEYS and value:
        value = obfuscate.encrypt(value)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def get_all_settings():
    """Alle instellingen, met secrets ontsleuteld. Niet direct naar de UI sturen."""
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = {}
    for r in rows:
        key, value = r["key"], r["value"]
        if key in SECRET_KEYS and value:
            try:
                value = obfuscate.decrypt(value)
            except ValueError:
                value = ""
        out[key] = value
    return out


# ---------- Persistente cache (metadata-API's) ----------

def cache_get(key):
    import time
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload, expires FROM kv_cache WHERE k = ?", (key,)
        ).fetchone()
    if not row or row["expires"] < time.time():
        return None
    try:
        return json.loads(row["payload"])
    except (ValueError, TypeError):
        return None


def cache_set(key, value, ttl_seconds):
    import time
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO kv_cache (k, expires, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(k) DO UPDATE SET expires=excluded.expires, payload=excluded.payload",
            (key, time.time() + ttl_seconds, json.dumps(value)),
        )
        conn.execute("DELETE FROM kv_cache WHERE expires < ?", (time.time(),))
        conn.commit()


# ---------- Geschiedenis ----------

def add_history(book_id, event, release_title=None, guid=None, detail=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO history (book_id, event, release_title, guid, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (book_id, event, release_title, guid, detail),
        )
        conn.commit()


def already_grabbed(book_id, guid) -> bool:
    if not guid:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM history WHERE book_id = ? AND guid = ? AND event = 'grabbed'",
            (book_id, guid),
        ).fetchone()
    return row is not None


# ---------- Volgen (auteurs + reeksen) ----------

def list_follows():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM follows ORDER BY type, name COLLATE NOCASE"
        )]


def add_follow(ftype: str, name: str, author: str = ""):
    name = (name or "").strip()
    author = (author or "").strip()
    ftype = ftype if ftype in ("author", "series") else "author"
    if not name:
        return None
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM follows WHERE type = ? AND name = ? COLLATE NOCASE "
            "AND COALESCE(author,'') = ?",
            (ftype, name, author),
        ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO follows (type, name, author) VALUES (?, ?, ?)",
            (ftype, name, author or None),
        )
        conn.commit()
        return cur.lastrowid


def delete_follow(follow_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM follows WHERE id = ?", (follow_id,))
        conn.commit()


def touch_follow(follow_id: int, result: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE follows SET last_checked = CURRENT_TIMESTAMP, last_result = ? WHERE id = ?",
            (result, follow_id),
        )
        conn.commit()


# ---------- Suggesties ----------

def list_suggestions(state: str = "pending"):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM suggestions WHERE state = ? ORDER BY found_via, title COLLATE NOCASE",
            (state,),
        )]


def count_pending_suggestions():
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE state = 'pending'"
        ).fetchone()[0]


def suggestion_exists(title, author, isbn):
    """Al eerder gesuggereerd (in welke staat dan ook)? Vergelijkt op hoofdtitel
    (zonder ondertitel) zodat 'Death's Domain' en 'Death's Domain: A Discworld
    Mapp' als hetzelfde tellen."""
    from .metadata import _norm_title, main_title

    with get_conn() as conn:
        rows = conn.execute("SELECT title, author, isbn FROM suggestions").fetchall()
    ni = re.sub(r"[^a-z0-9]+", "", (isbn or "").lower())
    mt = main_title(title)
    na = _norm_title((author or "").split(",")[0])
    for r in rows:
        if ni and re.sub(r"[^a-z0-9]+", "", (r["isbn"] or "").lower()) == ni:
            return True
        if mt and main_title(r["title"]) == mt and (
            not na or _norm_title((r["author"] or "").split(",")[0]) == na):
            return True
    return False


def add_suggestion(data: dict, found_via: str):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO suggestions
                   (title, author, language, isbn, cover_url, year, source_id,
                    description, found_via)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data.get("title"), data.get("author"), data.get("language"),
             data.get("isbn"), data.get("cover_url"), data.get("year"),
             data.get("source_id"), data.get("description"), found_via),
        )
        conn.commit()
        return cur.lastrowid


def set_suggestion_state(sug_id, state):
    with get_conn() as conn:
        conn.execute("UPDATE suggestions SET state = ? WHERE id = ?", (state, sug_id))
        conn.commit()


def delete_suggestion(sug_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM suggestions WHERE id = ?", (sug_id,))
        conn.commit()


def get_suggestion(sug_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (sug_id,)).fetchone()
        return dict(row) if row else None
