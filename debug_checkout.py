"""Debug checkout page — dump payment option elements"""

import asyncio
from playwright.async_api import async_playwright

PRODUCT_URL = "https://shop.line.me/@thelandofvava/product/1008229810"
SESSION_FILE = "line_session.json"
SIZE        = "12m"


async def debug():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=SESSION_FILE)
        page    = await context.new_page()

        # ไปที่ product page แล้วซื้อเพื่อเข้าหน้า checkout
        await page.goto(PRODUCT_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2_000)

        await page.get_by_role("button", name="Buy Now", exact=True).last.click()
        await page.get_by_role("button", name=SIZE, exact=True).wait_for(state="visible", timeout=5_000)
        await page.get_by_role("button", name=SIZE, exact=True).first.click()
        await page.wait_for_timeout(400)
        await page.get_by_role("button", name="Buy Now", exact=True).first.click()
        await page.wait_for_url(lambda url: "/product/" not in url, timeout=20_000)

        print("Checkout URL:", page.url)
        # รอ spinner หายและ content โหลดเสร็จ
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await page.wait_for_timeout(2_000)
        await page.screenshot(path="debug_checkout.png", full_page=True)
        print("Screenshot: debug_checkout.png (cart page)")

        # Dump all buttons on cart page
        buttons = page.locator("button")
        bc = await buttons.count()
        print(f"\n=== Buttons on cart page ({bc}) ===")
        for i in range(bc):
            b = buttons.nth(i)
            try:
                txt = (await b.inner_text()).strip()
                vis = await b.is_visible()
                dis = await b.is_disabled()
                if vis:
                    print(f"  [{i}] '{txt[:60]}' disabled={dis}")
            except Exception:
                pass

        # กด proceed/checkout button
        proceed_names = ["Proceed to Checkout", "Checkout", "ดำเนินการต่อ", "ต่อไป", "Place Order", "สั่งซื้อ"]
        for name in proceed_names:
            btn = page.get_by_role("button", name=name)
            if await btn.count() > 0 and await btn.first.is_visible():
                print(f"\nกด proceed: '{name}'")
                # screenshot ก่อนกด เพื่อดู modal ที่บัง
                await page.screenshot(path="debug_before_place_order.png", full_page=True)
                print("Screenshot: debug_before_place_order.png")
                modal_html = await page.locator("#modal-container").inner_html()
                print("Modal HTML:", modal_html[:2000])
                # รอ overlay หาย หรือ dismiss มัน
                overlay = page.locator("div[data-v-7bd9e4a8]")
                try:
                    await overlay.wait_for(state="hidden", timeout=8_000)
                    print("Overlay หายแล้ว")
                except Exception:
                    print("Overlay ยังอยู่ — click เพื่อปิด")
                    try:
                        await overlay.click(force=True, timeout=3_000)
                        await page.wait_for_timeout(1_000)
                    except Exception:
                        pass
                await btn.first.click()
                await page.wait_for_load_state("networkidle", timeout=15_000)
                await page.wait_for_timeout(2_000)
                break
        else:
            # ลอง locator ที่กว้างขึ้น
            proceed_loc = page.locator("button:has-text('Checkout'), button:has-text('ต่อไป'), button:has-text('Next'), a:has-text('Checkout')")
            if await proceed_loc.count() > 0:
                txt = await proceed_loc.first.inner_text()
                print(f"\nกด proceed locator: '{txt.strip()}'")
                await proceed_loc.first.click()
                await page.wait_for_load_state("networkidle", timeout=15_000)
                await page.wait_for_timeout(2_000)

        print("\nAfter proceed URL:", page.url)
        await page.screenshot(path="debug_checkout2.png", full_page=True)
        print("Screenshot: debug_checkout2.png (after proceed)")

        # Dump buttons on next page
        buttons2 = page.locator("button")
        bc2 = await buttons2.count()
        print(f"\n=== Buttons after proceed ({bc2}) ===")
        for i in range(bc2):
            b = buttons2.nth(i)
            try:
                txt = (await b.inner_text()).strip()
                vis = await b.is_visible()
                dis = await b.is_disabled()
                if vis and txt:
                    print(f"  [{i}] '{txt[:60]}' disabled={dis}")
            except Exception:
                pass

        # Dump ทุก input[type=radio] และ label
        radios = page.locator("input[type='radio']")
        rc = await radios.count()
        print(f"\n=== Radio inputs ({rc}) ===")
        for i in range(rc):
            r = radios.nth(i)
            val  = await r.get_attribute("value") or ""
            name = await r.get_attribute("name") or ""
            checked = await r.is_checked()
            print(f"  [{i}] name={name} value={val} checked={checked}")

        # Dump divs/labels ที่มีคำว่า prompt / pay / พร้อมเพย์
        keywords = ["prompt", "pay", "พร้อมเพย์", "payment", "PromptPay", "qr"]
        for kw in keywords:
            els = page.locator(f"*:has-text('{kw}')")
            ec  = await els.count()
            if ec:
                print(f"\n=== Elements containing '{kw}' ({ec}) ===")
                for i in range(min(ec, 10)):
                    el = els.nth(i)
                    try:
                        tag  = await el.evaluate("e => e.tagName")
                        txt  = (await el.inner_text()).strip()[:60]
                        cls  = (await el.get_attribute("class") or "")[:60]
                        vis  = await el.is_visible()
                        if vis and txt:
                            print(f"  [{i}] <{tag}> '{txt}' | class={cls}")
                    except Exception:
                        pass

        input("\nกด Enter เพื่อปิด...")
        await browser.close()


asyncio.run(debug())
