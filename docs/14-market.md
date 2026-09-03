# The market, honestly

Where this sits commercially: the category, who is forced toward
self-hosting, how fast the ground moves, whose incentives align, what could
kill it, and the research that would sharpen all of the above. Written for
the person deciding whether this corner is worth standing in.

## The category and its meter

Professional AI image/video creation is sold today as credit-metered SaaS —
Higgsfield, Krea, Runway, Midjourney — at **$79–215/seat/month** on team
tiers, with video billed by the second (~$0.39/second at the top tier: a
$35/month plan is about ninety seconds of output). A thirty-seat studio
pays **$28,000–77,000 a year**, and every prompt, model and output lives in
the vendor's cloud.

That per-seat meter is a price umbrella, and open source undercutting a
price umbrella on top of a supported platform is not a novel strategy — it
is the house strategy. ComfyUI is the open-source leader of the category
($500M valuation, 60,000+ community nodes, in production at Amazon Studios,
Apple, Autodesk, Netflix, Nike, Tencent, Ubisoft per comfy.org); this repo
is its missing enterprise deployment story. The pool serves ten designers
for $120–950/month plus the $775/month cluster floor — under the umbrella
with room to spare.

## The segment that cannot use SaaS at any price

Studios working under NDA — unannounced games, film pre-release, regulated
brands — cannot send frames to a vendor cloud regardless of cost. Leaked
pre-release content is a career event, not a line item. This segment is not
price-sensitive; it is **compliance-captive**, already buys private
infrastructure, and is therefore the natural beachhead. The air-gapped
install path on the roadmap exists for exactly these buyers, and no SaaS
competitor can follow them there structurally.

## How fast the ground moves

One commercial comp, three clocks: Higgsfield went **$1.3B → $5.4B
valuation in seven months**, **$20M → $700M annualized revenue in twelve**,
and reported **42× agentic-usage growth in three** (Reuters/FT/PR Newswire,
Aug 2026). Two readings matter here. First: demand is real and repricing
quarterly, so infrastructure decisions should assume the next reset, not
this one. Second: if agent-initiated generation keeps compounding, the
"multi-user pool" becomes a **multi-agent pool** — and agents wielding an
arbitrary-Python tool need the unreachable-worker security model *more*
than humans do. Same architecture, second market.

## Whose incentives align

- **Comfy Org** raised $30M explicitly to build enterprise and cloud
  muscle; a hardened reference deployment on OpenShift is complementary to
  their SaaS, not competitive with it.
- **NVIDIA** benefits from every GPU Operator showcase that turns idle
  desktop cards into datacenter demand.
- **Cloud providers** bill the underlying managed clusters and GPU hours.

Three parties whose interests point at this repo existing, before any
internal argument for it is made.

## What could kill this, and the standing answers

- **Comfy Org's own cloud goes enterprise.** Theirs is SaaS in their cloud;
  this is their tool in *your* VPC. Different buyer constraint (see the NDA
  segment). Partner surface, not collision.
- **Serving engines grow workflow-graph intake.** The promotion rule in
  `docs/13-vllm-omni.md` already assumes engines absorb the *head* of the
  usage curve; the 60,000-node long tail is a product category, not a
  feature gap, and rebuilding it inside an engine is a poor trade while the
  runtime already exists and runs here.
- **GPU sharing (MIG, time-slicing) changes the economics.** It changes
  them for the pool too — finer-grained sharing makes pooling *more*
  efficient, not less. (Time-slicing was evaluated and declined for other
  reasons: `docs/10-roadmap.md`.)
- **Model catalogs consolidate to one giant model.** The strongest version
  of the risk. Watch the showback concentration data: if one model comes to
  dominate real usage, promote it to a resident engine — the architecture's
  own answer — and the pool remains the on-ramp and experiment bench.

## Research worth running

Each of these is cheap and produces a number the deck currently asserts
by citation:

1. **Job-posting count**: "ComfyUI" mentions in creative/technical job
   listings, tracked quarterly — a first-party demand curve.
2. **Community demand mining**: count "how do we share a GPU across the
   team" threads in the ComfyUI Discord and subreddit; those posts are the
   ICP describing this repo in their own words.
3. **Win/loss interviews**: five studios on RunPod/ComfyDeploy-style
   hosting — what pushed them off self-hosting (the likely answer, ops
   burden, is precisely what a managed OpenShift underneath removes).
4. **A duty-cycle field study**: instrument one real design team's month
   (showback does this automatically) and replace the README's modeled
   4–26% duty with measured numbers.

## Sources

- Vendor team-tier pricing pages, 2026; Runway credit schedule.
- ComfyUI raise: Craft Ventures round, April 2026; adopter list: comfy.org.
- Higgsfield figures: Reuters, Financial Times, PR Newswire, August 2026.
- Cost model: `docs/02-cost.md`; serving comparison: `docs/13-vllm-omni.md`.
