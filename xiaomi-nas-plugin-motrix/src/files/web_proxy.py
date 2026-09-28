#!/usr/bin/env python3
"""Serve the Motrix Web UI through the Xiaomi NAS plugin origin."""

import argparse
import http.client
import http.server
import os
from pathlib import Path
import socketserver
from urllib.parse import urlsplit


HOP_HEADERS = {
    "connection", "content-length", "content-encoding", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "etag", "last-modified",
}


def rewrite_content(body, content_type, prefix):
    if not any(kind in content_type for kind in ("text/html", "javascript", "text/css")):
        return body
    text = body.decode("utf-8")
    text = text.replace("/assets/", prefix + "/assets/")
    text = text.replace('href="/favicon.ico"', f'href="{prefix}/favicon.ico"')
    text = text.replace('href="/apple-touch-icon.png"', f'href="{prefix}/apple-touch-icon.png"')
    if "text/html" in content_type:
        plugin_page = prefix.rsplit("/web", 1)[0] + "/index.html#/"
        return_link = (
            f'<a href="{plugin_page}" onclick="if(location.pathname.endsWith(\'/index.html\'))'
            '{location.reload();return false}" style="position:fixed;right:16px;bottom:16px;'
            'z-index:2147483647;padding:10px 14px;border-radius:8px;'
            'background:#26344a;color:white;text-decoration:none;font:14px sans-serif;'
            'box-shadow:0 2px 12px #0006">返回插件</a>'
        )
        text = text.replace("</body>", return_link + "</body>")
    # Motrix builds its RPC and event URLs from location.origin. Add the proxy
    # prefix so those requests stay inside the NAS client connection.
    text = text.replace(
        "Wc=globalThis.location?.origin??``,",
        f"Wc=globalThis.location?.origin+`{prefix}`??``,",
    )
    return text.encode("utf-8")


def make_handler(prefix, upstream_host, upstream_port, public_origin, token_file):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def address_string(self):
            return "local"

        def do_GET(self):
            self.forward()

        def do_POST(self):
            self.forward()

        def do_PUT(self):
            self.forward()

        def do_PATCH(self):
            self.forward()

        def do_DELETE(self):
            self.forward()

        def forward(self):
            if self.headers.get("Upgrade", "").lower() == "websocket":
                self.send_error(502, "WebSocket must use the Nginx event route")
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length > 64 * 1024 * 1024:
                self.send_error(413)
                return
            payload = self.rfile.read(length) if length else None
            headers = {
                key: value for key, value in self.headers.items()
                if key.lower() not in HOP_HEADERS and key.lower() not in ("host", "cookie", "authorization")
            }
            headers["Host"] = f"{upstream_host}:{upstream_port}"
            headers["Accept-Encoding"] = "identity"
            try:
                operator_token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError:
                self.send_error(503, "Motrix operator token is unavailable")
                return
            if not operator_token:
                self.send_error(503, "Motrix operator token is empty")
                return
            # Nginx admits only authenticated NAS requests to this route.
            # Keep the Motrix token on the NAS; never send it to the browser.
            headers["Authorization"] = f"Bearer {operator_token}"
            # Header names are case-insensitive. Nginx may pass the browser's
            # x-motrix-web-origin in lowercase, including on auth/status.
            for name in headers:
                lower = name.lower()
                if lower in ("origin", "x-motrix-web-origin"):
                    headers[name] = public_origin
                elif lower == "referer":
                    headers[name] = public_origin + "/"

            conn = http.client.HTTPConnection(upstream_host, upstream_port, timeout=60)
            try:
                conn.request(self.command, self.path, body=payload, headers=headers)
                response = conn.getresponse()
                body = response.read()
                content_type = response.getheader("Content-Type", "")
                body = rewrite_content(body, content_type, prefix)
                self.send_response(response.status)
                for key, value in response.getheaders():
                    lower = key.lower()
                    if lower in HOP_HEADERS or lower in ("cache-control", "set-cookie"):
                        continue
                    if lower == "location":
                        if value.startswith(public_origin):
                            value = prefix + value[len(public_origin):]
                        elif value.startswith("/"):
                            value = prefix + value
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (OSError, http.client.HTTPException) as error:
                self.send_error(502, str(error))
            finally:
                conn.close()

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9305)
    parser.add_argument("--unix-socket")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:8080")
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--token-file", default="/data/motrix-server/data/operator-token")
    args = parser.parse_args()
    upstream = urlsplit(args.upstream)
    handler = make_handler(args.prefix.rstrip("/"), upstream.hostname, upstream.port, args.public_origin.rstrip("/"), args.token_file)
    if args.unix_socket:
        class LocalHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        socket_path = Path(args.unix_socket)
        socket_path.unlink(missing_ok=True)
        with LocalHTTPServer(str(socket_path), handler) as server:
            os.chmod(socket_path, 0o600)
            server.serve_forever()
    else:
        http.server.ThreadingHTTPServer((args.listen, args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
