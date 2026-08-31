#!/usr/bin/env python3
"""ego-bridge: expose the local `ego-browser` CLI to a trusted LAN host over HTTP.

The pi agent on the VM runs a shim also named `ego-browser`; the shim forwards
`ego-browser nodejs <script>` here, this process runs the real macOS binary
against the running ego lite app, and returns stdout/stderr/exit code plus any
screenshot files the script produced and any files the browser downloaded.

Security: a request that reaches /run executes arbitrary JavaScript in ego
lite's Node runtime on this Mac. Access is gated by a bearer token AND a source
IP allowlist. Do not expose this port beyond the trusted LAN host.
"""
import hmac
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
# Self-contained: token, config and uploads live next to this script.
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
TOKEN_PATH = os.path.join(BASE, "token")

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8791,
    # Loopback only by default. Add the agent host's IP in config.json.
    "allow_ips": ["127.0.0.1", "::1"],
    "ego_browser": os.path.join(HOME, ".local/bin/ego-browser"),
    "timeout": 300,
    "max_parallel": 4,
    "allowed_subcommands": ["nodejs", "help", "--help", "-h", "--version", "-v"],
    # Directories watched for browser downloads. Empty means ~/Downloads,
    # which is where ego lite saves unless the user changed it in the browser.
    "watch_dirs": [],
    # Seconds to keep waiting after a script ends for a download to *appear*.
    # A click returns long before Chromium writes the file (measured ~3s for a
    # small one), so without this the file is only reported on the next call.
    # Interruptible: as soon as something shows up the wait ends. Set 0 to skip.
    "download_grace": 4,
    # Seconds to keep waiting once a partial download (.crdownload) is being
    # written, so half a file is never reported as complete.
    "download_settle": 20,
    # Files larger than this are reported but not shipped automatically.
    "max_download_bytes": 100 * 1024 * 1024,
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as fh:
            cfg.update(json.load(fh))
    return cfg


CFG = load_config()
with open(TOKEN_PATH) as fh:
    TOKEN = fh.read().strip()

ALLOW_NETS = [ipaddress.ip_network(a, strict=False) for a in CFG["allow_ips"]]
SLOTS = threading.Semaphore(CFG["max_parallel"])

WATCH_DIRS = [
    os.path.realpath(os.path.expanduser(p))
    for p in (CFG["watch_dirs"] or [os.path.join(HOME, "Downloads")])
]
PARTIAL_SUFFIXES = (".crdownload", ".part", ".download", ".tmp", ".partial")

# Files the bridge is willing to hand back to the agent host. Screenshots land
# in the per-user temp dir; downloads in the watched dirs; uploads pushed from
# the agent host get their own tree.
UPLOAD_DIR = os.path.join(BASE, "uploads")
_tmp = os.path.realpath(os.environ.get("TMPDIR", "/tmp"))
FILE_ROOTS = [
    os.path.realpath(p)
    for p in set([_tmp, "/tmp", "/private/tmp", UPLOAD_DIR] + WATCH_DIRS)
]
ARTIFACT_RE = re.compile(r"(/[^\s\"'`,;:()\[\]<>]+\.(?:png|jpe?g|webp|gif|pdf))")

# Only files touched after this point are treated as the agent's downloads, so
# a fresh bridge never sweeps up the user's existing Downloads folder.
_watermark_lock = threading.Lock()
_watermark = time.time()


def log(msg):
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


def path_allowed(path):
    rp = os.path.realpath(path)
    for root in FILE_ROOTS:
        if rp == root or rp.startswith(root + os.sep):
            return rp
    return None


def find_artifacts(text):
    out = []
    seen = set()
    for m in ARTIFACT_RE.finditer(text or ""):
        raw = m.group(1)
        rp = path_allowed(raw)
        if rp and os.path.isfile(rp) and raw not in seen:
            seen.add(raw)
            out.append({"path": raw, "size": os.path.getsize(rp)})
    return out


def scan_downloads(since):
    """Regular files in the watched dirs touched after `since`.

    Returns (files, partial_in_progress). Directories and dotfiles are skipped;
    a still-downloading `.crdownload` counts only as "wait a bit longer".
    """
    files = []
    partial = False
    for directory in WATCH_DIRS:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if name.startswith("."):
                continue
            path = os.path.join(directory, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_mtime <= since:
                continue
            if name.endswith(PARTIAL_SUFFIXES):
                partial = True
                continue
            files.append(
                {
                    "path": path,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "too_large": st.st_size > CFG["max_download_bytes"],
                }
            )
    return files, partial


def collect_downloads():
    """Downloads that appeared since the last time we reported any.

    Waits out an in-flight download rather than reporting half a file. The
    watermark is taken *before* the scan so a file written during the scan is
    picked up by the next run instead of being lost.
    """
    global _watermark
    with _watermark_lock:
        since = _watermark
    now = time.time()
    grace_until = now + float(CFG["download_grace"])
    settle_until = now + float(CFG["download_settle"])
    while True:
        scan_ts = time.time()
        files, partial = scan_downloads(since)
        if partial:
            # Something is mid-write. Wait for it rather than ship half a file.
            if scan_ts >= settle_until:
                break
            time.sleep(0.5)
            continue
        if files or scan_ts >= grace_until:
            break
        time.sleep(0.25)
    with _watermark_lock:
        if scan_ts > _watermark:
            _watermark = scan_ts
    if files:
        log("downloads: %s" % ", ".join(os.path.basename(f["path"]) for f in files))
    return files


class Handler(BaseHTTPRequestHandler):
    server_version = "ego-bridge/1.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log("%s %s" % (self.client_address[0], fmt % args))

    # --- helpers -----------------------------------------------------
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1024 * 1024)

    def _authorized(self):
        ip = ipaddress.ip_address(self.client_address[0])
        if not any(ip in net for net in ALLOW_NETS):
            self._send(403, {"error": "source ip not allowed: %s" % ip})
            return False
        auth = self.headers.get("Authorization", "")
        given = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not hmac.compare_digest(given, TOKEN):
            self._send(401, {"error": "bad or missing bearer token"})
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # --- routes ------------------------------------------------------
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/health":
            if not self._authorized():
                return
            return self._send(
                200,
                {
                    "ok": True,
                    "ego_browser": CFG["ego_browser"],
                    "watch_dirs": WATCH_DIRS,
                },
            )
        if url.path == "/file":
            if not self._authorized():
                return
            q = urllib.parse.parse_qs(url.query)
            raw = (q.get("path") or [""])[0]
            rp = path_allowed(raw)
            if not rp or not os.path.isfile(rp):
                return self._send(404, {"error": "no such readable file: %s" % raw})
            return self._send_file(rp)
        self._send(404, {"error": "unknown path"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if not self._authorized():
            return
        if url.path == "/upload":
            q = urllib.parse.parse_qs(url.query)
            name = os.path.basename((q.get("name") or ["upload.bin"])[0]) or "upload.bin"
            dest = os.path.join(UPLOAD_DIR, "%d-%s" % (int(time.time() * 1000), name))
            with open(dest, "wb") as fh:
                fh.write(self._body())
            return self._send(200, {"path": dest})
        if url.path != "/run":
            return self._send(404, {"error": "unknown path"})

        try:
            req = json.loads(self._body() or b"{}")
        except ValueError as exc:
            return self._send(400, {"error": "bad json: %s" % exc})

        args = [str(a) for a in (req.get("args") or [])]
        script = req.get("stdin") or ""
        timeout = float(req.get("timeout") or CFG["timeout"])

        head = [a for a in args if not a.startswith("--ego-server-name")]
        if not head or head[0] not in CFG["allowed_subcommands"]:
            return self._send(
                403,
                {"error": "subcommand not allowed: %s" % (head[0] if head else "<none>")},
            )

        cmd = [CFG["ego_browser"]] + args
        log("run: %s (stdin %d bytes)" % (" ".join(shlex.quote(c) for c in cmd), len(script)))
        with SLOTS:
            try:
                proc = subprocess.run(
                    cmd,
                    input=script.encode(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                )
                code, out, err = proc.returncode, proc.stdout, proc.stderr
            except subprocess.TimeoutExpired:
                return self._send(504, {"error": "ego-browser timed out after %ss" % timeout})
            except OSError as exc:
                return self._send(500, {"error": "cannot exec ego-browser: %s" % exc})

        out_s = out.decode("utf-8", "replace")
        err_s = err.decode("utf-8", "replace")
        self._send(
            200,
            {
                "code": code,
                "stdout": out_s,
                "stderr": err_s,
                "artifacts": find_artifacts(out_s + "\n" + err_s),
                "downloads": collect_downloads() if head[0] == "nodejs" else [],
            },
        )


def main():
    srv = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    srv.daemon_threads = True
    log("ego-bridge listening on %s:%s, allow=%s" % (CFG["host"], CFG["port"], CFG["allow_ips"]))
    log("watching for downloads in: %s" % ", ".join(WATCH_DIRS))
    srv.serve_forever()


if __name__ == "__main__":
    main()
