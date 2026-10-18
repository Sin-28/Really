"""HTTP transport and static asset serving for the local web interface."""
import json
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

WEB_DIR = Path(__file__).resolve().parents[2] / "web"


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type):
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            # This is a local development app: always fetch the current Python
            # and JavaScript-backed UI instead of reusing assets from another
            # copy that happened to serve the same localhost port earlier.
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            parsed = urlsplit(self.path)
            resources = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/graph": ("graph.html", "text/html; charset=utf-8"),
                "/graph/": ("graph.html", "text/html; charset=utf-8"),
                "/graph.html": ("graph.html", "text/html; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/graph.css": ("graph.css", "text/css; charset=utf-8"),
                "/graph.js": ("graph.js", "text/javascript; charset=utf-8"),
            }
            if self.path == "/api/health":
                body = json.dumps({
                    "status": "ok",
                    "project_root": str(Path(__file__).resolve().parents[2]),
                })
                self._send(200, body, "application/json; charset=utf-8")
                return
            if self.path == "/api/graph":
                nodes = []
                for node_id, raw in service.kg.graph["nodes"].items():
                    kind = raw.get("kind")
                    if kind not in {"recipe", "ingredient", "ingredient_type"}:
                        continue
                    nodes.append({
                        "id": node_id,
                        "kind": kind,
                        "name": raw.get("name", node_id),
                        "description": raw.get("description", raw.get("fdc_description", "")),
                        "aliases": raw.get("aliases", []),
                        "fdc_id": raw.get("source", {}).get("fdc_id"),
                        "ingredient_type": raw.get("ingredient_type"),
                        "substitution_group": raw.get("substitution_group"),
                    })
                node_ids = {node["id"] for node in nodes}
                edges = [edge for edge in service.kg.graph["edges"]
                         if edge.get("source") in node_ids and edge.get("target") in node_ids]
                body = json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False)
                self._send(200, body, "application/json; charset=utf-8")
                return
            resource = resources.get(parsed.path)
            if resource is None:
                self._send(404, "Not found", "text/plain; charset=utf-8")
                return
            filename, content_type = resource
            self._send(200, (WEB_DIR / filename).read_text(), content_type)

        def do_POST(self):
            if self.path not in {
                "/api/chat",
            }:
                self._send(404, json.dumps({"error": "Not found"}), "application/json; charset=utf-8")
                return
            try:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise ValueError("Invalid Content-Length header.") from None
                if length < 0:
                    raise ValueError("Content-Length must be non-negative.")
                if length > 64_000:
                    raise ValueError("Request is too large.")
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be a JSON object.")
                if self.path == "/api/chat":
                    response = service.chat(payload)
                self._send(200, json.dumps(response, ensure_ascii=False), "application/json; charset=utf-8")
            except (ValueError, json.JSONDecodeError) as exc:
                self._send(400, json.dumps({"error": str(exc)}), "application/json; charset=utf-8")
            except Exception:
                # Do not leak local paths, model details, or tracebacks into the chat.
                traceback.print_exc()
                message = "The prototype couldn't complete that request. Please try a supported dish or check the local data files."
                self._send(500, json.dumps({"error": message}), "application/json; charset=utf-8")

        def log_message(self, _format, *_args):
            return

    return Handler
