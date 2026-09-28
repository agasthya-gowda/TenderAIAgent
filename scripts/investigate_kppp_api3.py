"""Confirm the 'location' field's format (name vs id) when Procuring Entity
is set via the real dropdown, plus check the Next-page request body."""
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
        async def on_response(resp):
            if "search-eproc-tenders" in resp.url:
                req = resp.request
                captured.append({"url": resp.url, "post_data": req.post_data})
        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        await page.goto("https://kppp.karnataka.gov.in/#/portal/searchTender/live", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        captcha_solved = False
        for attempt in range(10):
            if attempt > 0:
                reload_btn = await page.query_selector("a.cpt-btn.reload")
                if reload_btn:
                    await reload_btn.click()
                    await page.wait_for_timeout(1000)
            try:
                await page.wait_for_selector("canvas", timeout=5000)
                canvas = await page.query_selector("canvas")
                img_bytes = await canvas.screenshot()
                solution = solve_captcha_local(img_bytes)
                print(f"[investigate3] OCR: {solution}")
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
                if "Please Enter correct Captcha" not in page_text:
                    captcha_solved = True
                    break
            except Exception as e:
                print(f"[investigate3] error: {e}")

        print(f"[investigate3] captcha_solved={captcha_solved}")
        if not captcha_solved:
            await browser.close()
            return

        await page.wait_for_timeout(2000)
        cat_select = await page.query_selector("#categorySelect")
        if cat_select:
            await cat_select.select_option(label="Goods")
            await page.wait_for_timeout(500)
        selects = await page.query_selector_all("select")
        if len(selects) >= 2:
            await selects[1].select_option(index=1)
            await page.wait_for_timeout(300)

        entity_select = await page.query_selector("#departmentstatusSelect")
        if entity_select:
            options = await entity_select.evaluate("el => Array.from(el.options).slice(0,5).map(o => o.text)")
            print(f"[investigate3] first few entity options: {options}")
            try:
                await entity_select.select_option(index=1)
                await page.wait_for_timeout(500)
                selected = await entity_select.evaluate("el => el.options[el.selectedIndex].text")
                print(f"[investigate3] selected entity: {selected}")
            except Exception as e:
                print(f"[investigate3] entity select error: {e}")

        search_btn = await page.query_selector("button:has-text('Search')")
        if search_btn:
            await search_btn.click()
            await page.wait_for_timeout(5000)

        print(f"[investigate3] captured {len(captured)} responses")
        for c in captured[:1]:
            print(f"[investigate3] URL: {c['url']}")
            print(f"[investigate3] POST DATA: {c['post_data']}")

        # Now click Next to see the page=1 request body
        captured.clear()
        next_link = await page.query_selector("li.pagination-next a, a[aria-label='Next']")
        if next_link:
            await next_link.click()
            await page.wait_for_timeout(3000)
            for c in captured[:1]:
                print(f"[investigate3] NEXT URL: {c['url']}")
                print(f"[investigate3] NEXT POST DATA: {c['post_data']}")
        else:
            print("[investigate3] no next link (maybe <20 results for this entity)")

        await browser.close()

asyncio.run(main())
