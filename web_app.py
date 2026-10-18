"""Command-line entry point for the local RenAlly web application."""
import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.web.http_server import make_handler
from src.web.service import RenAllyWebService, parse_budgets

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
__all__ = ["RenAllyWebService", "WEB_DIR", "make_handler", "parse_budgets", "main"]


def main():
    parser = argparse.ArgumentParser(description="Run the RenAlly local chatbot page")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")

    service = RenAllyWebService(ROOT / "data")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(service))
    print(f"Project directory: {ROOT}")
    print(f"RenAlly local chat is available at http://127.0.0.1:{args.port}")
    print("Press Ctrl+C to stop. Requests stay on this computer.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping RenAlly local chat.")
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
