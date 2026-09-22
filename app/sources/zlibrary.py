"""Z-Library (gratis account, ~10 downloads/dag).

Z-Library's clearnet-domeinen wisselen vaak. We proberen een standaardlijst;
werkt die niet, zet dan een werkend domein vast bij Instellingen (zlib_domain).
Gebruikt de JSON-API onder /eapi/ met cookie-authenticatie.

De download-route (/dl/...) van sommige mirrors zit achter een JavaScript
"proof of work"-check ("Checking your browser ..."): de pagina zoekt een getal
i zodat sha1(seed + i) bepaalde bytes heeft, zet dat als cookie en herlaadt.
`_solve_challenge` doet datzelfde in Python (± 65k sha1-pogingen, < 1s).
"""
import hashlib
import json
import re
import threading
import time

import requests

from . import _common as c

NAME = "zlibrary"
LABEL = "Z-Library"

DOMAINS = ("z-lib.io", "z-library.sk", "z-lib.fm", "1lib.sk", "zlibrary-global.se",
           "z-lib.gs", "z-lib.gl")

_lock = threading.Lock()
_session_cache = {"domain": None, "cookies": None}

_CHALLENGE_MARK = "Checking your browser"
_ARR_RE = re.compile(r"a0_0x2a54=\[([^\]]+)\]")
_ROT_RE = re.compile(r"\(a0_0x2a54,(0x[0-9a-fA-F]+)\)\)")
_TARGET_RE = re.compile(
    r"s\[n1\]===(0x[0-9a-fA-F]+)\)&&\(s\[n1\+0x1\]===(0x[0-9a-fA-F]+)\)")


def enabled():
    from .. import db
    return (db.get_setting("src_zlibrary") == "1"
            and bool(db.get_setting("zlib_email"))
            and bool(db.get_setting("zlib_password")))


def _domains():
    from .. import db
    d = (db.get_setting("zlib_domain") or "").strip().replace("https://", "").strip("/")
    return [d] if d else list(DOMAINS)


def _is_challenge(r):
    return (r.status_code in (403, 503)
            and "text/html" in r.headers.get("content-type", "")
            and _CHALLENGE_MARK in r.text)


def _solve_challenge(html, s, domain):
    """Los de 'Checking your browser'-puzzel op en zet het cookie op de sessie.
    Geeft True als het gelukt is."""
    arr_m = _ARR_RE.search(html)
    rot_m = _ROT_RE.search(html)
    if not arr_m or not rot_m:
        return False
    arr = re.findall(r"'([^']*)'", arr_m.group(1))
    if len(arr) < 3:
        return False
    shifts = int(rot_m.group(1), 16) % len(arr)
    a = arr[shifts:] + arr[:shifts]
    tokname, seed = a[0], a[2]
    if not re.fullmatch(r"[0-9A-Fa-f]{20,}", seed):
        return False
    n1 = int("0x" + seed[0], 16)
    tb = _TARGET_RE.search(html)
    t0, t1 = (int(tb.group(1), 16), int(tb.group(2), 16)) if tb else (0xB0, 0x0B)
    for i in range(4_000_000):
        d = hashlib.sha1(f"{seed}{i}".encode()).digest()  # noqa: S324
        if d[n1] == t0 and d[n1 + 1] == t1:
            s.cookies.set(tokname.rstrip("="), f"{seed}{i}", domain=domain)
            s.cookies.set("c_time", "1.5", domain=domain)
            c.log.info("Z-Library: browser-check opgelost (i=%d)", i)
            return True
    return False


def _raw_request(s, method, url, **kw):
    """s.request met herkansing bij een verbroken verbinding — Z-Library's
    servers resetten de connectie regelmatig (ConnectionResetError 10054)."""
    last = None
    for attempt in range(4):
        try:
            return s.request(method, url, **kw)
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            last = e
            if "getaddrinfo failed" in str(e) or "NameResolution" in str(e):
                break  # DNS-fout: opnieuw proberen heeft geen zin
            c.log.info("Z-Library: verbinding mislukt (poging %d): %s", attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    raise last


def _request_solving(s, domain, method, url, **kw):
    """HTTP-request met herkansing + automatische afhandeling van de browser-check.
    De check accepteert de oplossing niet altijd meteen, dus we proberen het
    een paar keer opnieuw."""
    r = _raw_request(s, method, url, **kw)
    for _ in range(4):
        if not _is_challenge(r):
            return r
        if not _solve_challenge(r.text, s, domain):
            return r
        time.sleep(1.2)  # c_time moet aannemelijk zijn (geen bot-snelheid)
        r = _raw_request(s, method, url, **kw)
    return r


def _get_solving(s, domain, url, **kw):
    return _request_solving(s, domain, "GET", url, **kw)


def _save_session(domain, s):
    from .. import db
    cookies = requests.utils.dict_from_cookiejar(s.cookies)
    _session_cache.update(domain=domain, cookies=cookies)
    try:
        db.set_setting("zlib_session", json.dumps({"domain": domain, "cookies": cookies}))
    except Exception:  # noqa: BLE001
        pass


def _load_session():
    from .. import db
    raw = db.get_setting("zlib_session")
    if not raw:
        return None, None
    try:
        j = json.loads(raw)
        return j.get("domain"), j.get("cookies") or {}
    except (ValueError, TypeError):
        return None, None


def _session_valid(s, domain):
    """Snelle check of de opgeslagen cookies nog werken."""
    try:
        r = _get_solving(s, domain, f"https://{domain}/eapi/user/profile", timeout=15)
        return bool(r.json().get("user"))
    except (requests.RequestException, ValueError):
        return False


def _login(s, domain, email, password):
    """Probeer in te loggen; geeft True als er auth-cookies gezet zijn."""
    try:
        r = _request_solving(s, domain, "POST", f"https://{domain}/eapi/user/login",
                             data={"email": email, "password": password}, timeout=20)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except (requests.RequestException, ValueError):
        j = {}
    user = (j.get("user") or {})
    uid = user.get("id") or j.get("user_id")
    key = user.get("remix_userkey") or j.get("remix_userkey")
    if uid and key:
        s.cookies.set("remix_userid", str(uid), domain=domain)
        s.cookies.set("remix_userkey", str(key), domain=domain)
        return True
    # fallback: klassieke rpc.php-login zet zelf cookies
    try:
        s.post(f"https://{domain}/rpc.php",
               data={"action": "login", "email": email, "password": password,
                     "gg_json_mode": "1", "site_mode": "books"},
               timeout=20)
    except requests.RequestException:
        return False
    return "remix_userkey" in s.cookies


def _ensure_session(login=True):
    """Geef (session, domein). Met login=False wordt er niet ingelogd: dan
    komt er `(None, None)` als er geen bruikbare sessie klaarstaat (handig voor
    een niet-kritieke check zoals de daglimiet)."""
    from .. import db
    email = db.get_setting("zlib_email")
    password = db.get_setting("zlib_password")
    with _lock:
        # 1) sessie uit het geheugen
        if _session_cache["cookies"]:
            s = c.http()
            s.cookies.update(_session_cache["cookies"])
            return s, _session_cache["domain"]
        # 2) opgeslagen sessie (blijft dagen geldig -> geen login-limiet raken)
        sdomain, scookies = _load_session()
        if sdomain and scookies:
            s = c.http()
            s.cookies.update(scookies)
            if _session_valid(s, sdomain):
                _session_cache.update(domain=sdomain, cookies=scookies)
                return s, sdomain
        if not login:
            return None, None
        # 3) verse login (evt. over meerdere domeinen)
        last_err = None
        for domain in _domains():
            for attempt in (1, 2, 3):
                s = c.http()
                try:
                    ok = _login(s, domain, email, password)
                except requests.RequestException as e:
                    last_err = e
                    ok = False
                if ok:
                    _save_session(domain, s)
                    c.log.info("Z-Library: ingelogd op %s", domain)
                    _mark(True)
                    return s, domain
                c.log.info("Z-Library: login-poging %d op %s mislukt", attempt, domain)
        msg = ("kon niet inloggen (klopt e-mail/wachtwoord? domein bereikbaar?)"
               f"{f' [{last_err}]' if last_err else ''}")
        _mark(False, msg)
        raise RuntimeError(f"Z-Library: {msg}")


def _reset_session():
    from .. import db
    with _lock:
        _session_cache.update(domain=None, cookies=None)
    try:
        db.set_setting("zlib_session", "")
    except Exception:  # noqa: BLE001
        pass


# ---------- Gezondheid (voor het statusbolletje in de UI) ----------

_health = {"ok": None, "detail": "", "at": 0.0}


def _mark(ok, detail=""):
    _health.update(ok=ok, detail=detail, at=time.time())


def status():
    """('ok' | 'error' | 'unconfigured' | 'unknown', detail-tekst).
    Doet nooit een (trage) login — leunt op de laatste echte zoek/download-poging
    en een goedkope sessiecheck."""
    if not enabled():
        return "unconfigured", ""
    stale = _health["ok"] is None or time.time() - _health["at"] > 300
    if stale:
        try:
            s, dom = _ensure_session(login=False)
            if s and _session_valid(s, dom):
                _mark(True)
            # geen sessie in cache -> laten we 'unknown', tot een echte zoekronde draait
        except Exception as e:  # noqa: BLE001
            _mark(False, str(e))
    if _health["ok"] is True:
        return "ok", ""
    if _health["ok"] is False:
        return "error", _health["detail"] or "Z-Library reageert niet"
    return "unknown", "nog niet gecontroleerd"


_quota_cache = {"at": 0.0, "data": None}


def quota(force=False):
    """{'used', 'limit', 'left', 'resets_in'} of None. ~2 min gecachet."""
    now = time.time()
    if not force and _quota_cache["data"] and now - _quota_cache["at"] < 120:
        return _quota_cache["data"]
    data = None
    try:
        s, domain = _ensure_session(login=False)  # niet inloggen voor een UI-check
        if s:
            j = _get_solving(s, domain, f"https://{domain}/papi/user/dstats",
                             timeout=12).json()
            limit = int(j.get("dailyDownloadsLimit") or 0)
            used = int(j.get("dailyDownloads") or 0)
            data = {"used": used, "limit": limit, "left": max(limit - used, 0),
                    "resets_in": j.get("resetTime")}
            _mark(True)
    except (requests.RequestException, ValueError, RuntimeError, KeyError):
        data = None
    _quota_cache.update(at=now, data=data)
    return data


def quota_left():
    q = quota()
    return q["left"] if q else None   # None = onbekend -> gewoon proberen


def search(book: dict):
    q = " ".join(p for p in (book.get("title"), book.get("author")) if p).strip()
    if not q:
        return []
    s, domain = _ensure_session()
    url = f"https://{domain}/eapi/book/search"
    # let op: POST met form-data; GET of JSON geeft de populaire lijst i.p.v. zoeken
    payload = {"message": q, "limit": 30, "page": 1}
    try:
        r = _request_solving(s, domain, "POST", url, data=payload, timeout=25)
        if r.status_code in (401, 403):  # sessie verlopen -> 1x opnieuw inloggen
            _reset_session()
            s, domain = _ensure_session()
            r = _request_solving(s, domain, "POST", f"https://{domain}/eapi/book/search",
                                 data=payload, timeout=25)
        books = r.json().get("books", [])
        _mark(True)
    except (requests.RequestException, ValueError) as e:
        _mark(False, str(e))
        raise RuntimeError(f"Z-Library: zoeken mislukt ({e})") from e

    want_title = book.get("title") or ""
    want_author = book.get("author") or ""
    want_lang = c.norm_lang(book.get("pref_language") or book.get("language"))

    def _score(b, ext, size):
        s = 0.0
        if c.norm(b.get("title")) == c.norm(want_title):
            s += 10                                   # exacte titel
        if want_lang and c.norm_lang(b.get("language")) == want_lang:
            s += 6                                    # juiste taal
        if ext == "epub":
            s += 4
        mb = (size or 0) / (1024 * 1024)
        if 0.12 <= mb <= 4:
            s += 3                                    # normale roman-grootte
        elif mb > 8:
            s -= 3                                    # opgeblazen/gescand
        try:
            s += min(float(b.get("qualityScore") or 0), 5) / 2
        except (TypeError, ValueError):
            pass
        return s

    scored = []
    for b in books:
        ext = (b.get("extension") or "epub").lower()
        if ext in ("pdf", "fb2", "djvu"):
            continue
        title, author = b.get("title") or "", b.get("author") or ""
        if want_title and not c.title_matches(want_title, title):
            continue
        if want_author and not c.author_matches(want_author, author):
            continue
        try:
            size = int(b.get("filesize") or 0) or None
        except (TypeError, ValueError):
            size = None
        scored.append((_score(b, ext, size), b, title, author, ext, size))

    scored.sort(key=lambda t: t[0], reverse=True)

    out, seen = [], set()
    for _, b, title, author, ext, size in scored:
        dedup = (c.norm(title), ext)          # meerdere identieke edities -> 1
        if dedup in seen:
            continue
        seen.add(dedup)
        # Z-Library geeft soms een onzin-'dl' (bv. "exactEnd"); alleen echte paden houden
        dl = b.get("dl")
        if not (isinstance(dl, str) and (dl.startswith("/") or dl.startswith("http"))):
            dl = None
        out.append(c.candidate(
            NAME, title=title, author=author, fmt=ext,
            language=b.get("language"), size=size,
            year=b.get("year"), detail=f"Z-Library ({domain})",
            download={"id": b.get("id"), "hash": b.get("hash"), "domain": domain,
                      "dl": dl, "md5": b.get("md5")},
        ))
        if len(out) >= 12:
            break
    return out


def _abs(url, domain):
    if not isinstance(url, str) or not (url.startswith("/") or url.startswith("http")):
        return None
    return url if url.startswith("http") else f"https://{domain}{url}"


def download(cand: dict, dest_dir):
    d = cand["download"]
    s, domain = _ensure_session()

    # 1) dl-link uit het zoekresultaat, 2) uit het boek-detail, 3) klassieke route
    candidates = [_abs(d.get("dl"), domain)]
    try:
        j = _get_solving(s, domain, f"https://{domain}/eapi/book/{d['id']}/{d['hash']}",
                         timeout=20).json()
        dl = (j.get("book") or {}).get("dl") or j.get("dl")
        candidates.append(_abs(dl, domain))
    except (requests.RequestException, ValueError):
        pass
    candidates.append(f"https://{domain}/dl/{d['id']}/{d['hash']}")
    candidates = [u for u in dict.fromkeys(candidates) if u]

    ext = cand.get("format", "epub")
    last_exc = None
    for url in candidates:
        try:
            r = _get_solving(s, domain, url, stream=True, timeout=180,
                             allow_redirects=True)
        except requests.RequestException as e:
            last_exc = e
            continue
        if r.status_code in (403, 429):
            raise RuntimeError(
                "Z-Library: downloadlimiet bereikt of sessie verlopen "
                "(gratis account = ~10 per dag)."
            )
        try:
            return c.save_response(r, dest_dir, ext=ext)
        except RuntimeError as e:
            last_exc = e
            continue
    raise RuntimeError(f"Z-Library: download mislukt ({last_exc})")
