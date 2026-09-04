# The brief

The six-slide deck one folder up, cut to what a mixed room retains: ten
slides, each opening with the question on the table and answering it in the
title, three or four bullets, one picture — the same hub-and-spoke diagram
drawn identically on slides 5, 8 and 9 with only the red highlight moving —
and a one-line takeaway at the foot. Three appendix slides hold the
comparison table, the cost ladder and the sources for Q&A.

Written for two readers at once: an engineer who knows text inference
(slide 2's "if you know vLLM" door: *the model is a deployment there, a part
pulled from the shelf here*) and a program manager new to the space (the other door: *a prompt box
is a vending machine, a graph is a production line*). The vocabulary is fixed on
slide 2 and never swapped afterwards: model (checkpoint), graph (workflow),
canvas (frontend), worker (pod).

**Leaner still:** [`index-lean.html`](index-lean.html) / [`comfyui-on-openshift-brief-lean.pdf`](comfyui-on-openshift-brief-lean.pdf) is the same ten slides with the chrome stripped — the question large, three or four short bullets, one picture, no appendix. Fill the rest by talking, or point at this deck and the repo.

*(For presenting: [`index.html`](index.html) is the live version, arrow keys
to navigate; [`comfyui-on-openshift-brief.pdf`](comfyui-on-openshift-brief.pdf)
is the same slides as a click-through PDF.)*

---

![Slide 1 — The market: a studio market, already large, repricing quarterly](slides/slide-1.png)

---

![Slide 2 — What this is, in your terms: a graph is a production line, a model is a part on the shelf](slides/slide-2.png)

---

![Slide 3 — Open source versus the commercial meter: the category's open-source leader, missing only its enterprise story](slides/slide-3.png)

---

![Slide 4 — How teams run it today: a card per person, idle most of the day, or one unauthenticated box](slides/slide-4.png)

---

![Slide 5 — The proposed state: same application, unchanged; only the silicon moved](slides/slide-5.png)

---

![Slide 6 — Efficient GPU spend: pooling is queueing, not division](slides/slide-6.png)

---

![Slide 7 — Scaling as the catalog grows: residency scales with catalog size, the pool scales with concurrency](slides/slide-7.png)

---

![Slide 8 — What the repo delivers: the production layer — queue, isolation, storage, scale](slides/slide-8.png)

---

![Slide 9 — Where vLLM Omni fits: two tiers, one stack; showback decides when a model moves](slides/slide-9.png)

---

![Slide 10 — Why Red Hat: the graph-input half of inference, on the platform we already sell](slides/slide-10.png)

---

Appendix, for the questions that follow:

![A1 — Against the alternatives](slides/slide-11.png)

![A2 — The cost ladder](slides/slide-12.png)

![A3 — Sources](slides/slide-13.png)

Every number is backed one folder up and in the repository: the cost model in
[`../../02-cost.md`](../../02-cost.md), the comparison table on the
[front page](../../../README.md), the catalog curve in
[`../../13-vllm-omni.md`](../../13-vllm-omni.md), the market case in
[`../../14-market.md`](../../14-market.md). The slide images and the PDF are
exported from `index.html` by [`export.py`](export.py) — rerun it after any
edit to the deck; `export.py lean` does the same for the lean variant.
