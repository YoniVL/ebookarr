"""Downloadbronnen. Vaste prioriteitsvolgorde; torrents (Prowlarr) altijd laatst.

Elke bron-module heeft:
  NAME, LABEL, enabled() -> bool, search(book) -> list[candidate]
Directe bronnen ook: download(candidate, dest_dir) -> Path
"""
import logging

from . import gutenberg, libgen, prowlarr_src, standardebooks, zlibrary

log = logging.getLogger("ebookarr.sources")

# Prioriteitsvolgorde: gratis/legaal eerst, dan schaduwbibliotheken, dan torrents.
REGISTRY = [gutenberg, standardebooks, libgen, zlibrary, prowlarr_src]

_BY_NAME = {m.NAME: m for m in REGISTRY}


def get(name):
    return _BY_NAME.get(name)


def label(name):
    m = _BY_NAME.get(name)
    return m.LABEL if m else name


def is_torrent(name):
    return getattr(_BY_NAME.get(name), "IS_TORRENT", False)


def enabled_in_order():
    out = []
    for m in REGISTRY:
        try:
            if m.enabled():
                out.append(m)
        except Exception:  # noqa: BLE001
            log.exception("Bron %s: enabled() faalde", m.NAME)
    return out
