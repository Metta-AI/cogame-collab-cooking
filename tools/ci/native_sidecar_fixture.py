"""Run the real prompt player against diagnostic loopback HTTP, without credentials."""

import http.server
import json
import threading

from collab_cooking.coworld.player import main


class Fixture(http.server.BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/v1/messages"
        assert request["model"] == "diagnostic-http-fixture"
        Fixture.calls += 1
        response = json.dumps(
            {
                "model": "diagnostic-http-fixture",
                "content": [{"type": "text", "text": '{"station":"hold","say":"fixture","note":"diagnostic only"}'}],
                "usage": {"input_tokens": 11, "output_tokens": 19},
                "stop_reason": "end_turn",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response)


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 18772), Fixture)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        main()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert Fixture.calls > 0, "The prompt player made no diagnostic HTTP requests"
    print(json.dumps({"diagnostic_http_requests": Fixture.calls}))
