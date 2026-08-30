#!/usr/bin/env python3
import argparse
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class PortalHandler(BaseHTTPRequestHandler):
    app_url = ""
    ssid = ""
    ca_cert: Path | None = None

    def do_GET(self) -> None:
        if self.path == "/local-ca.cert.crt" and self.ca_cert and self.ca_cert.is_file():
            content = self.ca_cert.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-x509-ca-cert")
            self.send_header("Content-Disposition", 'attachment; filename="multicam-local-ca.crt"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        ca_link = '<a class="secondary" href="/local-ca.cert.crt">Stáhnout lokální CA certifikát</a>' if self.ca_cert else ""
        content = f"""<!doctype html>
<html lang="cs"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MultiCam</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;font:16px system-ui;background:#0b1120;color:#e8edf5}}
main{{width:min(520px,calc(100% - 32px));padding:28px;border:1px solid #405170;border-radius:18px;background:#121b2e}}
h1{{margin-top:0}}a{{display:block;margin-top:14px;padding:14px;text-align:center;border-radius:10px;background:#7dd3fc;color:#07101d;font-weight:800;text-decoration:none}}
a.secondary{{color:#e8edf5;border:1px solid #405170;background:transparent}}small{{color:#9caac0}}
</style><main><small>síť {self.ssid}</small><h1>MultiCam</h1>
<p>Jste připojeni k ostrovní síti bez internetu. Pokračujte do lokální aplikace.</p>
<a href="{self.app_url}">Otevřít ovládací stránku</a>{ca_link}
<p><small>Pokud prohlížeč varuje před certifikátem, nainstalujte nejprve lokální CA a poté stránku otevřete znovu.</small></p>
</main></html>""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="10.42.0.1")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--app-url", required=True)
    parser.add_argument("--ssid", required=True)
    parser.add_argument("--ca-cert", type=Path)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    args = parser.parse_args()
    PortalHandler.app_url = args.app_url
    PortalHandler.ssid = args.ssid
    PortalHandler.ca_cert = args.ca_cert if args.ca_cert and args.ca_cert.is_file() else None
    server = ThreadingHTTPServer((args.bind, args.port), PortalHandler)
    if os.geteuid() == 0 and args.gid is not None and args.uid is not None:
        os.setgroups([])
        os.setgid(args.gid)
        os.setuid(args.uid)
    server.serve_forever()


if __name__ == "__main__":
    main()
