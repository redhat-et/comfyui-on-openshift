"""
Q3 — per-user output workspaces (docs/10-roadmap.md).

hub.py's own generate() says it plainly: X-Forwarded-User is client-supplied
whenever AUTH_MODE=none, and "never treat it as authorization". This item is
the one place that stops being true as prose and starts being true as code —
the header stops being informational and becomes a FILESYSTEM PATH COMPONENT,
because the whole point is that two users' generations land in separate
places instead of one flat /output directory every job shares. A path
component built from a client-supplied string is exactly the shape of a
path-traversal bug, so this is written as hostile security testing, not
happy-path testing.

None of this exists yet. Every image today is reported and served flat:
rewrite_image_urls() in hub.py builds `/outputs/{subfolder}/{filename}`
straight from whatever ComfyUI/the worker handed it, with no notion of WHO
submitted the job at all — collect_outputs() in worker_agent.py never reads
the envelope's `user` field, reserved for exactly this by F2. Concretely, the
stub ComfyUI (fake_comfy.py) reports the identical {"out_0001.png", ""} for
every single job regardless of who submitted it or what they submitted, so
on HEAD every job below — different users, hostile users, no user at all —
resolves to the exact same flat URL. Every assertion here that checks for a
non-flat, per-submitter workspace therefore fails on HEAD by construction.

Four things this check proves, matching the roadmap's item text:

  (a) two users' outputs land in separate PLACES (not just separate
      filenames) and each is served correctly from its own place.
  (b) a hostile username — path traversal, an absolute path, empty, very
      long, and one containing characters an oauth-proxy username may
      legitimately carry (@ and .) — either is rejected at submit, or the
      system cannot be made to produce or serve any path outside
      OUTPUT_ROOT. The confinement check below (`confined()`) recomputes
      containment independently of output_file()'s own resolve()/
      is_relative_to() guard in hub.py, so a sanitizer that PRODUCES an
      escaping path is caught here even before that endpoint's own guard
      would also catch it — this check is about what the system tries to
      build, not only what it refuses to serve.
  (c) with no authenticated user at all (no header — the common
      AUTH_MODE=none case with no proxy in front), the system still works,
      and "no user" is not silently aliased onto whichever real username
      happens to submit next.
  (d) a save node's filename_prefix (scoped_prefix() / scope_workflow_outputs()
      in worker_agent.py — the other half of Q3, alongside output_subfolder()
      which (a)-(c) above already exercise) is actually rewritten into the
      submitter's workspace before the workflow reaches ComfyUI, and a
      traversal attempt through it is refused rather than honoured. Every
      workflow every OTHER check in this suite submits is a bare KSampler
      with no filename_prefix input at all, so scope_workflow_outputs() finds
      nothing to rewrite anywhere else — every agent log line in a full
      `make test` run reads "0 save node(s) rewritten" without this. Proven
      by asking the stub what filename_prefix it actually received
      (fake_comfy.py's /__received_prefixes__), not by anything the gateway
      hands back — the stub's output manifest is the same
      {out_0001.png, ""} regardless of this input, so it cannot tell a
      rewritten prefix from an unrewritten one.

This file does not touch enterprise/gateway/hub.py or
enterprise/worker/worker_agent.py. check-10-stream.py's flat-URL pin (its
very first job, submitted with no X-Forwarded-User header at all) is
replaced in the same commit by a strictly stronger workspace-scoping
assertion — see that file's diff and the commit message for the old and new
text. The path-traversal assertion later in that same file
(`/outputs/../../etc/passwd`) is untouched and asserts a different thing:
that this endpoint's static guard holds for ANY path, not that outputs are
namespaced per user.
"""
import json, os, pathlib, sys, time, urllib.error, urllib.request, uuid
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
    """Recreate the single flat file fake_comfy's stubbed manifest always
    points at (out_0001.png, subfolder ""), the same way run.sh seeds it at
    suite start. Every scenario below reuses this identical source name on
    purpose: if per-user routing is not real, every URL asserted here
    collides on this one literal file no matter how many jobs run or who
    submitted them."""
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
    """user=None omits X-Forwarded-User entirely (no proxy in front, the
    AUTH_MODE=none default). user="" sends the header present but empty,
    which hub.py's request.headers.get(..., "") treats identically -- both
    are exercised below, deliberately, because they are different requests
    even though this endpoint currently treats them the same."""
    headers = {"Content-Type": "application/json"}
    if user is not None:
        headers["X-Forwarded-User"] = user
    workflow = {"3": {"class_type": "KSampler", "inputs": {}}}
    req = urllib.request.Request(
        GW + "/api/generate", data=json.dumps({"workflow": workflow}).encode(), headers=headers
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    terminal = drain(resp["job_id"], timeout=timeout)
    images = (terminal or {}).get("data", {}).get("images", [])
    return images[0]["url"] if images else None


def submit_with_save_node(user, prefix, timeout=15):
    """Like submit_and_wait, but the workflow also carries a SaveImage node
    whose filename_prefix is the thing under test -- scope_workflow_outputs()
    (worker_agent.py) is the only code in this whole system that ever reads
    this input, and every OTHER workflow in this suite (including
    submit_and_wait's own) omits it entirely. The node id is unique per call
    so fake_comfy's /__received_prefixes__ can be read for exactly this
    submission with no risk of reading a stale value a previous call left
    behind under the same key.

    Returns (terminal_event, node_id) -- not a URL, because the property
    under test here is what ComfyUI actually received, not what the gateway
    reported back afterward."""
    headers = {"Content-Type": "application/json"}
    if user is not None:
        headers["X-Forwarded-User"] = user
    node_id = f"save-{uuid.uuid4().hex[:8]}"
    workflow = {
        "3": {"class_type": "KSampler", "inputs": {}},
        node_id: {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix}},
    }
    req = urllib.request.Request(
        GW + "/api/generate", data=json.dumps({"workflow": workflow}).encode(), headers=headers
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return drain(resp["job_id"], timeout=timeout), node_id


def received_prefix(node_id):
    """The filename_prefix fake_comfy actually saw for `node_id` on the most
    recent /prompt that carried it -- None if it never arrived (the traversal
    case below, which must fail before ever reaching ComfyUI)."""
    body = json.loads(urllib.request.urlopen(COMFY + "/__received_prefixes__", timeout=10).read())
    return body.get(node_id)


def workspace_dirs(url):
    """The directory components between '/outputs/' and the filename -- i.e.
    WHERE an output lives, not what it is named. [] for today's flat
    '/outputs/<file>.png', which is exactly the shape this item removes."""
    if not url or not url.startswith("/outputs/"):
        return []
    return url[len("/outputs/"):].split("/")[:-1]


def confined(url):
    """Resolve the URL the same way output_file() does and check it never
    leaves OUTPUT_ROOT -- computed independently of that endpoint's own
    guard, so a sanitizer bug that PRODUCES an escaping path is caught here
    even before (or instead of) that endpoint refusing to serve it."""
    if not url or not url.startswith("/outputs/"):
        return False
    candidate = (OUTPUT_ROOT / url[len("/outputs/"):]).resolve()
    return candidate.is_relative_to(OUTPUT_ROOT)


def fetch_ok(url):
    try:
        return len(urllib.request.urlopen(GW + url, timeout=10).read()) > 0
    except Exception:
        return False


def workspace_ok(url):
    """The full positive claim this item makes about ANY accepted username:
    a real per-submitter directory, confined to OUTPUT_ROOT, that actually
    serves. All three conjuncts are required -- on HEAD workspace_dirs(url)
    is always [] (every job is flat), so this is False for every call in
    this file until Q3 lands."""
    return bool(url) and len(workspace_dirs(url)) >= 1 and confined(url) and fetch_ok(url)


# ---------------------------------------------------------------------------
# (a) two users, two separate places
# ---------------------------------------------------------------------------
print("\n== (a) two authenticated users get separate output workspaces")

seed_output()
url_alice = submit_and_wait("alice")
seed_output()
url_bob = submit_and_wait("bob")

check("alice and bob do NOT land on the same output -- fake_comfy reports "
      "the identical filename/subfolder for every job, so this can only "
      "differ if submitter identity actually changes WHERE the output goes",
      url_alice != url_bob, (url_alice, url_bob))

check("alice's output has its own confined, servable, per-submitter "
      "workspace directory (not the flat name every job shares today)",
      workspace_ok(url_alice), url_alice)
check("bob's output has its own confined, servable, per-submitter "
      "workspace directory (not the flat name every job shares today)",
      workspace_ok(url_bob), url_bob)

dirs_alice, dirs_bob = workspace_dirs(url_alice), workspace_dirs(url_bob)
check("alice's and bob's workspaces are different top-level PLACES, not "
      "just different filenames inside one shared directory",
      bool(dirs_alice) and bool(dirs_bob) and dirs_alice[0] != dirs_bob[0],
      (dirs_alice, dirs_bob))

# ---------------------------------------------------------------------------
# (b) hostile usernames
# ---------------------------------------------------------------------------
print("\n== (b) a hostile username is rejected at submit, or cannot escape OUTPUT_ROOT")

HOSTILE_USERS = [
    ("path traversal", "../../../../../../../../tmp/evil"),
    ("absolute path", "/etc/passwd"),
    ("empty string (header present, value empty)", ""),
    ("very long (2000 chars)", "a" * 2000),
]

for desc, hostile in HOSTILE_USERS:
    seed_output()
    rejected, url, err = False, None, None
    try:
        url = submit_and_wait(hostile)
    except (urllib.error.HTTPError, urllib.error.URLError, ConnectionError, OSError) as exc:
        rejected, err = True, repr(exc)

    safe = rejected or workspace_ok(url)
    check(f"hostile username [{desc}] is either rejected at submit, or "
          f"produces only a confined, namespaced, servable output -- never "
          f"a path outside OUTPUT_ROOT and never silent aliasing onto "
          f"today's shared flat name",
          safe, {"rejected": rejected, "error": err, "url": url})

# A username with characters an oauth-proxy legitimately produces must NOT
# be treated as hostile: it gets the same full positive guarantee as alice
# and bob above, not merely "did not escape".
print("\n== (b) a legitimate oauth-proxy-shaped username ('@' and '.') is not over-sanitized")
seed_output()
url_legit = submit_and_wait("alice.smith@example.com")
check("a username containing '@' and '.' still gets a real, confined, "
      "servable per-submitter workspace -- sanitization must not be so "
      "aggressive it breaks ordinary oauth-proxy usernames",
      workspace_ok(url_legit), url_legit)

# ---------------------------------------------------------------------------
# (c) no authenticated user at all
# ---------------------------------------------------------------------------
print("\n== (c) no authenticated user at all -- the system still works, and is not aliased onto a real user")

seed_output()
url_anon = submit_and_wait(None)
seed_output()
url_carol = submit_and_wait("carol")

check("an anonymous submission (no X-Forwarded-User header at all -- the "
      "ordinary AUTH_MODE=none shape) still completes and is served from a "
      "confined, workspace-scoped location, not from an unbounded path "
      "built out of an empty string and not by crashing",
      workspace_ok(url_anon), url_anon)

dirs_anon, dirs_carol = workspace_dirs(url_anon), workspace_dirs(url_carol)
check("the anonymous workspace is not the same place as a real "
      "authenticated user's -- 'no user' must not alias onto whoever the "
      "next real username happens to be",
      bool(dirs_anon) and bool(dirs_carol) and dirs_anon[0] != dirs_carol[0],
      (dirs_anon, dirs_carol))

# ---------------------------------------------------------------------------
# (d) a save node's filename_prefix is rewritten into the submitter's
#     workspace, and a traversal attempt through it is refused
# ---------------------------------------------------------------------------
print("\n== (d) a save node's filename_prefix is rewritten into the submitter's workspace")

BASE_PREFIX = "myrun"

seed_output()
terminal_dave, node_id = submit_with_save_node("dave", BASE_PREFIX)
check("the save-node job still completes",
      terminal_dave and terminal_dave["type"] == "completed", terminal_dave)

images = (terminal_dave or {}).get("data", {}).get("images", [])
url_dave = images[0]["url"] if images else None
dave_dirs = workspace_dirs(url_dave)
check("dave's job still produced its own workspace directory (as in (a) "
      "above -- the SaveImage node does not change output_subfolder()'s "
      "half of Q3)",
      bool(dave_dirs), url_dave)

expected_prefix = f"{dave_dirs[0]}/{BASE_PREFIX}" if dave_dirs else None
got_prefix = received_prefix(node_id)
check(f"the save node's filename_prefix reached ComfyUI already rewritten "
      f"to live inside dave's own workspace ({expected_prefix!r}), not left "
      f"as the bare {BASE_PREFIX!r} the workflow was submitted with -- proof "
      f"scope_workflow_outputs() actually ran, not just that it claims 0 "
      f"rewrites everywhere else in this suite",
      dave_dirs and got_prefix == expected_prefix, got_prefix)

print("\n== (d) a filename_prefix containing '..' is refused rather than honoured")

seed_output()
terminal_evil, evil_node_id = submit_with_save_node("dave", "../../evil")
check("a filename_prefix carrying a '..' segment fails the job instead of "
      "writing outside the workspace",
      terminal_evil and terminal_evil["type"] == "failed", terminal_evil)

err = (terminal_evil or {}).get("data", {}).get("error", "")
check("the failure names filename_prefix and the offending segment, so the "
      "caller can fix it",
      "filename_prefix" in err and ".." in err, err[:200])

check("the rejected workflow never reached ComfyUI at all -- scoped_prefix() "
      "raises before the job is ever submitted, so nothing here spent a GPU "
      "on a workflow that was going to be refused",
      received_prefix(evil_node_id) is None, received_prefix(evil_node_id))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
