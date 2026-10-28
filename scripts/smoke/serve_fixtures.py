#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Local fixture server for the emulator smoke test (media we generate ourselves).

  serve_fixtures.py <dir> <port>
Serves <dir> plus /slow.bin: an endless, throttled download used for the cancellation test.
"""
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class H(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/slow.bin"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(200 * 1024 * 1024))
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"\0" * 32768)
                    time.sleep(0.1)
            except OSError:
                return
        return super().do_GET()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), partial(H, directory=sys.argv[1])).serve_forever()
