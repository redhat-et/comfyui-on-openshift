# The briefing

Six slides. Each opens with the questions on the table and answers them:
the current state of ComfyUI teams, the proposed state, what this repository
does to enable it, the pool running live on a laptop, the one-sentence
economics — *residency scales with catalog size; the pool scales with
concurrency* — and where vLLM Omni fits.

*(GitHub renders this page when you browse the folder — the deck below is
the deck. For presenting: [`index.html`](index.html) is the live version,
arrow keys to navigate; [`comfyui-on-openshift-pitch.pdf`](comfyui-on-openshift-pitch.pdf)
is the same slides as a click-through PDF.)*

---

![Slide 1 — The current state: ComfyUI is the professionals' standard, run today as a card per person or a meter per seat](slides/slide-1.png)

---

![Slide 2 — The proposed state: same ComfyUI, unchanged; the GPUs move to a pooled, scale-to-zero cluster](slides/slide-2.png)

---

![Slide 3 — What the work does: the production layer neither stock ComfyUI nor the bridge provides, built and merged](slides/slide-3.png)

---

![Slide 4 — The pool, live on a laptop: make demo-local runs the real gateway, queue and workers on the machine's own GPU, and this render went through them](slides/slide-4.png)

---

![Slide 5 — The economics in one sentence: residency scales with catalog size, the pool scales with concurrency; the catalog curve and every supporting argument](slides/slide-5.png)

---

![Slide 6 — Where vLLM Omni fits: the engine is the throughput tier, the pool is the flexibility tier, and the asks](slides/slide-6.png)

---

Slide 4 also exists in motion:
[`demo-local-pool.mp4`](demo-local-pool.mp4) is twenty seconds of the real
thing — two browser panes queueing against the two-worker pool on one
laptop, progress streaming over WebSockets, two different renders landing.
`make demo-local` reproduces it on any machine with a GPU, Apple silicon
included.

Every number on these slides is backed in this repository: the cost model in
[`../02-cost.md`](../02-cost.md), the comparison table on the
[front page](../../README.md), the serving-tier arithmetic in
[`../13-vllm-omni.md`](../13-vllm-omni.md), and the market case in
[`../14-market.md`](../14-market.md). The slide images are exported from
`index.html`; regenerate them after editing it.
