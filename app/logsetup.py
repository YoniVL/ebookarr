"""Logging naar console én naar een roterend logbestand (ebookarr.log).

Belangrijk zodra de app headless via de Taakplanner draait: dan is het
logbestand de enige manier om te zien wat er gebeurt.
"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "ebookarr.log"

_configured = False


def configure():
    global _configured
    if _configured:
        return
    _configured = True

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    # Onze eigen loggers wat spraakzamer
    logging.getLogger("ebookarr").setLevel(logging.INFO)
    # Ruis dempen
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
