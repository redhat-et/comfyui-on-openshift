"""
N4 -- FOCUS-format chargeback export (docs/10-roadmap.md).

The roadmap item in one sentence: `GET /api/showback` grown a `?format=focus`
CSV in the FinOps FOCUS column set -- "the checkbox enterprise finance
actually asks for". Same accounting, same scoping, a second serialization.
Nothing implements this yet: `showback()` (hub.py) ignores any `format`
parameter and always returns JSON, and hub.py reads no GPU rate at all --
every FOCUS-specific assertion below is expected to fail on HEAD.

THE INTERFACE THIS CHECK PINS, the way check-95 pins the quota breaker's --
whoever implements N4 either matches this or updates this file, with both
shown in review:

  - `GET /api/showback?format=focus` answers `200 text/csv`. The default
    (no `format`, or `format=json`) keeps returning exactly the JSON report
    check-90 already pins -- finance gets a new serialization, nothing
    already reading the endpoint notices. An unknown `format` is a 400, not
    a silent fall-through to JSON: a typo'd `format=focsu` that quietly
    returned the wrong content type would be discovered by the spreadsheet
    that fails to import, which is the least useful place.

  - The header row is the FOCUS 1.2 column set below, exactly and in this
    order. Mandatory columns are all present; the conditional/optional ones
    included are the ones this system can say something TRUE in
    (SubAccountId/Name for the submitter, ChargeDescription). Columns the
    gateway cannot truthfully populate -- RegionId, ResourceId, SkuId: it
    does not know what region it runs in or which node ran a job, and the
    monthly Hash holds per-identity aggregates, not per-resource rows --
    are present-but-empty, which is what FOCUS specifies for a value the
    provider does not have, rather than filled with a plausible guess.
    An aggregate row is honest; a fabricated ResourceId is not.

  - One row per submitter, plus one for each non-zero bucket: `anonymous`
    and `other` bill under those literal SubAccountIds (they are real spend
    with a defined owner-class), and `excluded` -- GPU time held by workers
    that died mid-job, deliberately billed to nobody (Q4's reaper decision)
    -- exports with an EMPTY SubAccountId and a ChargeDescription that says
    what it is. Dropping the excluded row would make the CSV's total
    disagree with the JSON report's, and a FOCUS consumer summing BilledCost
    must see the same pool spend the JSON shows.

  - BilledCost = gpu_seconds / 3600 * GPU_HOURLY_RATE, the rate the new env
    var `GPU_HOURLY_RATE` carries (default 0.976 -- docs/02-cost.md's
    g6.xlarge all-in figure; this check pins its own value the way check-95
    pins QUOTA_GPU_SECONDS, and expects tolerant parsing on the same
    argument: a garbled rate must not crash-loop the gateway).
    BilledCost == EffectiveCost == ListCost == ContractedCost: this rate
    model has no commitment discounts and no negotiated pricing, and FOCUS's
    answer for that case is four equal columns, not blanks.

  - ChargePeriodStart/End and BillingPeriodStart/End are the UTC calendar
    month, ISO 8601 with a Z suffix, END-EXCLUSIVE (the first instant of the
    next month) -- FOCUS date-time semantics. The accumulator is one Hash
    per month with no finer timestamps (BEGIN SHARED SHOWBACK), so the
    charge period IS the billing period; pretending to per-day granularity
    would be fabrication.

  - The submitter identity round-trips through CSV intact. It is a
    client-supplied header (AUTH_MODE=none), so a name carrying a comma and
    a double quote must come back exactly itself through a conforming CSV
    parser, not shear the row into extra columns.

  - Scoping is the JSON report's, unchanged (check-66 pins the JSON side):
    under AUTH_MODE=oauth a caller not in SHOWBACK_OPERATORS gets a CSV of
    their own row only -- the full CSV is the list of submitters, which is
    exactly what scoped_showback() exists to withhold.
"""
import csv
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from harness import check, connect_redis, failures

sys.stdout.reconfigure(line_buffering=True)

# A dedicated gateway, not the shared one on 8100, so GPU_HOURLY_RATE is a
# value this file chose. 3.6 makes the arithmetic legible: $3.60 per
# GPU-hour is exactly $0.001 per GPU-second.
FGW_PORT = 8104
FGW = f"http://127.0.0.1:{FGW_PORT}"
GPU_HOURLY_RATE = 3.6

# And a second one under AUTH_MODE=oauth for the scoping half, the way
# check-66 runs its :8103. 8105 is the next free port in this suite.
OGW_PORT = 8105
OGW = f"http://127.0.0.1:{OGW_PORT}"

# The contract: FOCUS 1.2 columns, alphabetical. Every mandatory column is
# here; the non-mandatory ones included are those this system has true
# values for. If the implementation and this list disagree, one of them is
# wrong in review, which is the point of writing the list out twice.
FOCUS_COLUMNS = [
    "BilledCost", "BillingAccountId", "BillingAccountName",
    "BillingCurrency", "BillingPeriodEnd", "BillingPeriodStart",
    "ChargeCategory", "ChargeClass", "ChargeDescription",
    "ChargePeriodEnd", "ChargePeriodStart",
    "ConsumedQuantity", "ConsumedUnit",
    "ContractedCost", "ContractedUnitPrice",
    "EffectiveCost", "ListCost", "ListUnitPrice",
    "PricingQuantity", "PricingUnit",
    "ProviderName", "PublisherName",
    "RegionId", "RegionName", "ResourceId", "ResourceName",
    "ServiceCategory", "ServiceName", "SkuId",
    "SubAccountId", "SubAccountName",
]

SHOWBACK_KEY_PREFIX = "comfy:showback:"
SHOWBACK_USER_PREFIX = "u:"

r = connect_redis()


def showback_period(now):
    return time.strftime("%Y-%m", time.gmtime(now))


def month_bounds_iso(now):
    """This UTC month's [start, end) as ISO 8601 Z strings -- computed here
    independently of the implementation, from the same time the seeds use."""
    moment = time.gmtime(now)
    start = (moment.tm_year, moment.tm_mon)
    end = (moment.tm_year + (1 if moment.tm_mon == 12 else 0),
           1 if moment.tm_mon == 12 else moment.tm_mon + 1)
    fmt = "{:04d}-{:02d}-01T00:00:00Z"
    return fmt.format(*start), fmt.format(*end)


def seed(field, value):
    r.hset(f"{SHOWBACK_KEY_PREFIX}{showback_period(time.time())}", field, value)


def http(method, path, headers=None, base=FGW, timeout=15):
    """(status, content_type, raw_text) -- raw because the point of half
    these assertions is the bytes on the wire, not a parsed convenience."""
    req = urllib.request.Request(base + path, headers=headers or {},
                                 method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return (resp.getcode(), resp.headers.get("Content-Type", ""),
                resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return (exc.code, exc.headers.get("Content-Type", ""),
                exc.read().decode("utf-8", errors="replace"))


def rows_by_subaccount(raw_csv):
    """{SubAccountId: row-dict}, plus the ordered header, via a conforming
    CSV parser -- which is itself part of the contract: a row that shears on
    an embedded comma lands here as a wrong or missing SubAccountId."""
    reader = csv.DictReader(io.StringIO(raw_csv))
    rows = {row.get("SubAccountId", ""): row for row in reader}
    return reader.fieldnames or [], rows


def start_gateway(port, env_extra, log_name):
    env = dict(os.environ)
    env.update(env_extra)
    log = open(log_name, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "hub:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        env=env, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            status, _, _ = http("GET", "/readyz", base=f"http://127.0.0.1:{port}")
            if status == 200:
                return proc
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return proc


# --- fixtures ---------------------------------------------------------------

RUN = uuid.uuid4().hex[:8]
PLAIN_USER = f"focus-plain-{RUN}@example.com"
# A comma, a double quote and a leading '=' -- the CSV-hostile shapes a
# client-supplied header can take. (The '=' is why an implementation must
# not "helpfully" mangle values either: the identity must round-trip.)
HOSTILE_USER = f'focus,"evil"={RUN}@example.com'
OPERATOR = f"focus-operator-{RUN}@example.com"

PLAIN_SECONDS = 7200.0     # 2 GPU-hours -> $7.20 at the pinned rate
HOSTILE_SECONDS = 1800.0   # 0.5        -> $1.80
ANON_SECONDS = 900.0       # 0.25       -> $0.90
OTHER_SECONDS = 450.0      # 0.125      -> $0.45
EXCLUDED_SECONDS = 3600.0  # 1.0        -> $3.60, billed to nobody

seed(f"{SHOWBACK_USER_PREFIX}{PLAIN_USER}", PLAIN_SECONDS)
seed(f"{SHOWBACK_USER_PREFIX}{HOSTILE_USER}", HOSTILE_SECONDS)
seed(f"{SHOWBACK_USER_PREFIX}{OPERATOR}", 36.0)
seed("anonymous", ANON_SECONDS)
seed("other", OTHER_SECONDS)
seed("excluded", EXCLUDED_SECONDS)

PERIOD_START, PERIOD_END = month_bounds_iso(time.time())

print(f"--- starting a dedicated gateway on :{FGW_PORT} with "
      f"GPU_HOURLY_RATE={GPU_HOURLY_RATE}")
fgw = start_gateway(FGW_PORT, {"GPU_HOURLY_RATE": str(GPU_HOURLY_RATE)},
                    "focus-gateway.log")

print(f"--- and one on :{OGW_PORT} with AUTH_MODE=oauth for the scoping half")
ogw = start_gateway(OGW_PORT, {"GPU_HOURLY_RATE": str(GPU_HOURLY_RATE),
                               "AUTH_MODE": "oauth",
                               "SHOWBACK_OPERATORS": OPERATOR},
                    "focus-oauth-gateway.log")

try:
    print("\n== the endpoint answers CSV, and only for a format it knows")

    status, ctype, body = http("GET", "/api/showback?format=focus")
    check("GET /api/showback?format=focus is 200", status == 200, status)
    check("and its Content-Type is text/csv", ctype.startswith("text/csv"),
          ctype)

    status_json, _, body_json = http("GET", "/api/showback")
    try:
        parsed_json = json.loads(body_json)
    except json.JSONDecodeError:
        parsed_json = None
    check("the default (no format) is still check-90's JSON report",
          status_json == 200 and isinstance(parsed_json, dict)
          and "users" in parsed_json,
          {"status": status_json})

    status_bad, _, _ = http("GET", "/api/showback?format=focsu")
    check("an unknown format is refused with 400, not silently JSON",
          status_bad == 400, status_bad)

    status_period, _, _ = http("GET", "/api/showback?format=focus&period=2026-9")
    check("a malformed period is still a 400 through the focus path",
          status_period == 400, status_period)

    print("\n== the column set is FOCUS 1.2's, exactly")

    header, rows = rows_by_subaccount(body)
    check("the header row is the pinned FOCUS column list, in order",
          header == FOCUS_COLUMNS,
          {"got": header})

    print("\n== every line of the report is a row, and the math is the rate's")

    for who, seconds in ((PLAIN_USER, PLAIN_SECONDS),
                         (HOSTILE_USER, HOSTILE_SECONDS),
                         ("anonymous", ANON_SECONDS),
                         ("other", OTHER_SECONDS)):
        row = rows.get(who)
        label = who if "," not in who else "the CSV-hostile identity"
        check(f"{label} has a row, SubAccountId intact through a CSV parser",
              row is not None, {"subaccounts": sorted(rows)})
        if row is None:
            continue
        expect = seconds / 3600.0 * GPU_HOURLY_RATE
        try:
            billed = float(row["BilledCost"])
        except (KeyError, TypeError, ValueError):
            billed = None
        check(f"  BilledCost = {seconds}s / 3600 * rate = {expect:.6g}",
              billed is not None and abs(billed - expect) < 1e-6,
              row.get("BilledCost"))
        check("  BilledCost == EffectiveCost == ListCost == ContractedCost "
              "(no discounts exist in this rate model)",
              row.get("EffectiveCost") == row.get("BilledCost")
              == row.get("ListCost") == row.get("ContractedCost"),
              {k: row.get(k) for k in ("BilledCost", "EffectiveCost",
                                       "ListCost", "ContractedCost")})
        try:
            consumed = float(row["ConsumedQuantity"])
        except (KeyError, TypeError, ValueError):
            consumed = None
        check("  ConsumedQuantity is the raw GPU seconds",
              consumed is not None and abs(consumed - seconds) < 1e-6,
              row.get("ConsumedQuantity"))
        check("  the charge and billing periods are this UTC month, ISO 8601 Z, "
              "end-exclusive",
              row.get("ChargePeriodStart") == PERIOD_START
              and row.get("ChargePeriodEnd") == PERIOD_END
              and row.get("BillingPeriodStart") == PERIOD_START
              and row.get("BillingPeriodEnd") == PERIOD_END,
              {k: row.get(k) for k in ("ChargePeriodStart", "ChargePeriodEnd")})

    print("\n== the excluded bucket is exported, billed to nobody, and says so")

    excluded_rows = [row for row in rows.values()
                     if row.get("SubAccountId", "") == ""]
    check("exactly one row has an empty SubAccountId -- the excluded bucket",
          len(excluded_rows) == 1, len(excluded_rows))
    if excluded_rows:
        row = excluded_rows[0]
        expect = EXCLUDED_SECONDS / 3600.0 * GPU_HOURLY_RATE
        try:
            billed = float(row.get("BilledCost", ""))
        except (TypeError, ValueError):
            billed = None
        check("  its BilledCost is the excluded seconds at the same rate -- "
              "real pool spend, so a consumer summing the CSV sees what the "
              "JSON report's totals show",
              billed is not None and abs(billed - expect) < 1e-6,
              row.get("BilledCost"))
        check("  its ChargeDescription says the time is billed to no submitter",
              "nobody" in row.get("ChargeDescription", "").lower()
              or "no submitter" in row.get("ChargeDescription", "").lower(),
              row.get("ChargeDescription"))

    print("\n== columns the gateway cannot truthfully fill are empty, not faked")

    sample = rows.get(PLAIN_USER) or {}
    check("RegionId, ResourceId and SkuId are present-but-empty -- the gateway "
          "does not know its region, the monthly Hash has no per-resource "
          "rows, and an invented value would be exactly the dishonesty the "
          "roadmap item forbids",
          sample.get("RegionId", None) == "" and sample.get("ResourceId", None) == ""
          and sample.get("SkuId", None) == "",
          {k: sample.get(k) for k in ("RegionId", "ResourceId", "SkuId")})
    check("while ServiceName, ServiceCategory, ProviderName and "
          "BillingCurrency are filled",
          all(sample.get(k) for k in ("ServiceName", "ServiceCategory",
                                      "ProviderName", "BillingCurrency")),
          {k: sample.get(k) for k in ("ServiceName", "ServiceCategory",
                                      "ProviderName", "BillingCurrency")})

    print("\n== scoping: the CSV is the JSON report's view, per caller")

    _, _, own_csv = http("GET", "/api/showback?format=focus",
                         headers={"X-Forwarded-User": PLAIN_USER}, base=OGW)
    _, own_rows = rows_by_subaccount(own_csv)
    own_users = [sub for sub in own_rows
                 if sub not in ("", "anonymous", "other")]
    check("under AUTH_MODE=oauth a non-operator's CSV names no submitter but "
          "themselves -- the full CSV is the submitter list scoped_showback() "
          "exists to withhold",
          own_users == [PLAIN_USER], own_users)

    _, _, op_csv = http("GET", "/api/showback?format=focus",
                        headers={"X-Forwarded-User": OPERATOR}, base=OGW)
    _, op_rows = rows_by_subaccount(op_csv)
    check("while a SHOWBACK_OPERATORS member's CSV has every submitter's row",
          PLAIN_USER in op_rows and HOSTILE_USER in op_rows, sorted(op_rows))

finally:
    for proc in (fgw, ogw):
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all assertions passed")
