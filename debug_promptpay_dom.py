"""
Dump DOM ส่วน payment ที่หน้า checkout/cart — read-only ไม่กด Place Order
เทียบ state ก่อน/หลังคลิก PromptPay 1 ครั้ง เพื่อหาว่า attribute ไหนเปลี่ยน
"""

import asyncio
import json
import sys
from pathlib import Path
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8")

_cfg         = json.loads(Path("config.json").read_text(encoding="utf-8"))
PRODUCT_URL  = _cfg["product_url"]
SIZES        = _cfg["preferred_sizes"]
SESSION_FILE = _cfg["session_file"]
OUT_FILE     = "debug_promptpay_dom.txt"

# selector ชุดเดียวกับที่ bot.py ใช้ใน select_promptpay()
BOT_SELECTORS = [
    "text=PromptPay",
    "label:has-text('PromptPay')",
    "div:has-text('PromptPay')",
    "[data-testid='payment-option-0']",
    "[data-testid*='promptpay']",
]

DUMP_JS = """
() => {
  const attrs = e => ({
    tag: e.tagName,
    cls: (e.className || '').toString().slice(0, 80),
    testid: e.getAttribute('data-testid') || '',
    role: e.getAttribute('role') || '',
    ariaChecked: e.getAttribute('aria-checked') || '',
    ariaSelected: e.getAttribute('aria-selected') || '',
  });
  const out = { nodes: [], radios: [], ariaRadios: [], paymentText: [] };

  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while (n = w.nextNode()) {
    if (n.textContent.trim() !== 'PromptPay') continue;
    const chain = [];
    let el = n.parentElement;
    for (let i = 0; i < 6 && el; i++) { chain.push(attrs(el)); el = el.parentElement; }
    out.nodes.push({ chain, html: n.parentElement.outerHTML.slice(0, 400) });
  }

  document.querySelectorAll("input[type=radio]").forEach(r => out.radios.push({
    ...attrs(r),
    checked: r.checked,
    value: r.value || '',
    label: ((r.closest('label,li,div') || r).innerText || '').trim().slice(0, 60),
  }));

  document.querySelectorAll("[role=radio],[aria-checked]").forEach(e => out.ariaRadios.push({
    ...attrs(e), text: (e.innerText || '').trim().slice(0, 60),
  }));

  document.querySelectorAll("*").forEach(e => {
    if (e.children.length === 0 && /payment/i.test(e.textContent))
      out.paymentText.push({ ...attrs(e), text: e.textContent.trim().slice(0, 70) });
  });

  return out;
}
"""

async def dump(page, tag: str, fh) -> dict:
    def w(s):
        print(s)
        fh.write(s + "\n")

    data = await page.evaluate(DUMP_JS)
    w(f"\n{'=' * 64}\n[{tag}] URL: {page.url}\n{'=' * 64}")

    w(f"\n--- text node ที่เป็น 'PromptPay' เป๊ะ ๆ ({len(data['nodes'])}) ---")
    for i, nd in enumerate(data["nodes"]):
        w(f"  [{i}] ancestor chain:")
        for d, a in enumerate(nd["chain"]):
            w(f"      +{d} <{a['tag']}> testid='{a['testid']}' role='{a['role']}' "
              f"aria-checked='{a['ariaChecked']}' aria-selected='{a['ariaSelected']}' "
              f"class='{a['cls']}'")
        w(f"      HTML: {nd['html'][:300]}")

    w(f"\n--- input[type=radio] ({len(data['radios'])}) ---")
    for i, r in enumerate(data["radios"]):
        w(f"  [{i}] checked={r['checked']} value='{r['value']}' testid='{r['testid']}' "
          f"label='{r['label']}' class='{r['cls']}'")

    w(f"\n--- [role=radio] / [aria-checked] ({len(data['ariaRadios'])}) ---")
    for i, a in enumerate(data["ariaRadios"]):
        w(f"  [{i}] <{a['tag']}> aria-checked='{a['ariaChecked']}' testid='{a['testid']}' "
          f"text='{a['text']}'")

    w(f"\n--- leaf element ที่มีคำว่า payment ({len(data['paymentText'])}) ---")
    for i, p in enumerate(data["paymentText"][:10]):
        w(f"  [{i}] <{p['tag']}> testid='{p['testid']}' '{p['text']}'")

    return data


async def probe_selectors(page, fh) -> None:
    def w(s):
        print(s)
        fh.write(s + "\n")

    w("\n--- selector ที่ bot.py ใช้ resolve ไปที่อะไร ---")
    for sel in BOT_SELECTORS:
        try:
            loc = page.locator(sel)
            cnt = await loc.count()
            if cnt == 0:
                w(f"  {sel!r}: count=0  (ไม่เจอ)")
                continue
            first = loc.first
            info = await first.evaluate(
                "e => ({tag: e.tagName, cls: (e.className || '').toString().slice(0, 60),"
                " w: Math.round(e.getBoundingClientRect().width),"
                " h: Math.round(e.getBoundingClientRect().height),"
                " len: (e.innerText || '').trim().length})"
            )
            vis = await first.is_visible()
            w(f"  {sel!r}: count={cnt} first=<{info['tag']}> "
              f"{info['w']}x{info['h']}px textLen={info['len']} visible={vis} "
              f"class='{info['cls']}'")
        except Exception as e:
            w(f"  {sel!r}: ERROR {e}")


async def goto_cart(page) -> bool:
    """product page -> Buy Now -> เลือกไซซ์ -> Buy Now -> หยุดที่ /checkout/cart"""
    await page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=20_000)
    await page.wait_for_timeout(2_000)

    await page.get_by_role("button", name="Buy Now", exact=True).last.click(timeout=10_000)
    await page.wait_for_timeout(800)

    for size in SIZES:
        btn = page.get_by_role("button", name=size, exact=True).first
        try:
            await btn.wait_for(state="visible", timeout=2_500)
            if await btn.is_enabled():
                await btn.click(timeout=3_000)
                print(f"เลือกไซซ์ {size}")
                break
        except Exception:
            continue
    else:
        print(f"ไม่มีไซซ์ {SIZES} ให้เลือก")
        return False

    await page.wait_for_timeout(500)
    await page.get_by_role("button", name="Buy Now", exact=True).first.click(timeout=10_000)
    try:
        await page.wait_for_url(lambda u: "/checkout/cart" in u, timeout=20_000)
    except Exception:
        print(f"ไม่ถึงหน้า cart — ค้างที่ {page.url}")
        return False

    await page.wait_for_load_state("networkidle", timeout=15_000)
    await page.wait_for_timeout(1_500)
    print(f"ถึงหน้า cart: {page.url}")
    return True


async def main() -> None:
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False)
            context = await browser.new_context(storage_state=SESSION_FILE)
            page = await context.new_page()

            if not await goto_cart(page):
                await page.screenshot(path="debug_dom_before.png", full_page=True)
                await browser.close()
                return

            close_btn = page.locator("[data-testid='campaign-close-button']")
            try:
                await close_btn.wait_for(state="visible", timeout=4_000)
                await close_btn.click()
                await page.wait_for_timeout(600)
                print("ปิด campaign modal แล้ว")
            except Exception:
                print("ไม่พบ campaign modal")

            await page.screenshot(path="debug_dom_before.png", full_page=True)
            before = await dump(page, "ก่อนคลิก", fh)
            await probe_selectors(page, fh)

            if not before["nodes"]:
                print("\nไม่เจอ PromptPay ในหน้านี้ — ตะกร้าอาจว่าง ดู debug_dom_before.png")
            else:
                target = page.get_by_text("PromptPay", exact=True).first
                await target.scroll_into_view_if_needed()
                await page.wait_for_timeout(300)
                try:
                    await target.click(timeout=5_000)
                    print("คลิก PromptPay 1 ครั้งแล้ว (ไม่ force)")
                except Exception as e:
                    print(f"คลิกไม่สำเร็จ: {e}")
                await page.wait_for_timeout(800)
                await page.screenshot(path="debug_dom_after.png", full_page=True)
                await dump(page, "หลังคลิก 1 ครั้ง", fh)

            await browser.close()

    print(f"\nบันทึกผลที่ {OUT_FILE}")


asyncio.run(main())

