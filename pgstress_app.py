"""
pgstress desktop app: runs the pgstress UI server in the background and shows it in a native
window (Microsoft Edge WebView2). This is the entry point PyInstaller turns into pgstress.exe.

If the native window can't be created, it opens the default browser instead.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import webbrowser
from pathlib import Path

import pgstress_ui

# Fixed port range keeps the page origin stable, so the saved connection string and query survive restarts.
PREFERRED_PORT = 8765
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "pgstress"


def show_error(message: str):
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, "pgstress", 0x10)
    else:
        print(message, file=sys.stderr)


def main():
    try:
        server, url = pgstress_ui.start_server(PREFERRED_PORT)
    except OSError as e:
        show_error(f"Could not start the local server:\n{e}")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        import webview
    except ImportError:
        webview = None

    if webview is None:
        webbrowser.open(url)
        show_error(f"pgstress is running in your browser at {url}.\n\nClose this box to stop it.")
        server.shutdown()
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    webview.create_window("pgstress", url, width=1440, height=900, min_size=(960, 620),
                          background_color="#1e1e1e", maximized=True)
    try:
        # private_mode=False + storage_path keeps localStorage (saved DSN, query, options) between launches
        webview.start(private_mode=False, storage_path=str(DATA_DIR))
    except Exception as e:  # WebView2 runtime missing or broken: fall back to the browser
        webbrowser.open(url)
        show_error(f"The app window could not open ({e}).\n\npgstress is running in your browser at {url}.\n"
                   f"Close this box to stop it.")
    server.shutdown()


if __name__ == "__main__":
    main()
