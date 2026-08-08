"""Webhook 本地接收端（真机验证用）- 监听 8899，把飞书/钉钉格式 POST 记录到文件

用法: python scripts/webhook_receiver.py <logfile> [port]
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    logfile = "webhook_recv.log"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "path": self.path,
            "body": json.loads(body) if body else {},
        }
        with open(self.logfile, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        resp = b'{"code":0,"msg":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8899
    Handler.logfile = sys.argv[1] if len(sys.argv) > 1 else "webhook_recv.log"
    print(f"webhook receiver listening :{port} -> {Handler.logfile}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
