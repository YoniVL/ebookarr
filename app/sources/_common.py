"""Gedeelde helpers voor downloadbronnen."""
import logging
import re

import requests

log = logging.getLogger("ebookarr.sources")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

EBOOK_EXTS = ("epub", "azw3", "mobi", "azw")


def http():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en,nl;q=0.8"})
    return s


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _sig_words(s: str):
    words = re.findall(r"[a-z0-9]+", (s or "").lower())
    stop = {"the", "a", "an", "of", "and", "to", "de", "het", "een", "van", "in"}
    return [w for w in words if w not in stop and len(w) > 1]


def title_matches(want: str, got: str) -> bool:
    """Titel-match die ondertitels/reeksnummers tolereert."""
    a, b = norm(want), norm(got)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    wa, wb = set(_sig_words(want)), set(_sig_words(got))
    if not wa:
        return False
    # minstens 70% van de betekenisvolle titelwoorden komt terug
    return len(wa & wb) / len(wa) >= 0.7


def author_matches(want: str, got: str) -> bool:
    if not want:
        return True
    got_n = norm(got)
    parts = [norm(p) for p in re.split(r"[,;&]| and ", want) if norm(p)]
    surname = norm(want.split()[-1]) if want.split() else ""
    if surname and surname in got_n:
        return True
    return any(p in got_n for p in parts if len(p) > 3)


def norm_lang(code) -> str | None:
    if not code:
        return None
    c = str(code).strip().lower()
    if c in ("nl", "nld", "dut", "dutch", "nederlands"):
        return "nl"
    if c in ("en", "eng", "english"):
        return "en"
    return c[:2] or None


def candidate(source, *, title, author="", fmt="epub", language=None, size=None,
              year=None, detail="", download=None, kind="direct"):
    """Uniforme kandidaat. `download` is bron-specifiek (url, md5, id, ...)."""
    return {
        "source": source,
        "kind": kind,
        "title": (title or "").strip(),
        "author": (author or "").strip(),
        "format": (fmt or "epub").lower().lstrip("."),
        "language": norm_lang(language),
        "size": size,
        "year": year,
        "detail": detail,
        "download": download,
    }


def guess_ext_from_headers(resp, fallback="epub") -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
    if m:
        ext = m.group(1).rsplit(".", 1)[-1].lower()
        if ext in EBOOK_EXTS or ext == "pdf":
            return ext
    ct = resp.headers.get("content-type", "").lower()
    if "epub" in ct:
        return "epub"
    if "mobipocket" in ct or "x-mobi" in ct:
        return "mobi"
    if "pdf" in ct:
        return "pdf"
    return fallback


def save_response(r, dest_dir, *, ext="epub"):
    """Schrijf een (streaming) requests-Response naar dest_dir en valideer dat
    het echt een bestand is en geen foutpagina. Geeft het Path terug."""
    from pathlib import Path

    r.raise_for_status()
    ct = r.headers.get("content-type", "").lower()
    if "text/html" in ct:
        raise RuntimeError("kreeg een webpagina i.p.v. een bestand "
                           "(login vereist, of link verlopen?)")
    real_ext = guess_ext_from_headers(r, ext)
    dest = Path(dest_dir) / f"download.{real_ext}"
    total = 0
    first = b""
    with open(dest, "wb") as fh:
        for chunk in r.iter_content(65536):
            if not first:
                first = chunk[:16]
            fh.write(chunk)
            total += len(chunk)
    if first.lstrip()[:2] in (b"<!", b"<h", b"<H"):
        dest.unlink(missing_ok=True)
        raise RuntimeError("kreeg een HTML-pagina i.p.v. een bestand")
    if total < 2048:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"download te klein ({total} bytes) — waarschijnlijk een foutpagina")
    log.info("Bron-download klaar: %s (%d KB)", dest.name, total // 1024)
    return dest


def download_file(url, dest_dir, *, session=None, ext="epub", timeout=120,
                  headers=None):
    """GET `url` naar dest_dir; geeft het Path terug. Bestandsnaam-extensie uit
    de response-headers indien mogelijk."""
    s = session or http()
    with s.get(url, stream=True, timeout=timeout, headers=headers or {}) as r:
        return save_response(r, dest_dir, ext=ext)
