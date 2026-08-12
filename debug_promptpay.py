"""Find exact PromptPay element using JS evaluation"""

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

        # ไปถึง payment page
        await page.goto(PRODUCT_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2_000)
        await page.get_by_role("button", name="Buy Now", exact=True).last.click()
        await page.get_by_role("button", name=SIZE, exact=True).wait_for(state="visible", timeout=5_000)
        await page.get_by_role("button", name=SIZE, exact=True).first.click()
        await page.wait_for_timeout(400)
        await page.get_by_role("button", name="Buy Now", exact=True).first.click()
        await page.wait_for_url(lambda url: "/product/" not in url, timeout=20_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await page.wait_for_timeout(1_500)

        # ปิด campaign modal
        close_btn = page.locator("[data-testid='campaign-close-button']")
        try:
            await close_btn.wait_for(state="visible", timeout=5_000)
            await close_btn.click()
            await page.wait_for_timeout(800)
        except Exception:
            pass

        # กด Place Order
        await page.get_by_role("button", name="Place Order").click(timeout=10_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await page.wait_for_timeout(2_000)

        # ใช้ JS หา elements ที่มี text "PromptPay" โดยตรง (leaf/specific)
        result = await page.evaluate("""
            () => {
                const walker = document.createTreeWalker(
                    document.body,
                    NodeFilter.SHOW_TEXT,
                    null
                );
                const found = [];
                let node;
                while (node = walker.nextNode()) {
                    if (node.textContent.trim() === 'PromptPay') {
                        const el = node.parentElement;
                        const parent = el.parentElement;
                        found.push({
                            tag: el.tagName,
                            class: el.className,
                            testid: el.getAttribute('data-testid') || '',
                            parentTag: parent.tagName,
                            parentClass: parent.className,
                            parentTestid: parent.getAttribute('data-testid') || '',
                            outerHTML: el.outerHTML.substring(0, 300)
                        });
                    }
                }
                return found;
            }
        """)
        print(f"\n=== Text nodes with exact 'PromptPay' ===")
        for i, item in enumerate(result):
            print(f"  [{i}] <{item['tag']}> class='{item['class'][:60]}'")
            print(f"       testid='{item['testid']}'")
            print(f"       parent=<{item['parentTag']}> class='{item['parentClass'][:60]}'")
            print(f"       parent testid='{item['parentTestid']}'")
            print(f"       HTML: {item['outerHTML'][:200]}")
            print()

        # ลอง click ด้วย Playwright get_by_text exact
        pp = page.get_by_text("PromptPay", exact=True)
        cnt = await pp.count()
        print(f"\nget_by_text('PromptPay', exact=True) count: {cnt}")
        for i in range(cnt):
            el = pp.nth(i)
            tag  = await el.evaluate("e => e.tagName")
            cls  = await el.get_attribute("class") or ""
            vis  = await el.is_visible()
            print(f"  [{i}] <{tag}> visible={vis} class={cls[:60]}")

        input("\nกด Enter เพื่อปิด...")
        await browser.close()


asyncio.run(debug())
