"""Boeken naar de Kindle sturen via 'Send to Kindle' per e-mail.

Amazon geeft elke Kindle een eigen @kindle.com-adres. Een mail met het epub als
bijlage (vanaf een goedgekeurd afzenderadres) komt vanzelf op het toestel.
We versturen via een gewone SMTP-server (bv. Gmail met een app-wachtwoord).
"""
import io
import logging
import mimetypes
import posixpath
import re
import smtplib
import ssl
import time
import zipfile
from email.message import EmailMessage
from pathlib import Path

from . import db

log = logging.getLogger("ebookarr.kindle")

MAX_BYTES = 50 * 1024 * 1024   # limiet van Amazon per mail
OK_EXT = {".epub", ".pdf", ".docx", ".txt", ".rtf", ".html"}


# Gmail weigert zip-bestanden (een epub is er één) met "uitvoerbare" bestanden erin,
# bv. het watermerkbestand 'oceanofpdf.com' dat in sommige epubs zit.
BAD_EXT = {".com", ".exe", ".js", ".bat", ".vbs", ".scr", ".jar", ".cmd", ".msi", ".dll", ".lnk"}


class KindleError(Exception):
    pass


def _bad_named_items(zin, names, dropped):
    """Hoofdstukbestanden zonder .html-extensie (bv. '.html_split_000'): Amazon
    herkent die niet en levert het boek dan af als vaste lay-out (fout E016).
    Geeft {oude basisnaam: nieuwe basisnaam}."""
    opf = next((n for n in names if n.lower().endswith(".opf")), None)
    if not opf:
        return {}
    txt = zin.read(opf).decode("utf-8", "ignore")
    out = {}
    for m in re.finditer(r"<item[^>]*>", txt):
        tag = m.group(0)
        if "application/xhtml+xml" not in tag:
            continue
        h = re.search(r'href="([^"]+)"', tag)
        if not h:
            continue
        base = posixpath.basename(h.group(1).split("#")[0])
        if base and not re.search(r"\.(x?html?|xml)$", base, re.I):
            out[base] = base.lstrip(".") + ".xhtml"
    # alleen als de nieuwe naam nergens al bestaat
    existing = {posixpath.basename(n) for n in names}
    return {o: n for o, n in out.items() if n not in existing}


def clean_epub(path: Path) -> bytes:
    """Her-verpak het epub zonder mapitems en zonder verdachte bestanden (de
    originele blijft ongemoeid). Bij een kapot zip-bestand: de ruwe bytes."""
    raw = path.read_bytes()
    try:
        zin = zipfile.ZipFile(io.BytesIO(raw))
        names = [n for n in zin.namelist() if not n.endswith("/")]
        dropped = [n for n in names if posixpath.splitext(n)[1].lower() in BAD_EXT]
        renames = _bad_named_items(zin, names, dropped)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            if "mimetype" in names:       # moet eerst en onaangepast (stored)
                zout.writestr(zipfile.ZipInfo("mimetype"), zin.read("mimetype"), zipfile.ZIP_STORED)
            for n in names:
                if n == "mimetype" or n in dropped:
                    continue
                data = zin.read(n)
                if renames and n.lower().endswith((".opf", ".ncx", ".xhtml", ".html", ".htm", ".xml")):
                    txt = data.decode("utf-8", "ignore")
                    for old_b, new_b in renames.items():
                        txt = re.sub(r"(?<![\w.\-])" + re.escape(old_b) + r"(?![\w.\-])", new_b, txt)
                    data = txt.encode("utf-8")
                if n.lower().endswith(".opf") and dropped:
                    txt = data.decode("utf-8", "ignore")
                    for d in dropped:
                        base = re.escape(posixpath.basename(d))
                        txt = re.sub(r'<item[^>]*href="[^"]*' + base + r'"[^>]*/?>', "", txt)
                    data = txt.encode("utf-8")
                zout.writestr(posixpath.join(posixpath.dirname(n), renames[posixpath.basename(n)])
                              if posixpath.basename(n) in renames else n, data)
        if renames:
            log.info("Kindle: %d bestandsnamen zonder extensie hersteld in %s", len(renames), path.name)
        if dropped:
            log.info("Kindle: %s uit %s gehaald voor het versturen", ", ".join(dropped), path.name)
        return out.getvalue()
    except (zipfile.BadZipFile, KeyError, OSError):
        return raw


def friendly_error(e) -> str:
    code = getattr(e, "smtp_code", None)
    text = str(getattr(e, "smtp_error", b"") or e)
    if code == 552 or "5.7.0" in text:
        return "Gmail blokkeert dit bestand als mogelijk onveilig"
    if code in (421, 450, 451, 452) or "limit" in text.lower():
        return "Gmail-verzendlimiet bereikt — probeer het later opnieuw"
    return f"verzenden mislukt ({code or 'fout'})"


def config():
    def g(k, d=""):
        return db.get_setting(k) or d

    try:
        port = int(g("smtp_port", "587") or 587)
    except ValueError:
        port = 587
    return {
        "to": g("kindle_email").strip(),
        "host": g("smtp_host").strip(),
        "port": port,
        "user": g("smtp_user").strip(),
        "password": g("smtp_password"),
        "sender": g("smtp_from").strip() or g("smtp_user").strip(),
    }


def _check(cfg):
    if not cfg["to"] or "@" not in cfg["to"]:
        raise KindleError("Vul eerst je Kindle-e-mailadres in (bv. naam_123@kindle.com).")
    if not cfg["host"] or not cfg["sender"]:
        raise KindleError("Vul eerst de SMTP-server en het afzenderadres in.")


def _connect(cfg):
    ctx = ssl.create_default_context()
    try:
        if cfg["port"] == 465:
            smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30, context=ctx)
        else:
            smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.ehlo()
        if cfg["user"]:
            smtp.login(cfg["user"], cfg["password"])
        return smtp
    except smtplib.SMTPAuthenticationError as e:
        raise KindleError("Inloggen bij de mailserver mislukt — controleer gebruikersnaam en "
                          "(app-)wachtwoord.") from e
    except (OSError, smtplib.SMTPException) as e:
        raise KindleError(f"Mailserver niet bereikbaar: {e}") from e


def test_connection():
    cfg = config()
    _check(cfg)
    smtp = _connect(cfg)
    try:
        smtp.quit()
    except Exception:  # noqa: BLE001
        pass
    return f"Verbonden met {cfg['host']}."


def send_files(items):
    """items: lijst van (book_id, path, title). Eén mail per boek.
    Geeft {book_id: None | foutmelding}."""
    cfg = config()
    _check(cfg)
    results = {}
    smtp = _connect(cfg)
    try:
        for book_id, path, title in items:
            p = Path(path) if path else None
            if not p or not p.is_file():
                results[book_id] = "bestand niet gevonden"
                continue
            if p.suffix.lower() not in OK_EXT:
                results[book_id] = f"{p.suffix} wordt door Kindle niet geaccepteerd (epub wel)"
                continue
            if p.stat().st_size > MAX_BYTES:
                results[book_id] = "bestand groter dan 50 MB"
                continue
            msg = EmailMessage()
            msg["From"] = cfg["sender"]
            msg["To"] = cfg["to"]
            msg["Subject"] = title or p.stem
            msg.set_content("Verstuurd door EbookArr.")
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if p.suffix.lower() == ".epub":
                ctype = "application/epub+zip"
            main, sub = ctype.split("/", 1)
            payload = clean_epub(p) if p.suffix.lower() == ".epub" else p.read_bytes()
            msg.add_attachment(payload, maintype=main, subtype=sub, filename=p.name)
            try:
                smtp.send_message(msg)
                results[book_id] = None
                log.info("Naar Kindle gestuurd: %s", p.name)
                time.sleep(0.4)   # niet alles in één salvo naar Gmail/Amazon
            except smtplib.SMTPServerDisconnected as e:
                results[book_id] = "verbinding met de mailserver verbroken"
                log.warning("Kindle-verzending mislukt voor %s: %s", p.name, e)
                try:
                    smtp = _connect(cfg)
                except KindleError:
                    break
            except (OSError, smtplib.SMTPException) as e:
                results[book_id] = friendly_error(e)
                log.warning("Kindle-verzending mislukt voor %s: %s", p.name, e)
    finally:
        try:
            smtp.quit()
        except Exception:  # noqa: BLE001
            pass
    return results
