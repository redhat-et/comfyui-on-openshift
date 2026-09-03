#!/usr/bin/env python3
"""
The monthly savings report (docs/10-roadmap.md, N3): the pool's business
case, assembled from the accounting it already keeps. Reads one showback
report — live from a gateway, or a captured JSON file — and prints markdown
that pastes into Slack: total spend, cost per render, utilization, and the
card-per-person counterfactual from docs/02-cost.md.

Two sources, because the report is most needed when the cluster is gone:

    # live, port-forwarded or in-cluster
    scripts/savings-report.py --gateway http://localhost:8000

    # from the capture the teardown habit already saves
    # (oc exec ... curl -s localhost:8000/api/showback > showback-2026-08.json)
    scripts/savings-report.py --from-json showback-2026-08.json

Stdlib only, deliberately: it must run on the same laptop that runs the
teardown capture, with no venv and no cluster.

What the numbers are, so the report can be argued with rather than about:

  - SPEND is held GPU time (Q4's definition: wall-clock a worker held the
    card, checkpoint loads and failures included) times one all-in
    $/GPU-hour (--rate; the default is docs/02's g6.xlarge figure). Time
    held by workers that died mid-job is in pool spend and named on its own
    line, but excluded from cost per render — no submitter was billed for
    it, and its measurement is padded by detection lag.
  - COST PER RENDER is billed spend over billed jobs, the counter the
    accrual writes beside the seconds. Failed and cancelled jobs count,
    because Q4 bills them. A capture from before the counter existed gets
    "unavailable", not a fabricated denominator.
  - THE COUNTERFACTUAL is headcount x --card-month (docs/02: one always-on
    L4 is ~$713/month), prorated by how much of the period has elapsed so a
    mid-month run is not compared against a whole month of dedicated cards.
    Headcount defaults to the identities the report names — a floor, and
    the report says so — because lurkers who rendered nothing this month
    would still have needed a card each; pass --headcount for the real
    team size.
  - UTILIZATION is held hours over headcount x elapsed hours: the duty
    cycle those dedicated cards would have run at, docs/02's own idle-tax
    framing. It is not a measure of how busy the POOL was — the pool's
    whole point is that its card count is not the headcount.
"""

import argparse
import calendar
import json
import sys
import time
import urllib.request


def fetch_json(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def load_report(args):
    """(report, stats_or_None). stats is the live gateway's /api/stats —
    the point-in-time queue picture a captured file cannot carry."""
    if args.from_json:
        with open(args.from_json) as f:
            report = json.load(f)
        if not isinstance(report, dict) or "period" not in report:
            sys.exit(f"{args.from_json} is not a captured /api/showback "
                     f"report (no 'period' key)")
        return report, None

    base = args.gateway.rstrip("/")
    query = f"?period={args.period}" if args.period else ""
    report = fetch_json(f"{base}/api/showback{query}")

    try:
        stats = fetch_json(f"{base}/api/stats")
    except Exception:  # noqa: BLE001 — stats are a garnish, not the report
        stats = None

    return report, stats


def period_bounds(period):
    """[start, end) of a YYYY-MM UTC month, as epoch seconds."""
    year, month = int(period[:4]), int(period[5:7])
    next_year = year + (1 if month == 12 else 0)
    next_month = 1 if month == 12 else month + 1

    return (calendar.timegm((year, month, 1, 0, 0, 0)),
            calendar.timegm((next_year, next_month, 1, 0, 0, 0)))


def seconds_of(report, key):
    try:
        return max(0.0, float(report.get(key, 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def usd(amount):
    return f"${amount:,.2f}"


def build(report, stats, rate, card_month, headcount_arg, now=None):
    now = time.time() if now is None else now
    period = report["period"]
    start, end = period_bounds(period)

    # How much of the period the numbers cover. For a past month this is
    # the whole of it; mid-month it prorates the counterfactual and the
    # utilization base, so the comparison is per elapsed hour on both sides.
    elapsed = max(0.0, min(now, end) - start)
    fraction = elapsed / (end - start) if end > start else 1.0

    users = report.get("users") or {}
    anonymous = seconds_of(report, "anonymous_gpu_seconds")
    other = seconds_of(report, "other_gpu_seconds")
    excluded = seconds_of(report, "excluded_gpu_seconds")

    if "users_total_gpu_seconds" in report:
        users_seconds = seconds_of(report, "users_total_gpu_seconds")
    else:
        # An older capture without the precomputed total: sum the rows,
        # tolerating the odd unparseable value the way showback_report does.
        users_seconds = sum(seconds_of({"v": v}, "v") for v in users.values())

    billed_seconds = users_seconds + anonymous + other

    billed_hours = billed_seconds / 3600.0
    excluded_hours = excluded / 3600.0
    held_hours = billed_hours + excluded_hours

    billed_spend = billed_hours * rate
    pool_spend = held_hours * rate

    jobs = report.get("billed_jobs")
    try:
        jobs = int(jobs)
    except (TypeError, ValueError):
        jobs = None

    derived_headcount = len(users) + (1 if anonymous > 0 else 0)
    headcount = headcount_arg or derived_headcount

    counterfactual = headcount * card_month * fraction
    savings = counterfactual - pool_spend
    utilization = (held_hours / (headcount * (elapsed / 3600.0))
                   if headcount > 0 and elapsed > 0 else None)

    lines = [f"# ComfyUI pool — savings report, {period}", ""]

    lines.append(f"**Pool spend: {usd(pool_spend)}** — {held_hours:,.1f} "
                 f"GPU-hours held at ${rate}/GPU-hour")

    if jobs and billed_seconds > 0:
        lines.append(f"- billed to submitters: {usd(billed_spend)} across "
                     f"{jobs} renders — **{usd(billed_spend / jobs)} per "
                     f"render** (completed or not: a failed job held the "
                     f"card too)")
    elif billed_seconds > 0:
        lines.append(f"- billed to submitters: {usd(billed_spend)} — cost "
                     f"per render unavailable (this period's data predates "
                     f"the billed-jobs counter)")
    else:
        lines.append("- billed to submitters: $0.00 — nothing ran this "
                     "period")

    if excluded > 0:
        lines.append(f"- held by workers that died mid-job, billed to "
                     f"nobody: {usd(excluded_hours * rate)} "
                     f"(excluded_gpu_seconds — this line climbing is a "
                     f"cluster-health signal, not a billing one)")

    lines.append("")
    prorate = (f", prorated to the {fraction * 100:.0f}% of the period "
               f"elapsed" if fraction < 1.0 else "")
    lines.append(f"**The card-per-person counterfactual: "
                 f"{usd(counterfactual)}** — {headcount} dedicated cards at "
                 f"{usd(card_month)}/month each{prorate}")
    lines.append(f"**Savings versus a card per person: {usd(savings)}**")

    if utilization is not None:
        lines.append("")
        lines.append(f"**Those dedicated cards would have run at "
                     f"{utilization * 100:.1f}% utilization** — idle the "
                     f"other {(1 - utilization) * 100:.1f}%, which is the "
                     f"idle tax the pool exists to remove (docs/02-cost.md)")

    if stats is not None:
        lines.append("")
        lines.append(f"Queue right now: depth "
                     f"{stats.get('queue_depth', '?')}, estimated wait "
                     f"{stats.get('estimated_wait_seconds', '?')}s, "
                     f"{stats.get('workers_registered', '?')} worker(s) — "
                     f"point-in-time, from the live gateway's /api/stats")

    lines.append("")
    lines.append("Caveats:")

    if headcount_arg:
        lines.append(f"- headcount {headcount} was set with --headcount")
    else:
        lines.append(f"- headcount {headcount} is derived from the "
                     f"identities in the report — a floor, since anyone who "
                     f"rendered nothing this month is invisible to it but "
                     f"would still have needed a card. --headcount overrides "
                     f"it.")

    lines.append("- identities are client-supplied under AUTH_MODE=none — "
                 "this is attribution for a cost conversation, not an audit "
                 "trail")

    if report.get("truncated"):
        lines.append("- the report is TRUNCATED: the identity cap sent at "
                     "least one submitter's time to the shared `other` "
                     "bucket, so per-person figures are incomplete")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="The pool's monthly savings report, as Slack-pasteable "
                    "markdown, from showback data.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--gateway",
                        help="gateway base URL, e.g. http://localhost:8000")
    source.add_argument("--from-json",
                        help="a captured /api/showback JSON file")
    parser.add_argument("--period",
                        help="UTC month YYYY-MM (live source only; a capture "
                             "carries its own)")
    parser.add_argument("--rate", type=float, default=0.976,
                        help="all-in $/GPU-hour (default: 0.976, docs/02's "
                             "g6.xlarge)")
    parser.add_argument("--card-month", type=float, default=713.0,
                        help="$/month for one dedicated always-on card "
                             "(default: 713, docs/02's L4)")
    parser.add_argument("--headcount", type=int,
                        help="team size for the counterfactual (default: "
                             "identities in the report — a floor)")
    args = parser.parse_args()

    try:
        report, stats = load_report(args)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"could not read the showback report: {exc}")

    print(build(report, stats, args.rate, args.card_month, args.headcount),
          end="")


if __name__ == "__main__":
    main()
