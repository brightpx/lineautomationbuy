"""Debug script — ถ่าย screenshot และ dump button/text หลัง Buy Now modal เปิด"""

import asyncio
from playwright.async_api import async_playwright

PRODUCT_URL = "https://shop.line.me/@thelandofvava/product/1008229810"
SESSION_FILE = "line_session.json"


async def debug():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=SESSION_FILE)
        page = await context.new_page()

        await page.goto(PRODUCT_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2_000)

        # Screenshot ก่อนกด
        await page.screenshot(path="debug_before.png", full_page=False)
        print("Screenshot saved: debug_before.png")

        # กด Buy Now
        buy_btn = page.locator("button:has-text('Buy Now'), button:has-text('ซื้อเลย')")
        print(f"Buy Now buttons found: {await buy_btn.count()}")
        await buy_btn.first.click()
        await page.wait_for_timeout(2_000)

        # Screenshot หลังกด
        await page.screenshot(path="debug_after_click.png", full_page=False)
        print("Screenshot saved: debug_after_click.png")

        # Dump button texts ทั้งหมด
        buttons = page.locator("button")
        count = await buttons.count()
        print(f"\n=== Buttons ({count}) ===")
        for i in range(count):
            b = buttons.nth(i)
            try:
                txt = (await b.inner_text()).strip()
                visible = await b.is_visible()
                disabled = await b.is_disabled()
                cls = (await b.get_attribute("class") or "")[:60]
                if txt:
                    print(f"  [{i}] '{txt}' | visible={visible} disabled={disabled} | class={cls}")
            except Exception:
                pass

        # Dump div/span ที่มีข้อความสั้น (น่าจะเป็นไซซ์)
        size_candidates = page.locator(
            "div[class*='option'], div[class*='variation'], div[class*='variant'], "
            "li[class*='option'], li[class*='variation'], span[class*='size']"
        )
        sc = await size_candidates.count()
        print(f"\n=== Size/Variation candidates ({sc}) ===")
        for i in range(sc):
            el = size_candidates.nth(i)
            try:
                txt = (await el.inner_text()).strip()
                visible = await el.is_visible()
                tag = await el.evaluate("el => el.tagName")
                cls = (await el.get_attribute("class") or "")[:80]
                if txt:
                    print(f"  [{i}] <{tag}> '{txt[:40]}' | visible={visible} | class={cls}")
            except Exception:
                pass

        print("\nURL:", page.url)
        input("\nกด Enter เพื่อปิด...")
        await browser.close()


asyncio.run(debug())
