# Unit tests

Pure functions in `enterprise/gateway/hub.py` and `enterprise/worker/
worker_agent.py`, called directly and in-process -- no Redis, no ComfyUI, no
subprocess. See `../README.md` for the end-to-end suite this complements.

```bash
pip install -r ../../gateway/requirements.txt -r requirements-test.txt websocket-client
python3 -m pytest enterprise/test/unit
```

210 assertions in well under a second. `conftest.py` explains the two import
guards this needs (hub.py imports cleanly with no environment set; importing
worker_agent.py installs real SIGTERM/SIGINT handlers as a side effect, so
the import is wrapped to save and restore whatever pytest already had
installed).

## Covered

- **`test_envelope.py`** -- `build_envelope()`/`parse_envelope()` round trip
  (every field), the `MAX_ENVELOPE_FIELD_CHARS` clamp on both the producer
  and consumer side, malformed/missing `job_id`/`workflow`, `schema_version`
  coercion (including the pre-F2 shape and a payload from a newer gateway),
  `needs_payload()`/`with_workflow()`/`queue_record()`, `attempt_count_of()`.
  Runs against both `hub.py`'s and `worker_agent.py`'s copies of the shared
  block.
- **`test_period_boundaries.py`** -- `showback_period()` and
  `quota_period_reset()` across the December→January boundary, a leap-year
  February, and the UTC assumption both functions make (proven with the
  process's actual local timezone shifted via `time.tzset()`, not mocked).
- **`test_workspace_name.py`** -- check-60's hostile strings (traversal,
  absolute path, 2000 chars), the anonymous case, the slug-allowlist and
  digest-length invariants, two usernames that share a truncated slug or
  collapse to the same readable half getting different names, a Cyrillic
  homoglyph not colliding with its ASCII lookalike, and what NFC-vs-NFD
  Unicode normalization actually does to the result (documented, not fixed
  -- see the final wave-3A report). Both copies.
- **`test_output_paths.py`** -- `is_bare_filename()` (both copies, including
  that it does not decode percent-encoding -- documented, see the report),
  `output_url()` and `rewrite_image_urls()` (hub.py, the raw-`executed`-event
  path check-10 exercises end to end), `scoped_prefix()` and
  `scope_workflow_outputs()` (worker_agent.py), `output_subfolder()` and
  `workspace_path()` against a `tmp_path` `OUTPUT_ROOT`, including a planted
  symlink escaping it. Also documents (not fixes) the cross-workspace
  `os.replace()` behaviour tracked as W5 in AUDIT-AND-PLAN.md.
- **`test_wait_and_quota.py`** -- `estimated_wait_seconds()` and
  `quota_gpu_seconds_used()` against a small fake async Redis connection:
  empty queue, malformed JSON, a missing/bool/string `submitted_at`, future
  timestamps clamping to zero, the fail-open paths (Redis error, a
  non-numeric field), and that the read side names the same key/field the
  showback accrual writes.
- **`test_misc_helpers.py`** -- `caller_identity()`, `locate_output()`'s
  resolve-then-compare containment check (including the
  `<mine>/../<theirs>/x` case check-66 pins), `quota_headers()`, and
  `showback_accrue_call()`'s key/arg order.

## Deliberately skipped

- **`quota_refusal()`, `generate()`, `output_file()`, `showback()` and
  everything else that is `async def` and touches the live Redis connection
  pool or the ASGI request/response cycle end to end** -- these are
  integration behaviour, not pure functions, and are exactly what
  check-15/-66/-90/-95 already prove against a real gateway.
- **`collect_outputs()`, `run_job()`, `finish()`, the reaper functions** --
  all call out to ComfyUI's HTTP API or hold real Redis/thread state; the
  e2e suite's stub-ComfyUI fixtures are what this would have to reinvent.
  This includes the `type: temp`/preview-image gap tracked as W7 --
  `collect_outputs()` never filters on `image.get("type")`, but the function
  that would need to change is not a pure one.
- **The showback Lua script itself (`SHOWBACK_ACCRUE_LUA`) and
  `FAIR_ENQUEUE_LUA`** -- Lua run inside Redis via `EVAL`, not Python;
  `bench-fair-enqueue.py` and check-50/-90 are what exercises them.
- **`ensure_workspace()`'s multi-pod convergence (`set_shared_mode()`'s
  `EPERM`-is-normal path)** -- needs two different arbitrary UIDs writing
  the same directory, which is cluster-day behaviour `scripts/lint.sh` pins
  the *mode* of; a single-UID `tmp_path` cannot reproduce the interesting
  case.
