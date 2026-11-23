"""Downloadbronnen. Vaste prioriteitsvolgorde.

Elke bron-module heeft:
  NAME, LABEL, enabled() -> bool, search(book) -> list[candidate]
Directe bronnen ook: download(candidate, dest_dir) -> Path
"""
import logging

from . import gutenberg, libgen, standardebooks, zlibrary

log = logging.getLogger("ebookarr.sources")

# Prioriteitsvolgorde: gratis/legaal eerst, dan schaduwbibliotheken.
REGISTRY = [gutenberg, standardebooks, libgen, zlibrary]

_BY_NAME = {m.NAME: m for m in REGISTRY}


def get(name):
    return _BY_NAME.get(name)


def label(name):
    m = _BY_NAME.get(name)
    return m.LABEL if m else name


def enabled_in_order():
    out = []
    for m in REGISTRY:
        try:
            if m.enabled():
                out.append(m)
        except Exception:  # noqa: BLE001
            log.exception("Bron %s: enabled() faalde", m.NAME)
    return out
