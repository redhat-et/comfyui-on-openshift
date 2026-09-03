#!/usr/bin/env python3
"""Regenerate slides/slide-N.png and the PDF from index.html.

Every edit to index.html owes a rerun of this script: the folder README
embeds the PNGs and GitHub renders the PDF on click, so a stale export
shows the old deck to exactly the people the deck exists for.

The deck itself provides the capture surface — loading index.html with the
fragment `#3x` opens slide 3 in export mode (nav hidden, stage at scale 1,
videos pinned to their poster frame); this script just walks the fragments
and screenshots the stage at 2x.

Needs:  pip install playwright img2pdf
        playwright install chromium-headless-shell
Run:    python3 docs/pitch/export.py   (from anywhere)
"""
import pathlib

import img2pdf
from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
SLIDES = HERE / "slides"
COUNT = 6  # keep in step with the <section class="slide"> count in index.html

pngs = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(
        viewport={"width": 1400, "height": 800}, device_scale_factor=2
    )
    for i in range(1, COUNT + 1):
        page.goto(f"file://{HERE}/index.html#{i}x")
        # goto() to the same document with a new fragment does not reload,
        # and the export-mode hook runs at load — so force one.
        page.reload()
        page.wait_for_load_state("networkidle")  # webfonts, poster frame
        page.wait_for_timeout(300)
        out = SLIDES / f"slide-{i}.png"
        page.locator("#stage").screenshot(path=str(out))
        pngs.append(out)
        print(f"exported {out.name}")
    browser.close()

pdf = HERE / "comfyui-on-openshift-pitch.pdf"
pdf.write_bytes(img2pdf.convert([str(x) for x in pngs]))
print(f"wrote {pdf.name} ({pdf.stat().st_size // 1024} KB)")
