"""Debug payment selection page"""

import asyncio
from playwright.async_api import async_playwright

PRODUCT_URL = "https://shop.line.me/@thelandofvava/product/1008229810"
SESSION_FILE = "line_session.json"
SIZE         = "12m"


async def debug():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=SESSION_FILE)
        page    = await context.new_page()

        # 1. ไปซื้อสินค้า
        await page.goto(PRODUCT_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2_000)
        await page.get_by_role("button", name="Buy Now", exact=True).last.click()
        await page.get_by_role("button", name=SIZE, exact=True).wait_for(state="visible", timeout=5_000)
        await page.get_by_role("button", name=SIZE, exact=True).first.click()
        await page.wait_for_timeout(400)
        await page.get_by_role("button", name="Buy Now", exact=True).first.click()
        await page.wait_for_url(lambda url: "/product/" not in url, timeout=20_000)

        # 2. รอหน้า cart โหลดเสร็จ
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await page.wait_for_timeout(1_500)

        # 3. ปิด campaign modal (LINE POINTS popup)
        close_btn = page.locator("[data-testid='campaign-close-button']")
        try:
            await close_btn.wait_for(state="visible", timeout=5_000)
            await close_btn.click()
            print("ปิด campaign modal แล้ว")
            await page.wait_for_timeout(800)
        except Exception:
            print("ไม่พบ campaign modal")

        # 4. กด Place Order
        await page.get_by_role("button", name="Place Order").click(timeout=10_000)
        print("กด Place Order แล้ว")
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await page.wait_for_timeout(2_000)

        print("\nPayment page URL:", page.url)
        await page.screenshot(path="debug_payment.png", full_page=True)
        print("Screenshot: debug_payment.png")

        # 5. Dump all visible buttons
        buttons = page.locator("button")
        bc = await buttons.count()
        print(f"\n=== Buttons ({bc}) ===")
        for i in range(bc):
            b = buttons.nth(i)
            try:
                txt = (await b.inner_text()).strip()
                vis = await b.is_visible()
                dis = await b.is_disabled()
                cls = (await b.get_attribute("class") or "")[:60]
                if vis:
                    print(f"  [{i}] '{txt[:60]}' disabled={dis} | class={cls}")
            except Exception:
                pass

        # 6. ค้นหา PromptPay / พร้อมเพย์
        for kw in ["PromptPay", "Promptpay", "พร้อมเพย์", "promptpay", "QR"]:
            els = page.locator(f"*:has-text('{kw}')")
            ec  = await els.count()
            print(f"\n=== '{kw}' elements ({ec}) ===")
            for i in range(min(ec, 8)):
                el = els.nth(i)
                try:
                    tag = await el.evaluate("e => e.tagName")
                    txt = (await el.inner_text()).strip()[:80]
                    vis = await el.is_visible()
                    cls = (await el.get_attribute("class") or "")[:70]
                    tid = (await el.get_attribute("data-testid") or "")
                    if vis and txt:
                        print(f"  [{i}] <{tag}> '{txt}' | testid={tid} | class={cls}")
                except Exception:
                    pass

        input("\nกด Enter เพื่อปิด...")
        await browser.close()


asyncio.run(debug())
