"""EbookArr als achtergrond-app met systeemtray-icoon.

Start de webserver in een aparte thread en toont een tray-icoon met:
  - Open EbookArr   (opent http://localhost:8686 in je browser)
  - Afsluiten       (stopt de server en sluit af)

Wordt via de Windows Taakplanner bij het aanmelden gestart met pythonw.exe,
dus zonder zichtbaar venster. Zie install-task.bat.
"""
import logging
import socket
import threading
import webbrowser

import uvicorn

from . import __version__, logsetup

logsetup.configure()
log = logging.getLogger("ebookarr.tray")

PORT = 8686
URL = f"http://localhost:{PORT}"


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _make_icon():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([12, 8, 52, 56], radius=4, fill=(91, 140, 255, 255))
    d.rectangle([12, 8, 21, 56], fill=(58, 88, 170, 255))
    for y in (20, 30, 40):
        d.line([27, y, 46, y], fill=(255, 255, 255, 230), width=3)
    return img


def main():
    if _port_in_use(PORT):
        log.info("EbookArr draait al op poort %s — browser openen.", PORT)
        webbrowser.open(URL)
        return

    config = uvicorn.Config(
        "app.main:app", host="0.0.0.0", port=PORT, log_config=None
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # niet in de main-thread

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    log.info("EbookArr tray gestart (versie %s) op %s", __version__, URL)

    import pystray

    def on_open(icon=None, item=None):
        webbrowser.open(URL)

    def on_quit(icon, item):
        log.info("Afsluiten via tray-menu")
        server.should_exit = True
        icon.stop()

    icon = pystray.Icon(
        "EbookArr",
        _make_icon(),
        f"EbookArr {__version__}",
        menu=pystray.Menu(
            pystray.MenuItem("Open EbookArr", on_open, default=True),
            pystray.MenuItem("Afsluiten", on_quit),
        ),
    )
    icon.run()

    # Tray is gestopt: server netjes laten uitlopen.
    server.should_exit = True
    thread.join(timeout=6)
    log.info("EbookArr afgesloten.")


if __name__ == "__main__":
    main()
