"""
Q3 FIX 4a — the reported OUTPUT FILENAME is confined too, not just the
subfolder (docs/10-roadmap.md).

check-60-user-workspaces.py proves output_subfolder() confines `subfolder`.
It cannot prove anything about `filename`, because fake_comfy.py's stub
manifest is the fixed literal {out_0001.png, ""} for every job in that file
-- there is no way to ask it to report a hostile filename. This file adds
exactly that hook (fake_comfy.py's /__set_next_output__, one-shot, added in
the same commit) and uses it to reproduce, then close, the gap an adversarial
gate found on this branch:

    output_subfolder(workspace, subfolder="", filename="../../OUTSIDE/secret.txt")

confines `subfolder` correctly (it comes back as the caller's own workspace),
but before this fix collect_outputs() then built the served URL by
concatenating the RAW, unconfined `filename` onto that safe subfolder --
    f"/outputs/{subfolder}/{filename}"
-- so a subfolder-only check never saw the escape: it lived one level up, in
how the URL's two halves were put together, not in what output_subfolder()
itself returned. No file ever escaped OUTPUT_ROOT (nothing here writes
outside it) and hub.py's /outputs endpoint independently refuses to SERVE
such a URL regardless -- this was a gap in depth, confirmed by the gate off
this file's own history, not a live breach. It is closed by making
output_subfolder() confine `filename` on the same footing as `subfolder`:
a `filename` that is not already a single bare path component (no `/`, no
`\\`, no NUL, not `.` or `..`) is refused outright -- collect_outputs() never
builds a URL for that output at all, rather than serving it from wherever a
naive join would resolve to.

(a) reproduces the gate's finding first, against the real running worker
    agent (not a reimplementation of its sanitizer): submits one ordinary job
    to learn the real, live confined workspace prefix the agent computed for
    a submitter, then builds the exact pre-fix URL shape by hand -- the
    confined subfolder with the hostile filename concatenated onto it -- and
    confirms THAT escapes OUTPUT_ROOT. This is the finding, demonstrated
    with the system's own output, before the fix is checked at all.
(b) proves the fix end-to-end: a job whose stubbed manifest entry carries
    that same hostile filename still completes, and never produces an image
    entry in the browser-visible manifest at all -- the hostile output is
    dropped, not served from a rewritten or truncated name.
(c) proves the fix is not over-sanitization: an ordinary filename with
    embedded parentheses -- the kind ComfyUI's own batch-counter
    naming produces -- still round-trips to a real, confined, servable URL
    exactly like check-60's workspace_ok() expects for a legitimate
    username.
(d) proves a filename that merely CONTAINS a separator without spelling
    ".." (e.g. a node hardcoding "sub/evil.png") is refused the same way --
    subfolder is the only field ComfyUI's own save nodes use to nest, so a
    slash inside filename itself is never legitimate.
"""
import json, os, pathlib, sys, urllib.request
import websocket

GW = "http://127.0.0.1:8100"
COMFY = "http://127.0.0.1:8999"
OUTPUT_ROOT = pathlib.Path(os.environ["OUTPUT_ROOT"]).resolve()
failures = []



def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + str(detail) if detail else ""))
    if not cond:
        failures.append(name)


def seed_output():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "out_0001.png").write_bytes(b"fake png bytes")


def drain(job_id, timeout=15):
    ws = websocket.WebSocket()
    ws.connect(f"ws://127.0.0.1:8100/ws/{job_id}", timeout=10)
    ws.settimeout(timeout)
    terminal = None
    while True:
        try:
            m = json.loads(ws.recv())
        except Exception:
            break
        if m.get("type") == "ping":
            continue
        if m["type"] in ("completed", "failed", "cancelled"):
            terminal = m
            break
    ws.close()
    return terminal


def submit_and_wait(user, timeout=15):
    headers = {"Content-Type": "application/json"}
    if user is not None:
        headers["X-Forwarded-User"] = user
    workflow = {"3": {"class_type": "KSampler", "inputs": {}}}
    req = urllib.request.Request(
        GW + "/api/generate", data=json.dumps({"workflow": workflow}).encode(), headers=headers
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return resp["job_id"], drain(resp["job_id"], timeout=timeout)


def set_next_output(filename, subfolder="", type="output", images=None):
    """What the stub's /history manifest reports for the NEXT job. One entry
    by default; `images` (a list of {filename, subfolder, type}) when the
    scenario needs a manifest with more than one entry in it."""
    body = {"images": images} if images is not None else {
        "filename": filename, "subfolder": subfolder, "type": type}
    req = urllib.request.Request(
        COMFY + "/__set_next_output__",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).read()


def confined(url):
    """Same independent recomputation check-60 uses: resolve the URL the way
    output_file() does and check it never leaves OUTPUT_ROOT."""
    if not url or not url.startswith("/outputs/"):
        return False
    candidate = (OUTPUT_ROOT / url[len("/outputs/"):]).resolve()
    return candidate.is_relative_to(OUTPUT_ROOT)


def fetch_ok(url):
    try:
        return len(urllib.request.urlopen(GW + url, timeout=10).read()) > 0
    except Exception:
        return False


def workspace_dirs(url):
    if not url or not url.startswith("/outputs/"):
        return []
    return url[len("/outputs/"):].split("/")[:-1]


def workspace_ok(url):
    return bool(url) and len(workspace_dirs(url)) >= 1 and confined(url) and fetch_ok(url)


hostile_filename = "../../OUTSIDE/secret.txt"

# ---------------------------------------------------------------------------
# (a) reproduce the gate's finding against the real, running agent's own
#     output, before checking anything about the fix.
# ---------------------------------------------------------------------------
print("\n== (a) reproduction: the pre-fix URL shape really does escape OUTPUT_ROOT")

seed_output()
_, terminal_control = submit_and_wait("alice-repro")
control_images = (terminal_control or {}).get("data", {}).get("images", [])
url_control = control_images[0]["url"] if control_images else None
check("control job (ordinary manifest) completes with a real, confined "
      "workspace URL -- this is what a submitter's confined subfolder "
      "actually looks like, learned from the live agent rather than "
      "reimplemented here",
      workspace_ok(url_control), url_control)

control_workspace = "/".join(workspace_dirs(url_control))

# This is exactly the pre-fix URL-building shape: the agent's own confined
# subfolder for a real submitter, with the hostile filename concatenated
# onto it raw -- the shape collect_outputs() used before FIX 4a.
pre_fix_url = f"/outputs/{control_workspace}/{hostile_filename}".replace("//", "/")
check("reproduction: concatenating a raw hostile filename onto a real, "
      "correctly-confined workspace subfolder still escapes OUTPUT_ROOT -- "
      "confirming the gate's finding: subfolder confinement alone was never "
      "enough",
      not confined(pre_fix_url), pre_fix_url)


# ---------------------------------------------------------------------------
# (b) end-to-end: a job whose manifest carries the hostile filename
# ---------------------------------------------------------------------------
print("\n== (b) end-to-end: a hostile filename in the manifest never reaches a served URL")

seed_output()
set_next_output(hostile_filename, subfolder="")
job_id, terminal = submit_and_wait("mallory")
check("the job still completes -- a hostile output does not fail the whole "
      "job, it is simply not served",
      terminal and terminal["type"] == "completed", terminal)

images = (terminal or {}).get("data", {}).get("images", [])
check("no image entry is reported for the hostile filename at all -- not "
      "confined-and-served, not truncated-and-served, just absent",
      images == [], images)

# ---------------------------------------------------------------------------
# (c) an ordinary filename with parentheses is not over-sanitized
# ---------------------------------------------------------------------------
print("\n== (c) an ordinary filename (parentheses) still gets a real, confined, servable URL")

seed_output()
legit_filename = "final_output(2).png"
(OUTPUT_ROOT / legit_filename).write_bytes(b"fake png bytes")
set_next_output(legit_filename, subfolder="")
job_id, terminal = submit_and_wait("nora")
images = (terminal or {}).get("data", {}).get("images", [])
url_legit = images[0]["url"] if images else None
check("a legitimate filename with parentheses is still served "
      "from a real, confined, per-submitter workspace -- confinement must "
      "not be so aggressive it breaks ordinary output filenames",
      workspace_ok(url_legit), url_legit)

# ---------------------------------------------------------------------------
# (d) a filename containing a slash, without spelling '..', is still refused
# ---------------------------------------------------------------------------
print("\n== (d) a filename containing '/' (no '..' needed) is refused the same way")

seed_output()
set_next_output("sub/evil.png", subfolder="")
job_id, terminal = submit_and_wait("oscar")
check("the job still completes",
      terminal and terminal["type"] == "completed", terminal)
images = (terminal or {}).get("data", {}).get("images", [])
check("a filename containing '/' is refused just like one containing '..' "
      "-- subfolder is the only field a save node uses to nest, so a slash "
      "inside filename itself is never legitimate",
      images == [], images)

# ---------------------------------------------------------------------------
# (e) a preview (type "temp") beside a real output is not reported as one
# ---------------------------------------------------------------------------
print("\n== (e) a 'temp' preview in the manifest is dropped; the 'output' beside it is served")

# ComfyUI's PreviewImage node reports its files in the same manifest shape as
# SaveImage, with type "temp" instead of "output" -- and writes them under
# --temp-directory, which is /tmp in the pod and not the shared volume at all.
# A URL built for one of those is a 404 by construction, and worse than a 404:
# it is one that resolves into the shared volume at a path a later SaveImage
# on another worker could legitimately fill.
seed_output()
set_next_output(None, images=[
    {"filename": "ComfyUI_temp_abcde_00001_.png", "subfolder": "", "type": "temp"},
    {"filename": "out_0001.png", "subfolder": "", "type": "output"},
])
job_id, terminal = submit_and_wait("petra")
check("the job completes", terminal and terminal["type"] == "completed", terminal)
images = (terminal or {}).get("data", {}).get("images", [])
check("exactly one image is reported -- the durable output, not the preview "
      "that ComfyUI wrote to its temp directory and will never serve",
      len(images) == 1 and images[0]["filename"] == "out_0001.png"
      and images[0].get("type") == "output", images)
check("and that one is a real, confined, servable URL",
      workspace_ok(images[0]["url"]) if images else False, images)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
