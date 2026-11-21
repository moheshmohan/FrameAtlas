import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import signal
import threading
from urllib.parse import urlparse, parse_qs

from .store import Store, DATA
from .service import Service
from .worker import Worker, WorkerBusy

STATIC = Path(__file__).parent / "static"
READ_ACTIONS = {"search", "asset", "coverage", "stats", "jobs", "job", "resolve", "models", "idea_history",
                "picker_sessions", "picker_queued_items", "picker_reselection", "compare_sources",
                "idea_pad", "idea_pad_moments", "metadata_coverage", "people", "person", "people_status"}


def serve(root=DATA, port=7318):
    store = Store(root)
    try:
        worker = Worker(store)
    except WorkerBusy:
        worker = None
    service = Service(store)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            # No filenames, source URLs or search text in HTTP logs.
            pass

        def allowed(self):
            host = self.headers.get("Host", "")
            valid_hosts = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
            if host not in valid_hosts:
                self.reply({"error": "Invalid local host"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + h for h in valid_hosts}:
                self.reply({"error": "Cross-origin access is disabled"}, 403)
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.reply({"error": "Cross-site access is disabled"}, 403)
                return False
            return True

        def reply(self, value, status=200, mime="application/json; charset=utf-8"):
            body = json.dumps(value, ensure_ascii=False).encode() if not isinstance(value, bytes) else value
            self.send_response(status)
            for k, v in {"Content-Type": mime, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                         "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                         "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"}.items():
                self.send_header(k, v)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.allowed():
                return
            try:
                url = urlparse(self.path)
                path = url.path
                if path == "/api/session":
                    self.reply({"csrf": token, "name": "FrameAtlas", "version": "0.1.0"})
                elif path.startswith("/api/"):
                    action = path[5:]
                    if action not in READ_ACTIONS:
                        self.reply({"error": "Use POST for this action"}, 405)
                        return
                    args = {k: v[-1] for k, v in parse_qs(url.query).items()}
                    self.reply(service.dispatch(action, args))
                elif path.startswith("/face/"):
                    name = path[len("/face/"):]
                    if not (name.endswith(".jpg") and name[:-4].isdigit() and len(name) < 24):
                        raise ValueError("Invalid face")
                    p = store.root / "faces" / name
                    if p.is_symlink() or not p.is_file() or p.stat().st_size > 512 * 1024:
                        self.reply({"error": "Face not found"}, 404)
                    else:
                        self.reply(p.read_bytes(), mime="image/jpeg")
                elif path.startswith("/thumb/"):
                    name = path[7:]
                    import re
                    if not re.fullmatch(r"[a-f0-9]{24}\.jpg", name):
                        raise ValueError("Invalid thumbnail")
                    p = store.root / "thumbnails" / name
                    if p.is_symlink() or not p.is_file() or p.stat().st_size > 2 * 1024 ** 2:
                        self.reply({"error": "Preview not found"}, 404)
                    else:
                        self.reply(p.read_bytes(), mime="image/jpeg")
                else:
                    files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"), "/ideapad.js": ("ideapad.js", "text/javascript; charset=utf-8"), "/people.js": ("people.js", "text/javascript; charset=utf-8")}
                    if path not in files:
                        self.reply({"error": "Not found"}, 404)
                        return
                    name, mime = files[path]
                    self.reply((STATIC / name).read_bytes(), mime=mime)
            except (ValueError, TypeError, OSError) as exc:
                self.reply({"error": str(exc)[:1000]}, 400)
            except Exception:
                self.reply({"error": "Request failed safely. Check the local service and try again."}, 500)

        def do_POST(self):
            # A request refused before its body is read leaves that body on a kept-alive connection,
            # where it would be read as the start of the next request: close it instead.
            self.close_connection = True
            if not self.allowed():
                return
            if not secrets.compare_digest(self.headers.get("X-FrameAtlas-Token", ""), token):
                self.reply({"error": "Reload the local app to refresh its session"}, 403)
                return
            try:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("Expected application/json")
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > 8 * 1024 ** 2:
                    raise ValueError("Request exceeds the 8 MiB limit")
                body = self.rfile.read(size)
                self.close_connection = False
                args = json.loads(body)
                path = urlparse(self.path).path
                if not path.startswith("/api/"):
                    raise ValueError("Unknown endpoint")
                result = service.dispatch(path[5:], args)
                self.reply(result)
            except (ValueError, TypeError, OSError) as exc:
                self.reply({"error": str(exc)[:1500]}, 400)
            except Exception as exc:
                self.reply({"error": "Operation failed safely: " + str(exc)[:800]}, 500)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        if worker:
            worker.close()
        raise
    server.daemon_threads = True
    if worker:
        worker.start()
    print("FrameAtlas Local: http://127.0.0.1:%d" % server.server_port, flush=True)
    print("Local metadata: " + str(store.root) + " · no hosted AI", flush=True)
    if not worker:
        print("Using the existing background analysis worker.", flush=True)
    def stop(*unused):
        if worker:
            worker.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.server_close()
        if worker:
            worker.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=7318)
    p.add_argument("--data", type=Path, default=DATA)
    args = p.parse_args()
    try:
        serve(args.data, args.port)
    except (ValueError, OSError) as exc:
        p.exit(1, "FrameAtlas: " + str(exc) + "\n")
