"""Diagnostic: save the raw CAPTCHA screenshot from 3 fresh page loads to
check whether the canvas screenshot itself is actually changing between
loads, or whether it's the OCR that's stuck."""
import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
        )
        page = await context.new_page()

        out_dir = os.path.join(os.path.dirname(__file__), "..", "captcha", "diag")
        os.makedirs(out_dir, exist_ok=True)

        hashes = []
        import hashlib
        for i in range(3):
            await page.goto("https://kppp.karnataka.gov.in/#/portal/searchTender/live", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await page.wait_for_selector("canvas", timeout=5000)
            canvas = await page.query_selector("canvas")
            img_bytes = await canvas.screenshot()
            h = hashlib.md5(img_bytes).hexdigest()
            hashes.append(h)
            path = os.path.join(out_dir, f"captcha_{i+1}.png")
            with open(path, "wb") as f:
                f.write(img_bytes)
            print(f"[diag] attempt {i+1}: saved {path}, size={len(img_bytes)} bytes, md5={h}")

        print(f"[diag] all same image? {len(set(hashes)) == 1}")
        await browser.close()

asyncio.run(main())
