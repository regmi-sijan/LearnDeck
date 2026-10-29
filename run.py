#!/usr/bin/env python3
"""Start LearnDeck:  python run.py   (then open http://127.0.0.1:8765)"""
import argparse
import sys
import threading
import webbrowser

if sys.version_info < (3, 9):
    sys.exit("LearnDeck needs Python 3.9 or newer (3.11+ recommended).")

from app import config  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Turn documents, code and links into drill-down lessons.")
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    ap.add_argument("--reload", action="store_true", help="auto-reload on code changes (for development)")
    args = ap.parse_args()

    try:
        import uvicorn
    except ImportError:
        sys.exit("Dependencies are missing. Run:  pip install -r requirements.txt")

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"\n  LearnDeck is running at {url}\n  Press Ctrl+C to stop.\n")
    if config.OPEN_BROWSER and not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.server:app", host=args.host, port=args.port, reload=args.reload, log_level="warning")


if __name__ == "__main__":
    main()
