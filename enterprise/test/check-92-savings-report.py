"""
N3 -- the monthly savings report (docs/10-roadmap.md).

The roadmap item in one sentence: a small reporter assembling, from data the
system already collects, "utilization, waits, cost per render, spend versus
the card-per-person counterfactual -- so the system generates its own
business case". Nothing implements this yet: there is no
scripts/savings-report.py, and the showback Hash counts no jobs, so nothing
on HEAD can put a denominator under "cost per render" -- every assertion
below is expected to fail on HEAD.

THE INTERFACE THIS CHECK PINS -- whoever implements N3 either matches this
or updates this file, with both shown in review:

  - The reporter is `scripts/savings-report.py`, python3 stdlib only (it has
    to run on the same laptop that runs the teardown-capture habit, with no
    venv), reading EITHER a live gateway (`--gateway URL`, which is where
    "waits if available" comes from -- /api/stats is point-in-time and only
    exists while a gateway is up) OR a captured report file (`--from-json
    PATH` -- the exact JSON `oc exec ... /api/showback > showback-2026-08.json`
    already saves in Q4's documented teardown habit, which is the shape a
    monthly report is most likely to be run FROM, the cluster being gone).

  - It prints markdown to stdout -- headings, bold figures, plain bullet
    lines, no tables (Slack renders none) -- "suitable for pasting into
    Slack" being the item's own acceptance test.

  - The denominator: the accrual gains a billed-jobs counter -- a `jobs`
    field on the SAME period Hash, incremented in the SAME Lua script that
    accrues submitter seconds (BEGIN SHARED SHOWBACK: same key, so all three
    key-space bounds still hold; the reserved field is unprefixed, so a
    submitter calling themselves "jobs" lands on "u:jobs" and cannot collide)
    -- and `/api/showback` reports it as `billed_jobs`. Counted on the
    submitter path only: failed and cancelled jobs count, because Q4 bills
    them ("a job that failed after twenty minutes is billed twenty
    minutes"), and reaper-excluded time does not, because no submitter was
    billed for it. "Cost per render" is billed spend over billed jobs --
    the honest version of the phrase under Q4's own definitions.

  - The arithmetic, pinned against a fixture this file fabricates (a fully
    elapsed past month, so nothing depends on when the check runs):
    spend = held seconds / 3600 * --rate; the counterfactual = headcount x
    --card-month (docs/02's $713/mo per always-on L4, the default),
    prorated by elapsed period fraction so a mid-month run does not compare
    a part month against a whole one; utilization = held hours over
    headcount x elapsed hours -- the duty cycle the counterfactual's
    dedicated cards would have needed, docs/02's own idle-tax framing.
    Headcount defaults to the identities the report actually names (users +
    anonymous, a floor, and says so) and `--headcount` overrides it, because
    the team is usually larger than the set who submitted this month.

  - Excluded time is in pool spend and NOT in cost-per-render's numerator,
    with its own line -- the same reaper decision the report and quota
    already honour, surfaced rather than folded in.
"""
import calendar
import json
import os
import re
import subprocess
import sys
import time
import uuid

from harness import GW, check, failures, showback

sys.stdout.reconfigure(line_buffering=True)

# run.sh copies every check into a scratch work dir, so __file__ points
# nowhere useful -- but it exports WORKER_AGENT anchored at the repo, and
# the reporter under test lives beside it.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(os.environ["WORKER_AGENT"]))))
REPORTER = os.path.join(REPO, "scripts", "savings-report.py")


def run_reporter(*args):
    proc = subprocess.run([sys.executable, REPORTER, *args],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def money(text, amount):
    """True if `text` shows `amount` as a dollar figure, thousands separator
    and two decimals -- $1,944.00 -- the one formatting rule pinned here so
    the fixture's arithmetic is checkable by grep."""
    return f"${amount:,.2f}" in text


# --- the deterministic half: a fabricated, fully-elapsed month --------------

print("== the reporter's arithmetic, against a captured-report fixture")

now = time.gmtime()
prev_year = now.tm_year - (1 if now.tm_mon == 1 else 0)
prev_mon = 12 if now.tm_mon == 1 else now.tm_mon - 1
PERIOD = f"{prev_year:04d}-{prev_mon:02d}"
MONTH_HOURS = calendar.monthrange(prev_year, prev_mon)[1] * 24

# 50h + 25h billed to users, 1h anonymous, 2h excluded, 380 billed jobs.
FIXTURE = {
    "period": PERIOD,
    "users": {"alice@example.com": 180000.0, "bob@example.com": 90000.0},
    "users_total_gpu_seconds": 270000.0,
    "anonymous_gpu_seconds": 3600.0,
    "excluded_gpu_seconds": 7200.0,
    "other_gpu_seconds": 0.0,
    "billed_jobs": 380,
    "truncated": False,
    "periods_available": [PERIOD],
}

RATE = 2.0        # $/GPU-hour, chosen for legible arithmetic
CARD_MONTH = 700.0

BILLED_HOURS = (270000.0 + 3600.0) / 3600.0          # 76.0
EXCLUDED_HOURS = 7200.0 / 3600.0                     # 2.0
POOL_SPEND = (BILLED_HOURS + EXCLUDED_HOURS) * RATE  # $156.00
COST_PER_RENDER = BILLED_HOURS * RATE / 380          # $0.40
HEADCOUNT = 3                                        # alice, bob, anonymous
COUNTERFACTUAL = HEADCOUNT * CARD_MONTH              # $2,100.00, full month
SAVINGS = COUNTERFACTUAL - POOL_SPEND                # $1,944.00
UTILIZATION = (BILLED_HOURS + EXCLUDED_HOURS) / (HEADCOUNT * MONTH_HOURS)

fixture_path = os.path.abspath(f"savings-fixture-{uuid.uuid4().hex[:8]}.json")
with open(fixture_path, "w") as f:
    json.dump(FIXTURE, f)

code, out, err = run_reporter("--from-json", fixture_path,
                              "--rate", str(RATE),
                              "--card-month", str(CARD_MONTH))

check("the reporter exists and exits 0 on a captured report",
      code == 0, {"code": code, "stderr": err[-400:]})
check("total pool spend is billed + excluded at the given rate",
      money(out, POOL_SPEND), f"expected ${POOL_SPEND:,.2f} in output")
check("cost per render is billed spend over billed jobs -- excluded time in "
      "neither the numerator nor the count",
      money(out, COST_PER_RENDER), f"expected ${COST_PER_RENDER:,.2f}")
check("the render count itself is stated",
      re.search(r"\b380\b", out) is not None, "expected '380'")
check("the counterfactual is headcount x card-month, unprorated for a past "
      "month (the period is fully elapsed)",
      money(out, COUNTERFACTUAL), f"expected ${COUNTERFACTUAL:,.2f}")
check("and the savings line is the difference",
      money(out, SAVINGS), f"expected ${SAVINGS:,.2f}")
check("utilization is held hours over headcount x elapsed hours, as a "
      "percentage to one decimal",
      f"{UTILIZATION * 100:.1f}%" in out, f"expected {UTILIZATION * 100:.1f}%")
check("the excluded line is its own, named -- the reaper decision surfaced",
      "excluded" in out.lower(), None)
check("the output is markdown with a heading",
      out.lstrip().startswith("#"), out[:80])
check("headcount is called a floor when derived from the report",
      "floor" in out.lower() or "at least" in out.lower(), None)

code_hc, out_hc, _ = run_reporter("--from-json", fixture_path,
                                  "--rate", str(RATE),
                                  "--card-month", str(CARD_MONTH),
                                  "--headcount", "10")
check("--headcount overrides the derived floor: 10 x card-month",
      code_hc == 0 and money(out_hc, 10 * CARD_MONTH),
      f"expected ${10 * CARD_MONTH:,.2f}")

# A capture from before the jobs counter existed: cost per render must be
# reported unavailable, not divided by zero and not silently omitted.
old = dict(FIXTURE)
del old["billed_jobs"]
old_path = fixture_path + ".old"
with open(old_path, "w") as f:
    json.dump(old, f)
code_old, out_old, err_old = run_reporter("--from-json", old_path,
                                          "--rate", str(RATE),
                                          "--card-month", str(CARD_MONTH))
check("a pre-counter capture still reports, saying cost per render is "
      "unavailable rather than fabricating or crashing",
      code_old == 0 and ("unavailable" in out_old.lower()
                         or "no billed-job count" in out_old.lower()),
      {"code": code_old, "stderr": err_old[-200:]})

os.unlink(fixture_path)
os.unlink(old_path)

# --- the live half: the jobs counter through the real accrual ---------------

print("\n== the billed-jobs counter, through a real submit and the real Lua")

USER = f"savings-{uuid.uuid4().hex[:8]}@example.com"
before = showback(GW)
jobs_before = int(before.get("billed_jobs", 0) or 0)

job_ids = []
for _ in range(2):
    probe = f"probe-{uuid.uuid4().hex[:8]}"
    resp = GW.post("/api/generate",
                   {"workflow": {probe: {"class_type": "KSampler",
                                         "inputs": {}}}},
                   user=USER)
    job_ids.append(resp["job_id"])

deadline = time.time() + 60
done = set()
while time.time() < deadline and len(done) < len(job_ids):
    for job_id in job_ids:
        if job_id in done:
            continue
        try:
            state = GW.get(f"/api/jobs/{job_id}")
        except Exception:  # noqa: BLE001
            continue
        if state.get("status") in ("completed", "failed", "cancelled"):
            done.add(job_id)
    time.sleep(0.2)

check("two real jobs reached a terminal state", len(done) == 2, done)

after = showback(GW)
jobs_after = int(after.get("billed_jobs", 0) or 0)
check("/api/showback reports billed_jobs, and the two jobs moved it by two",
      jobs_after - jobs_before == 2,
      {"before": jobs_before, "after": jobs_after})
check("and this submitter's seconds landed beside the count, as always",
      USER in (after.get("users") or {}), sorted(after.get("users") or {}))

code_live, out_live, err_live = run_reporter(
    "--gateway", "http://127.0.0.1:8100", "--rate", "2.0")
check("the reporter runs against a live gateway and exits 0",
      code_live == 0, {"code": code_live, "stderr": err_live[-400:]})
check("a live run includes the point-in-time queue line -- the 'waits if "
      "available' the item asks for, which only a live gateway can supply",
      "wait" in out_live.lower() and "queue" in out_live.lower(),
      out_live[-400:])
check("and a cost-per-render line, from the counter the live half just moved",
      "per render" in out_live.lower(), None)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
