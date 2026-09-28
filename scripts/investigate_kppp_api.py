"""Investigation script: find KPPP's real pagination API. Uses the fixed
captcha-refresh-click approach (a.cpt-btn.reload between attempts)."""
import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright
from captcha.solver import solve_captcha_local

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
        )
        page = await context.new_page()

        captured = []
        def on_response(resp):
            url = resp.url
            if resp.request.method == "POST" or ("api" in url.lower() and "kppp" in url.lower()):
                captured.append((resp.request.method, url))
        page.on("response", on_response)

        await page.goto("https://kppp.karnataka.gov.in/#/portal/searchTender/live", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        captcha_solved = False
        MAX_ATTEMPTS = 10
        for attempt in range(MAX_ATTEMPTS):
            print(f"[investigate] === attempt {attempt+1}/{MAX_ATTEMPTS} ===")
            if attempt > 0:
                reload_btn = await page.query_selector("a.cpt-btn.reload")
                if reload_btn:
                    await reload_btn.click()
                    await page.wait_for_timeout(1000)
            try:
                await page.wait_for_selector("canvas", timeout=5000)
                canvas = await page.query_selector("canvas")
                if not canvas:
                    continue
                img_bytes = await canvas.screenshot()
                solution = solve_captcha_local(img_bytes)
                print(f"[investigate] local OCR solution: {solution}")
                if not solution or len(solution) < 3:
                    continue
                captcha_input = await page.query_selector("input[type='text']")
                if captcha_input:
                    await captcha_input.fill("")
                    await captcha_input.type(solution, delay=100)
                    await page.wait_for_timeout(500)
                check_btn = await page.query_selector("input[type='button'], input[value='Check']")
                if check_btn:
                    await check_btn.click()
                    await page.wait_for_timeout(2000)
                page_text = await page.inner_text("body")
                if "Please Enter correct Captcha" in page_text:
                    print("[investigate] wrong captcha, retry")
                else:
                    captcha_solved = True
                    break
            except Exception as e:
                print(f"[investigate] captcha error: {e}")

        print(f"[investigate] captcha_solved={captcha_solved}")
        if not captcha_solved:
            await browser.close()
            return

        await page.wait_for_timeout(2000)
        cat_select = await page.query_selector("#categorySelect")
        if cat_select:
            await cat_select.select_option(label="Goods")
            await page.wait_for_timeout(300)
        selects = await page.query_selector_all("select")
        if len(selects) >= 2:
            await selects[1].select_option(index=1)
            await page.wait_for_timeout(300)

        search_btn = await page.query_selector("button:has-text('Search')")
        if search_btn:
            await search_btn.click()
            await page.wait_for_timeout(4000)
            print("[investigate] search submitted")

        print("[investigate] captured after search:")
        for m, u in captured:
            print(f"  {m} {u}")
        captured.clear()

        next_link = await page.query_selector("li.pagination-next a, a[aria-label='Next']")
        if next_link:
            await next_link.click()
            await page.wait_for_timeout(3000)
            print("[investigate] captured after Next click:")
            for m, u in captured:
                print(f"  {m} {u}")
        else:
            print("[investigate] no Next link found")

        await browser.close()

asyncio.run(main())
