#!/usr/bin/env python3
"""
Speed-test target for the dashboard's Network Speed Test panel.

Deliberately separate from rosbridge/ROS entirely - see README "Connecting
to the real robot": network connectivity and robot telemetry are different
things, tested independently. This is why it's started unconditionally
from entrypoint.sh (below) rather than from start_ros_stack.sh - you should
be able to test whether the network path to the robot works even before
anyone has SSHed in and started the ROS stack, same as you'd want to be
able to ping/reach a real robot before its software is up.

Deliberately stdlib-only (http.server + socketserver) - no extra Docker
image dependencies for something this small. Not meant to be a general-
purpose web server: three endpoints only, matching exactly what the
dashboard backend's /api/speedtest/* routes need (see backend/main.py).

    GET  /ping             -> tiny fixed response, for round-trip timing
    GET  /down?bytes=N     -> streams exactly N bytes (capped, see below)
    POST /up               -> reads and discards the request body,
                               returns how many bytes were received

Threaded (ThreadingHTTPServer) so a slow/large transfer on one endpoint
doesn't block a concurrent ping. No auth, no HTTPS, no CORS headers - none
needed, since only the dashboard backend talks to this directly (browser
never does - see README/the "browser vs backend-proxied" discussion this
was built around). Not something to expose beyond a trusted local/robot
network.
"""
import json
import os
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("SPEEDTEST_PORT", "8091"))

# Hard cap regardless of what the caller requests, so a misbehaving/hostile
# client can't ask for an unbounded download and tie up the robot's own
# network link indefinitely. The dashboard backend never asks for more
# than ~20MB today; this is a generous ceiling above that, not a tuned
# limit.
MAX_DOWNLOAD_BYTES = 200_000_000
CHUNK = 65536


class Handler(BaseHTTPRequestHandler):
    # Quiet by default - the dashboard may run many of these per test;
    # default BaseHTTPRequestHandler logs one line per request to stderr,
    # which is just noise for a speed test loop.
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/ping":
            body = b"pong"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/down":
            qs = parse_qs(parsed.query)
            try:
                n = int(qs.get("bytes", ["1000000"])[0])
            except ValueError:
                n = 1_000_000
            n = max(0, min(n, MAX_DOWNLOAD_BYTES))

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(n))
            self.end_headers()
            remaining = n
            # Reuse one chunk of random bytes rather than calling
            # os.urandom per-chunk - the randomness itself isn't the
            # point, just moving real bytes over the wire is.
            block = os.urandom(min(CHUNK, n)) if n else b""
            while remaining > 0:
                to_write = block[: min(CHUNK, remaining)]
                try:
                    self.wfile.write(to_write)
                except (BrokenPipeError, ConnectionResetError):
                    # Client (backend) gave up early - e.g. it hit its own
                    # 5s cap mid-stream. Not an error on this end.
                    return
                remaining -= len(to_write)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/up":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        received = 0
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(CHUNK, remaining))
            if not chunk:
                break
            received += len(chunk)
            remaining -= len(chunk)

        body = json.dumps({"received": received}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    with Server(("0.0.0.0", PORT), Handler) as httpd:
        print(f"[speedtest_server] listening on :{PORT} (/ping, /down, /up)", flush=True)
        httpd.serve_forever()
