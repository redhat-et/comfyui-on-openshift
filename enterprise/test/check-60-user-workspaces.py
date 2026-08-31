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

Three things this check proves, matching the roadmap's item text:

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
import json, os, pathlib, sys, time, urllib.error, urllib.request
import websocket

GW = "http://127.0.0.1:8100"
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

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
