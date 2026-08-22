"""วัดเวลา close_popup + select_promptpay บนหน้า checkout จริง (ไม่กด Place Order)"""
import asyncio
import json
import time
from pathlib import Path

from playwright.async_api import async_playwright

from checkout_direct import (
    api_add_to_cart,
    api_mark_checkout_and_build_url,
    close_popup,
    select_promptpay,
)


async def main():
    session_file = "line_session.json"
    handle = "@thelandofvava"

    ok = await api_add_to_cart(1008243987, 27499188, 1, session_file, handle)
    print(f"add-to-cart: {ok}")
    url = await api_mark_checkout_and_build_url(session_file, handle, 1008243987, 27499188)
    print(f"checkout URL: {url}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=session_file)
        page = await context.new_page()

        t0 = time.perf_counter()
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)

        # Step C เดิม: ปิด popup (วัดเวลา)
        for i in range(3):
            ts = time.perf_counter()
            closed = await close_popup(page, "before-promptpay")
            print(f"close_popup #{i+1}: closed={closed} ({(time.perf_counter()-ts)*1000:.0f} ms)")
            if not closed:
                break

        # Step: เลือก PromptPay (วัดเวลา)
        ts = time.perf_counter()
        ok = await select_promptpay(page)
        print(f"select_promptpay: {ok} ({(time.perf_counter()-ts)*1000:.0f} ms)")
        print(f"รวมตั้งแต่ goto: {(time.perf_counter()-t0):.2f} s")

        # ยืนยันว่า aria-checked = true แต่ "ไม่กด" Place Order
        state = await page.evaluate("""
            () => {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                while (walker.nextNode()) {
                    if (walker.currentNode.textContent.trim().toLowerCase() !== 'promptpay') continue;
                    let el = walker.currentNode.parentElement;
                    for (let i = 0; i < 8 && el; i++) {
                        const ac = el.getAttribute('aria-checked');
                        if (ac !== null) return ac;
                        el = el.parentElement;
                    }
                    return 'not-found';
                }
                return 'no-text';
            }
        """)
        print(f"aria-checked = {state!r} (คาดหวัง 'true', ไม่กด Place Order)")

        await browser.close()


asyncio.run(main())
