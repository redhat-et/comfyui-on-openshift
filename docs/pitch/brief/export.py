#!/usr/bin/env python3
"""Regenerate slides/slide-N.png and the PDF from index.html (the brief).

Same capture surface as ../export.py: index.html#Nx opens slide N in export
mode (nav hidden, stage at scale 1); this walks the fragments and screenshots
the stage at 2x. Ten slides in the arc, three appendix slides after it.

Needs:  pip install playwright img2pdf
        playwright install chromium
Run:    python3 docs/pitch/brief/export.py        (the brief: index.html)
        python3 docs/pitch/brief/export.py lean   (the lean brief: index-lean.html)
"""
import pathlib
import sys

import img2pdf
from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
VARIANT = sys.argv[1] if len(sys.argv) > 1 else ""
SUFFIX = f"-{VARIANT}" if VARIANT else ""
HTML = HERE / f"index{SUFFIX}.html"
SLIDES = HERE / f"slides{SUFFIX}"
COUNT = {"": 13, "lean": 10}[VARIANT]  # keep in step with the <section class="slide"> counts

pngs = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 800}, device_scale_factor=2)
    for i in range(1, COUNT + 1):
        page.goto(f"file://{HTML}#{i}x")
        page.reload()  # same-document fragment change does not re-run the load hook
        page.wait_for_load_state("networkidle")  # webfonts
        page.wait_for_timeout(300)
        out = SLIDES / f"slide-{i}.png"
        page.locator("#stage").screenshot(path=str(out))
        pngs.append(out)
        print(f"exported {out.name}")
    browser.close()

pdf = HERE / f"comfyui-on-openshift-brief{SUFFIX}.pdf"
pdf.write_bytes(img2pdf.convert([str(x) for x in pngs]))
print(f"wrote {pdf.name} ({pdf.stat().st_size // 1024} KB)")
