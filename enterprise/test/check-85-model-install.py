"""
The gateway's one-button model install (BEGIN MODEL INSTALLS in hub.py),
end to end against a local model source: the refusals a hostile or clumsy
request must meet, and the download path a legitimate one takes — streamed,
format-checked at byte nine, renamed into place atomically, reported
truthfully over /api/models/installs.

A dedicated gateway (the check-15 pattern: deterministic thresholds this
file chooses, not production defaults nobody here controls) runs with
MODELS_ROOT in a temp dir, the allowlist pointed at loopback, the http
escape hatch on (a self-signed-TLS stand-in would test OpenSSL, not this
feature), and a 4 MB byte cap so the over-limit path resolves in
milliseconds instead of thirty gigabytes.

The model source serves three files:

  /good.safetensors  a real safetensors shape — 8-byte little-endian header
                     length, the JSON it promises, then padding
  /fake.safetensors  a zip magic (a torch .ckpt renamed) — must die at byte
                     nine with the .part cleaned up
  /big.safetensors   valid header, but the body walks past the byte cap
                     with no Content-Length to warn the gateway up front —
                     must die on the streamed count, .part cleaned up
"""
import http.server
import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from harness import check, failures

sys.stdout.reconfigure(line_buffering=True)

DGW_PORT = 8103
DGW = f"http://127.0.0.1:{DGW_PORT}"
SRC_PORT = 8104
CAP = 4 * 1024 * 1024

GOOD_BODY = struct.pack("<Q", 2) + b"{}" + b"\0" * (2 * 1024 * 1024)
FAKE_BODY = b"PK\x03\x04" + b"\0" * 1024
BIG_CHUNK = struct.pack("<Q", 2) + b"{}" + b"\0" * (1024 * 1024)


class ModelSource(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/good.safetensors":
            self.send_response(200)
            self.send_header("Content-Length", str(len(GOOD_BODY)))
            self.end_headers()
            self.wfile.write(GOOD_BODY)
        elif self.path == "/fake.safetensors":
            self.send_response(200)
            self.send_header("Content-Length", str(len(FAKE_BODY)))
            self.end_headers()
            self.wfile.write(FAKE_BODY)
        elif self.path == "/big.safetensors":
            # Chunked, no Content-Length: the gateway cannot refuse up front
            # and must enforce the cap on the counted stream.
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for _ in range(8):  # 8 MB+ against a 4 MB cap
                    self.wfile.write(f"{len(BIG_CHUNK):x}\r\n".encode())
                    self.wfile.write(BIG_CHUNK + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except BrokenPipeError:
                # The gateway hanging up mid-stream IS the byte cap working;
                # without this clause the pass prints a traceback.
                pass
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):
        pass


source = http.server.ThreadingHTTPServer(("127.0.0.1", SRC_PORT), ModelSource)
threading.Thread(target=source.serve_forever, daemon=True).start()

models_root = pathlib.Path(tempfile.mkdtemp(prefix="model-install-"))

env = dict(os.environ)
env["MODELS_ROOT"] = str(models_root)
env["MODEL_INSTALL_ALLOWED_HOSTS"] = "127.0.0.1"
env["MODEL_INSTALL_ALLOW_HTTP"] = "1"
env["MODEL_INSTALL_MAX_BYTES"] = str(CAP)
dgw_log = open("model-install-gateway.log", "w")
dgw = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "hub:app", "--host", "127.0.0.1",
     "--port", str(DGW_PORT), "--log-level", "warning"],
    env=env, stdout=dgw_log, stderr=subprocess.STDOUT)

up = False
deadline = time.time() + 30
while time.time() < deadline and dgw.poll() is None:
    try:
        if urllib.request.urlopen(DGW + "/healthz", timeout=2).getcode() == 200:
            up = True
            break
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)

check("the dedicated gateway came up", up, "" if up else open("model-install-gateway.log").read()[-2000:])
if not up:
    print("1 FAILED: ['dedicated gateway did not start']")
    sys.exit(1)


def post(body, content_type="application/json"):
    request = urllib.request.Request(
        DGW + "/api/models/install",
        data=json.dumps(body).encode() if isinstance(body, dict) else body,
        headers={"Content-Type": content_type}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.getcode(), json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def installs():
    with urllib.request.urlopen(DGW + "/api/models/installs", timeout=10) as response:
        return {f"{m['folder']}/{m['filename']}": m for m in json.loads(response.read())["installs"]}


SRC = f"http://127.0.0.1:{SRC_PORT}"

try:
    print("\n== the refusals: source, folder, filename, format, media type")

    code, body = post({"url": "http://127.0.0.2:1/m.safetensors"})
    check("a host off the allowlist is a 400 naming the allowlist",
          code == 400 and "allowlist" in body.get("detail", ""), f"{code} {body}")

    code, body = post({"url": SRC + "/good.safetensors", "folder": "../output"})
    check("a folder outside the enum is a 400", code == 400, f"{code} {body}")

    code, body = post({"url": SRC + "/good.safetensors", "filename": "../up.safetensors"})
    check("a traversal filename is a 400", code == 400, f"{code} {body}")

    code, body = post({"url": SRC + "/model.ckpt"})
    check("a non-safetensors name is a 400", code == 400, f"{code} {body}")

    code, body = post({"url": SRC + "/good.safetensors"}, content_type="text/plain")
    check("a cross-site-shaped text/plain post is a 415", code == 415, f"{code} {body}")

    print("\n== the download: streamed in, verified at byte nine, renamed into place")

    code, body = post({"url": SRC + "/good.safetensors"})
    check("a legitimate install is admitted", code == 200 and body["status"] == "downloading", f"{code} {body}")

    target = models_root / "checkpoints" / "good.safetensors"
    deadline = time.time() + 30
    state = {}
    while time.time() < deadline:
        state = installs().get("checkpoints/good.safetensors", {})
        if state.get("status") in ("done", "error"):
            break
        time.sleep(0.3)

    check("the install reports done", state.get("status") == "done", str(state))
    check("the file landed with every byte",
          target.exists() and target.stat().st_size == len(GOOD_BODY),
          f"exists={target.exists()}")
    check("no .part is left behind",
          not list((models_root / "checkpoints").glob(".*part")),
          str(list((models_root / "checkpoints").iterdir())))

    code, body = post({"url": SRC + "/good.safetensors"})
    check("a repeat install says already_present without re-downloading",
          code == 200 and body.get("already_present") is True, f"{code} {body}")

    print("\n== the failures a report must survive: wrong format, unbounded body")

    code, body = post({"url": SRC + "/fake.safetensors"})
    check("the renamed pickle is admitted by name", code == 200, f"{code} {body}")

    deadline = time.time() + 30
    while time.time() < deadline:
        state = installs().get("checkpoints/fake.safetensors", {})
        if state.get("status") in ("done", "error"):
            break
        time.sleep(0.3)

    check("…and dies at byte nine with the header named",
          state.get("status") == "error" and "header" in state.get("error", ""), str(state))
    check("…leaving neither the file nor a .part",
          not (models_root / "checkpoints" / "fake.safetensors").exists()
          and not list((models_root / "checkpoints").glob(".*part")),
          str(list((models_root / "checkpoints").iterdir())))

    code, body = post({"url": SRC + "/big.safetensors"})
    check("the unbounded body is admitted by shape", code == 200, f"{code} {body}")

    deadline = time.time() + 30
    while time.time() < deadline:
        state = installs().get("checkpoints/big.safetensors", {})
        if state.get("status") in ("done", "error"):
            break
        time.sleep(0.3)

    check("…and is cut off at the byte cap",
          state.get("status") == "error" and "limit" in state.get("error", ""), str(state))
    check("…leaving no partial file",
          not (models_root / "checkpoints" / "big.safetensors").exists()
          and not list((models_root / "checkpoints").glob(".*part")),
          str(list((models_root / "checkpoints").iterdir())))
finally:
    dgw.terminate()
    dgw.wait(timeout=10)
    source.shutdown()

print(f"\n{len(failures)} FAILED: {failures}" if failures else "\nALL PASSED")
sys.exit(1 if failures else 0)
