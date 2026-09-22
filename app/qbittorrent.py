"""Kleine client voor de qBittorrent Web API."""
import logging

import requests

log = logging.getLogger("ebookarr.qbittorrent")


class QBittorrentClient:
    def __init__(self, base_url: str, username: str, password: str, category: str = "ebooks"):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.category = category
        self._session = requests.Session()
        # qBittorrent's WebUI weigert requests zonder passende Referer/Origin
        # (CSRF-bescherming) met een 403. Zet ze standaard op de WebUI-URL.
        self._session.headers.update(
            {"Referer": self.base_url, "Origin": self.base_url}
        )
        self._logged_in = False

    # ---------- Auth ----------

    def login(self):
        try:
            resp = self._session.post(
                f"{self.base_url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=10,
            )
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                f"Kan qBittorrent niet bereiken op {self.base_url}. "
                "Draait qBittorrent en klopt de URL/poort?"
            ) from e

        body = resp.text.strip()
        got_sid = "SID" in resp.cookies or "SID" in self._session.cookies
        log.info(
            "qBittorrent login: url=%s user=%r pwlen=%d -> HTTP %s SID=%s body=%r",
            self.base_url, self.username, len(self.password or ""),
            resp.status_code, got_sid, body[:120],
        )

        if resp.status_code == 403:
            raise RuntimeError(
                "qBittorrent gaf 403 terug. Meestal: je IP is tijdelijk "
                "geblokkeerd na te veel mislukte pogingen (herstart qBittorrent), "
                "of 'Host header validation' blokkeert de URL. Gebruik "
                "http://localhost:<poort> of http://127.0.0.1:<poort>."
            )
        if body == "Fails.":
            raise RuntimeError(
                "qBittorrent login mislukt — controleer gebruikersnaam/wachtwoord "
                "(qBittorrent > Tools > Opties > Web UI)."
            )
        if "ban" in body.lower():
            raise RuntimeError(
                "qBittorrent heeft je IP tijdelijk geblokkeerd na te veel mislukte "
                "inlogpogingen. Herstart qBittorrent en probeer opnieuw."
            )

        version = self._verify_logged_in()
        self._logged_in = True
        return version

    def _ensure_login(self):
        if not self._logged_in:
            self.login()

    def _verify_logged_in(self):
        try:
            resp = self._session.get(
                f"{self.base_url}/api/v2/app/version", timeout=10
            )
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"Kan qBittorrent niet bereiken op {self.base_url}.") from e

        log.info(
            "qBittorrent verificatie GET /api/v2/app/version -> HTTP %s body=%r",
            resp.status_code, resp.text[:80],
        )
        if resp.status_code == 403:
            raise RuntimeError(
                "qBittorrent accepteerde de login niet (geen geldige sessie). "
                "Controleer gebruikersnaam/wachtwoord."
            )
        if resp.status_code == 404 or not resp.text.strip().startswith("v"):
            raise RuntimeError(
                f"Wat op {self.base_url} draait lijkt geen qBittorrent Web UI te "
                "zijn. Klopt de poort? (qBittorrent > Tools > Opties > Web UI)"
            )
        resp.raise_for_status()
        return resp.text.strip()

    def test_connection(self):
        return self.login()

    # ---------- Torrents ----------

    def add_torrent(self, download_url: str, tag: str | None = None):
        self._ensure_login()
        self._session.post(
            f"{self.base_url}/api/v2/torrents/createCategory",
            data={"category": self.category},
            timeout=10,
        )
        data = {
            "urls": download_url,
            "category": self.category,
            "autoTMM": "false",
        }
        if tag:
            data["tags"] = tag
        resp = self._session.post(
            f"{self.base_url}/api/v2/torrents/add",
            data=data,
            timeout=30,
        )
        resp.raise_for_status()
        if resp.text.strip().lower() == "fails.":
            raise RuntimeError(
                "qBittorrent weigerde de torrent (ongeldige of onbereikbare "
                "download-URL)."
            )
        return resp.text

    def torrents_info(self, tag: str | None = None, hashes: str | None = None):
        self._ensure_login()
        params = {}
        if tag:
            params["tag"] = tag
        if hashes:
            params["hashes"] = hashes
        resp = self._session.get(
            f"{self.base_url}/api/v2/torrents/info", params=params, timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    def torrent_files(self, torrent_hash: str):
        self._ensure_login()
        resp = self._session.get(
            f"{self.base_url}/api/v2/torrents/files",
            params={"hash": torrent_hash},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
