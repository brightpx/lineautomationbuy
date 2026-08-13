"""
LINE Shopping Auto-Buy Bot
- ตรวจสินค้าทุก 5 วินาที
- กดซื้อทันทีที่เปิดขาย
- เลือกไซซ์/สีอัตโนมัติ
- ไปถึงหน้าชำระเงินอัตโนมัติ
"""

import asyncio
import json
import logging
from pathlib import Path
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeout

# ==================== CONFIGURATION ====================
CONFIG_FILE = "config.json"

def load_config() -> dict:
    """โหลดการตั้งค่าจากไฟล์ config.json"""
    config_path = Path(CONFIG_FILE)
    if not config_path.exists():
        # สร้าง config เริ่มต้นถ้ายังไม่มี
        default_config = {
            "product_url": "https://shop.line.me/@thelandofvava/product/1008229723",
            "preferred_sizes": ["2y"],
            "check_interval": 5,
            "headless": False,
            "session_file": "line_session.json",
            "is_test": False,
            "debug_screenshots": False  # เปิด/ปิด screenshot สำหรับ debug
        }
        config_path.write_text(json.dumps(default_config, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("สร้างไฟล์ %s เริ่มต้นแล้ว", CONFIG_FILE)
        return default_config

    return json.loads(config_path.read_text(encoding="utf-8"))

# โหลด config
config = {}  # จะถูกโหลดใน main()
# =======================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("linebot")


# ==================== HELPER FUNCTIONS ====================

async def save_screenshot(page: Page, filename: str, enabled: bool = True) -> None:
    """บันทึก screenshot ถ้า debug mode เปิดอยู่"""
    if not enabled:
        return
    try:
        await page.screenshot(path=filename)
        log.info(f"Screenshot: {filename}")
    except Exception:
        pass


async def find_button_with_retry(page: Page, selectors: list[str], target_text: str,
                                  max_retries: int = 2, wait_between: int = 100) -> tuple[object, int]:
    """
    หาปุ่มด้วย selector หลายตัว พร้อม retry

    Returns:
        (matching_buttons_locator, count)
    """
    for retry in range(max_retries):
        if retry > 0:
            await page.wait_for_timeout(wait_between)

        for selector in selectors:
            try:
                matching_buttons = page.locator(selector)
                count = await matching_buttons.count()
                if count > 0:
                    log.info(f"  พบปุ่ม '{target_text}' ด้วย selector: {selector} ({count} ปุ่ม)")
                    return matching_buttons, count
            except Exception as e:
                log.debug(f"  Selector '{selector}' ล้มเหลว: {e}")
                continue

    return None, 0


async def close_popup(page: Page, context: str = "") -> bool:
    """
    ปิด popup/modal ที่อาจขวาง

    Args:
        context: บริบทการใช้งาน เช่น "modal", "cart", "checkout"

    Returns:
        True ถ้าปิดสำเร็จ, False ถ้าไม่เจอ popup
    """
    # ลอง Escape key ก่อน (เร็วที่สุด)
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(150)
    except:
        pass

    # Combined selector — ตรวจทีเดียวแทนการ loop 12 ตัว (ลด IPC round-trips)
    combined_selector = (
        "button:has-text('×'), button:has-text('X'), button:has-text('Close'), "
        "button:has-text('ปิด'), button:has-text('Got it'), button:has-text('รับทราบ'), "
        "button:has-text('ไม่ใช้'), [data-testid*='close'], [aria-label*='close' i], "
        "button[class*='close' i]"
    )

    for attempt in range(2):
        try:
            close_btn = page.locator(combined_selector).first
            if await close_btn.is_visible(timeout=500):
                log.info(f"  พบ popup ({context}) — ปิด...")

                # เก็บ URL ก่อนคลิก
                url_before = page.url
                await close_btn.click(timeout=2000, force=True)
                await page.wait_for_timeout(200)

                # ตรวจสอบว่าไม่ redirect ไปที่อื่น
                url_after = page.url
                if "/checkout/cart" in url_before and "/checkout/cart" not in url_after:
                    log.warning(f"  ⚠️  ปุ่มนี้ redirect ไปที่: {url_after}")
                    if "/product/" in url_after:
                        return False
                log.info(f"  ✓ ปิด popup สำเร็จ")
                return True
        except:
            pass

        if attempt < 1:
            await page.wait_for_timeout(300)

    return False


async def select_size_button(page: Page, sizes: list[str]) -> tuple[bool, str]:
    """
    เลือกไซส์จาก modal/bottom-sheet

    Returns:
        (success: bool, selected_size: str)
    """
    # ===== DEBUG: dump ทุก element ที่มีข้อความเกี่ยวกับไซส์ =====
    try:
        dump = await page.evaluate("""
            (sizes) => {
                const result = { exactMatch: [], containsMatch: [], dialogs: [], allButtons: [], zModalContent: '' };

                // 1. หา z-modal container และ dump innerHTML
                const zModal = document.querySelector('.z-modal, [class*="z-modal"]');
                if (zModal) {
                    result.zModalContent = zModal.innerHTML.slice(0, 2000);
                }

                // 2. หา dialogs/modals/bottom-sheets ที่เปิดอยู่
                document.querySelectorAll('[role="dialog"], [role="bottomsheet"], [class*="modal" i], [class*="sheet" i], [class*="overlay" i]').forEach(el => {
                    result.dialogs.push({ tag: el.tagName, role: el.getAttribute('role') || '', cls: (el.className||'').slice(0,80), visible: el.offsetParent !== null, childCount: el.children.length });
                });

                // 3. dump ปุ่มทั้งหมดพร้อม text (สูงสุด 50 ปุ่ม)
                let btnCount = 0;
                document.querySelectorAll('button, [role="button"]').forEach(el => {
                    if (btnCount >= 50) return;
                    const txt = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
                    if (txt) {
                        result.allButtons.push({ txt: txt.slice(0,100), tag: el.tagName, cls: (el.className||'').slice(0,60), disabled: el.disabled, visible: el.offsetParent !== null });
                        btnCount++;
                    }
                });

                // 4. TreeWalker หา exact match ใน text nodes
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                while (walker.nextNode()) {
                    const txt = walker.currentNode.textContent.trim();
                    if (!txt) continue;
                    const el = walker.currentNode.parentElement;
                    if (!el) continue;
                    if (sizes.some(s => txt.toLowerCase() === s.toLowerCase())) {
                        result.exactMatch.push({ text: txt, tag: el.tagName, role: el.getAttribute('role')||'', cls:(el.className||'').slice(0,80), disabled: el.disabled??null, outerHTML: el.outerHTML.slice(0,200) });
                    } else if (sizes.some(s => txt.toLowerCase().includes(s.toLowerCase()))) {
                        result.containsMatch.push({ text: txt.slice(0,120), tag: el.tagName, role: el.getAttribute('role')||'', cls:(el.className||'').slice(0,80) });
                    }
                }
                return result;
            }
        """, sizes)
        log.info("=== SIZE DOM DUMP ===")
        log.info("z-modal innerHTML (first 500 chars): %s", dump.get('zModalContent','(empty)')[:500])
        log.info("Dialogs/Modals open: %d", len(dump.get('dialogs', [])))
        for d in dump.get('dialogs', []):
            log.info("  [dialog] <%s> role='%s' visible=%s childCount=%d class='%s'", d['tag'], d['role'], d['visible'], d['childCount'], d['cls'])
        log.info("Exact match elements: %d", len(dump.get('exactMatch', [])))
        for d in dump.get('exactMatch', []):
            log.info("  [exact] text='%s' <%s> role='%s' disabled=%s class='%s'", d['text'], d['tag'], d['role'], d['disabled'], d['cls'])
            log.info("    HTML: %s", d['outerHTML'])
        log.info("Contains-match elements: %d", len(dump.get('containsMatch', [])))
        for d in dump.get('containsMatch', []):
            log.info("  [contains] text='%s' <%s> role='%s' class='%s'", d['text'], d['tag'], d['role'], d['cls'])
        log.info("All buttons (%d):", len(dump.get('allButtons', [])))
        for b in dump.get('allButtons', []):
            log.info("  [btn] <%s> disabled=%s visible=%s text='%s' class='%s'", b['tag'], b['disabled'], b['visible'], b['txt'], b['cls'])
        log.info("=== END SIZE DOM DUMP ===")
    except Exception as e:
        log.warning("DEBUG dump ล้มเหลว: %s", e)
    # ================================================================

    selectors_template = [
        'button:text("{size}")',
        '[role="button"]:text("{size}")',
        'div:text("{size}")',
        "button:has-text('{size}')",
    ]

    for size in sizes:
        log.info(f"กำลังหาไซส์: {size}")

        # สร้าง selectors สำหรับไซส์นี้
        selectors = [s.format(size=size) for s in selectors_template]

        # หาปุ่ม พร้อม retry
        matching_buttons, count = await find_button_with_retry(page, selectors, size, max_retries=2, wait_between=150)

        if count == 0:
            log.info(f"  ไม่พบปุ่ม '{size}'")
            continue

        # ลองคลิกปุ่มที่พบ
        for i in range(count):
            btn = matching_buttons.nth(i)

            try:
                btn_text = (await btn.text_content() or "").strip()
            except:
                continue

            # ตรวจสอบว่าเป็นไซส์เดียว (ไม่ใช่รายการหลายไซส์)
            if btn_text.lower() != size.lower():
                log.info(f"    ข้าม: '{btn_text}' (ไม่ตรงกับ '{size}')")
                continue

            try:
                is_disabled = await btn.is_disabled()
                is_visible = await btn.is_visible()
            except:
                continue

            log.info(f"    ปุ่ม {i}: '{btn_text}' (disabled={is_disabled}, visible={is_visible})")

            # คลิกปุ่มที่ไม่ disabled และ visible
            if not is_disabled and is_visible:
                try:
                    await btn.click(force=True, timeout=2000)
                    log.info(f"✓ เลือกไซส์: {size} (text='{btn_text}')")
                    return True, size
                except Exception as e:
                    log.warning(f"    ไม่สามารถกดปุ่ม {i} ได้: {e}")
                    continue

    return False, ""


# ==================== MAIN FUNCTIONS ====================

async def save_session(context: BrowserContext, session_file: str) -> None:
    storage = await context.storage_state()
    Path(session_file).write_text(json.dumps(storage), encoding="utf-8")
    log.info("บันทึก session ลงไฟล์ %s แล้ว", session_file)


async def do_login(playwright, session_file: str) -> None:
    """เปิดเบราว์เซอร์ให้ user ล็อกอิน LINE แล้วบันทึก session"""
    browser = await playwright.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()

    log.info("กำลังเปิดหน้า LINE SHOPPING — กรุณาล็อกอินด้วย LINE account ในหน้าต่างเบราว์เซอร์")
    await page.goto("https://shop.line.me/", wait_until="domcontentloaded")

    log.info("=" * 60)
    log.info("กรุณาล็อกอินในหน้าต่างเบราว์เซอร์ให้เสร็จสมบูรณ์")
    log.info("เมื่อล็อกอินเสร็จแล้ว กลับมาที่ terminal นี้แล้วกด Enter")
    log.info("=" * 60)

    # รอ user ยืนยันเองผ่าน terminal — ไม่พลาด
    await asyncio.get_event_loop().run_in_executor(None, input, ">>> กด Enter เมื่อล็อกอินเสร็จแล้ว: ")

    await save_session(context, session_file)
    log.info("✓ บันทึก session เสร็จแล้ว — ปิดเบราว์เซอร์")
    await browser.close()


async def check_size_in_stock(page: Page, sizes: list[str]) -> tuple[bool, str]:
    """
    เช็คว่าไซส์มีในสต็อกหรือไม่โดยไม่ต้องโหลดหน้าใหม่

    Returns:
      (in_stock: bool, available_size: str)
    """
    try:
        # เปิด modal โดยใช้ force=True เพื่อบังคับคลิกแม้มี overlay
        buy_btn = page.get_by_role("button", name="Buy Now", exact=True)
        await buy_btn.last.click(timeout=1_500, force=True)

        # รอ modal เปิดจริง ๆ: รอให้ปุ่มเพิ่มขึ้นจาก 5 (หน้าหลัก) เป็น >5 (modal เพิ่มปุ่มไซส์)
        # selector 'y'/'m' เดิมผิดพลาดเพราะ "Buy Now" บนหน้าหลักมีตัว 'y' อยู่แล้ว
        try:
            await page.wait_for_function(
                "() => document.querySelectorAll('button').length > 5",
                timeout=3000
            )
            log.info("✓ Modal เปิดแล้ว — พบปุ่มเพิ่มขึ้น")
        except:
            log.warning("Modal อาจยังไม่เปิด — ลองต่อ")

        await page.wait_for_timeout(50)

        # เลือกไซส์
        success, selected_size = await select_size_button(page, sizes)

        if success:
            # ไม่ปิด modal — ส่งต่อให้ proceed_to_checkout ใช้ modal ที่เปิดอยู่ (ลด race condition)
            log.info(f"✓ ไซส์ {selected_size} มีในสต็อก (modal ยังเปิดอยู่)")
        else:
            # ปิด modal ก่อนออก
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(50)
            log.warning(f"ไม่พบไซส์ {sizes} ที่ใช้งานได้")

        return success, selected_size

    except Exception as e:
        log.error("Error ตอนเช็คสต็อก: %s", e)
        try:
            await page.keyboard.press("Escape")
        except:
            pass
        return False, ""


async def is_product_available(page: Page) -> bool:
    """คืนค่า True ถ้าปุ่ม Buy Now ไม่ถูก disabled — ใช้ JS inject แทน Playwright locator (เร็วกว่า)"""
    try:
        return await page.evaluate("""
            () => {
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    const txt = btn.textContent?.trim();
                    if ((txt === 'Buy Now' || txt === 'ซื้อเลย') && !btn.disabled)
                        return true;
                }
                return false;
            }
        """)
    except Exception:
        return False


async def proceed_to_checkout(page: Page, sizes: list[str], debug_screenshots: bool = False,
                              modal_already_open: bool = False, preselected_size: str = "") -> tuple[bool, str]:
    """
    LINE Shopping modal flow:
      1. กด Buy Now (เปิด bottom-sheet)
      2. รอ size buttons ปรากฏ
      3. เลือกไซซ์
      4. กด Buy Now ใน modal (confirm)
      5. รอ navigate ออกจากหน้า product

    Returns:
      (success: bool, reason: str)
      - (True, "success") = ซื้อสำเร็จ
      - (False, "sold_out") = ไซซ์หมด ต้องรอเติมของ
      - (False, "error") = error อื่น ๆ
    """
    try:
        # Step 0 — ตรวจสอบว่าอยู่หน้า product
        current_url = page.url
        log.info(f"URL ก่อนกด Buy Now: {current_url}")

        if "/product/" not in current_url:
            log.error(f"ไม่อยู่หน้า product! URL: {current_url}")
            return False, "error"

        if modal_already_open:
            # modal เปิดอยู่แล้วจาก check_size_in_stock() — ข้าม Step 1+2
            log.info("✓ Modal เปิดอยู่แล้ว (จาก check_size_in_stock) — ข้ามการกด Buy Now")
            current_url = page.url
        else:
            # Step 1 — กด Buy Now เพื่อเปิด modal
            buy_btn = page.get_by_role("button", name="Buy Now", exact=True)
            btn_count = await buy_btn.count()
            log.info(f"พบปุ่ม Buy Now: {btn_count} ปุ่ม")

            if btn_count == 0:
                log.error("ไม่เจอปุ่ม Buy Now!")
                return False, "error"

            try:
                await buy_btn.last.click(timeout=5_000, force=True)
                log.info("✓ กด Buy Now สำเร็จ (เปิด modal)...")
            except Exception as e:
                log.error(f"✗ ไม่สามารถกด Buy Now ได้: {e}")
                await save_screenshot(page, "debug_click_failed.png", debug_screenshots)
                return False, "error"

            # Step 2 — รอให้ modal เปิดจริง: ปุ่มในหน้าเพิ่มขึ้นจาก 5 (หน้าหลัก) เป็นมากกว่านั้น
            try:
                await page.wait_for_function(
                    "() => document.querySelectorAll('button').length > 5",
                    timeout=2_000
                )
                log.info("✓ Modal เปิดแล้ว — พบปุ่มเพิ่มขึ้น")
            except:
                log.warning("Modal อาจยังโหลดไม่เสร็จ — ลองต่อ")

            await page.wait_for_timeout(150)

            log.info("ตรวจสอบ popup ใน modal...")
            await close_popup(page, "modal")
            await page.wait_for_timeout(100)

            current_url = page.url
            log.info(f"URL หลังกด Buy Now: {current_url}")

        # ถ้าไปหน้า checkout/cart แล้ว = ระบบเพิ่มสินค้าเข้าตะกร้าอัตโนมัติ (สำเร็จ)
        if "/checkout/cart" in current_url:
            log.info("✓ ระบบเพิ่มสินค้าเข้าตะกร้าอัตโนมัติแล้ว — ข้ามขั้นตอนเลือกไซส์")

            # รอให้หน้า checkout โหลดเสร็จก่อน
            log.info("รอหน้า checkout โหลดเสร็จ...")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                await page.wait_for_timeout(1000)
                log.info("✓ หน้า checkout โหลดเสร็จแล้ว")
            except Exception as e:
                log.warning(f"⚠️  หน้า checkout อาจยังไม่โหลดเสร็จ: {e}")

            # ปิด popup ที่หน้า cart
            log.info("ปิด popup ที่หน้า cart...")
            await close_popup(page, "cart")

            return True, "success"

        # ถ้าออกจากหน้า product ไปที่อื่น (ไม่ใช่ checkout) = error
        if "/product/" not in current_url:
            log.error(f"ออกจากหน้า product ไปที่ไม่คาดคิด: {current_url}")
            await save_screenshot(page, "debug_wrong_page.png", debug_screenshots)
            return False, "error"

        await save_screenshot(page, "debug_modal.png", debug_screenshots)

        # Step 3 — เลือกไซซ์ (ข้ามถ้า preselect ไว้แล้วจาก check_size_in_stock)
        if modal_already_open and preselected_size:
            picked, selected_size = True, preselected_size
            log.info(f"✓ ใช้ไซส์ที่เลือกไว้แล้ว: {selected_size}")
        else:
            picked, selected_size = await select_size_button(page, sizes)

        if not picked:
            log.warning("ไซซ์ %s หมดทุกตัวแล้ว (sold out) — รอเติมของ...", sizes)
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(100)
            except Exception:
                pass
            return False, "sold_out"

        await page.wait_for_timeout(100)

        # Step 4 — กด Buy Now ใน modal (ปุ่มแรกใน modal ที่ไม่ disabled)
        buy_now_buttons = page.get_by_role("button", name="Buy Now", exact=True)
        count = await buy_now_buttons.count()
        log.info(f"พบปุ่ม Buy Now {count} ปุ่ม")

        await save_screenshot(page, "debug_before_place_order.png", debug_screenshots)

        # ลองกดปุ่ม Buy Now ที่ visible ทีละปุ่ม โดยใช้ force=True
        clicked = False
        for i in range(count):
            btn = buy_now_buttons.nth(i)
            try:
                is_visible = await btn.is_visible()
                is_disabled = await btn.is_disabled()

                log.info(f"  ปุ่ม Buy Now {i}: visible={is_visible}, disabled={is_disabled}")

                if is_visible and not is_disabled:
                    log.info(f"  พยายามกดปุ่มที่ {i}...")
                    await btn.click(timeout=2_000, force=True)
                    log.info(f"  ✓ กด Buy Now ปุ่มที่ {i} สำเร็จ")
                    clicked = True

                    await page.wait_for_timeout(200)
                    log.info(f"  URL หลังกด: {page.url}")
                    break
            except Exception as e:
                log.warning(f"  ปุ่ม {i} กดไม่ได้: {e}")
                continue

        if not clicked:
            log.error("ไม่สามารถกดปุ่ม Buy Now ใน modal ได้")
            await save_screenshot(page, "debug_failed_click.png", debug_screenshots)
            await page.keyboard.press("Escape")
            return False, "error"

        log.info("กด Buy Now ใน modal...")

        # Step 5 — รอออกจากหน้า product
        log.info("รอเปลี่ยนหน้า (ออกจาก /product/)...")

        # Step 5 — รอออกจากหน้า product (ใช้ wait_for_url แทน polling)
        log.info("รอเปลี่ยนหน้า (ออกจาก /product/)...")
        try:
            await page.wait_for_url(lambda url: "/product/" not in url, timeout=15_000)
        except PWTimeout:
            log.error("✗ Timeout 15s — URL ยังอยู่หน้า product: %s", page.url)
            await save_screenshot(page, "debug_timeout.png", debug_screenshots)
            return False, "error"

        current_url = page.url
        log.info("✓ ถึงหน้า checkout/cart: %s", current_url)

        # รอให้หน้า checkout โหลดเสร็จ
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except PWTimeout:
            log.warning("⚠️  domcontentloaded timeout — ดำเนินการต่อ")

        await page.wait_for_timeout(400)
        final_url = page.url

        if "/product/" in final_url:
            log.error("✗ ถูก redirect กลับหน้า product: %s", final_url)
            await save_screenshot(page, "debug_redirect_back.png", debug_screenshots)
            return False, "error"

        log.info("✓ ยืนยันอยู่หน้า checkout: %s", final_url)
        return True, "success"

    except Exception as e:
        log.error("❌ Error: %s", e, exc_info=True)
        await save_screenshot(page, "debug_error.png", debug_screenshots)
        return False, "error"


async def select_promptpay(page: Page, debug_screenshots: bool = False) -> bool:
    """
    Fast path: ใช้ JS inject เพื่อเลือก PromptPay และเตรียม Place Order
    - ไม่รอ visual render / popup / scroll
    - JS รันใน browser โดยตรง ไม่สนว่า element visible หรือถูกบัง
    """
    try:
        log.info("รอหน้า checkout โหลด (domcontentloaded)...")
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)

        # ======== Step A: ถ้ายังอยู่หน้า cart — กด Checkout ผ่านไปหน้า payment ========
        current_url = page.url
        if "/checkout/cart" in current_url:
            log.info("อยู่หน้า cart — ปิด popup แล้วรอ Vue render...")

            # ปิด popup ก่อน (สำคัญ: ต้องทำก่อนรอปุ่ม ไม่งั้น Vue ไม่ hydrate)
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
            except:
                pass
            await close_popup(page, "cart-checkout")
            await page.wait_for_timeout(200)

            # ข้อ 2: เปลี่ยนจาก networkidle → domcontentloaded (เร็วกว่ามาก)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5_000)
                log.info("✓ domcontentloaded แล้ว")
            except PWTimeout:
                log.warning("⚠️  domcontentloaded timeout — ดำเนินการต่อ")

            # รอปุ่ม Place Order ขึ้นบน cart page (Vue render) สูงสุด 15s
            try:
                await page.wait_for_selector(
                    'button:has-text("Place Order"), button:has-text("Checkout"), button:has-text("ชำระเงิน")',
                    state="attached", timeout=15_000
                )
                log.info("✓ ปุ่ม Place Order/Checkout ปรากฏบน cart page")
            except PWTimeout:
                log.warning("⚠️  รอ cart buttons timeout — ลองต่อ")

            await save_screenshot(page, "debug_cart_page.png", debug_screenshots)

            # Dump ปุ่มทั้งหมดบน cart page
            cart_btns = await page.evaluate("""
                () => {
                    const btns = [];
                    document.querySelectorAll('button, [role="button"], a[href*="checkout"]').forEach((b, i) => {
                        const txt = b.innerText?.trim() || b.textContent?.trim() || '';
                        if (txt) btns.push({ index: i, text: txt.slice(0,60), disabled: b.disabled, tag: b.tagName });
                    });
                    return btns;
                }
            """)
            log.info("=== ปุ่มบน cart page (%d ปุ่ม) ===", len(cart_btns))
            for b in cart_btns:
                log.info("  [%d] <%s> '%s' disabled=%s", b["index"], b["tag"], b["text"].replace("\n"," "), b["disabled"])
            log.info("=================================")

            # กด Checkout / Place Order / Next
            checkout_btn_selectors = [
                'button:has-text("Checkout")',
                'button:has-text("ชำระเงิน")',
                'button:has-text("Place Order")',
                'button:has-text("สั่งซื้อ")',
                'button:has-text("Next")',
                'button:has-text("ถัดไป")',
                'button:has-text("Proceed")',
                'button:has-text("Confirm")',
            ]
            cart_clicked = False
            for sel in checkout_btn_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0:
                        is_dis = await btn.is_disabled()
                        log.info("พบ '%s' disabled=%s — %s", sel, is_dis, "ข้าม" if is_dis else "กด!")
                        if not is_dis:
                            await btn.click(timeout=3_000)
                            log.info("✓ กด Checkout สำเร็จ")
                            cart_clicked = True
                            break
                except Exception as e:
                    log.debug("  %s ล้มเหลว: %s", sel, e)
                    continue

            if cart_clicked:
                log.info("รอออกจาก cart page...")
                try:
                    await page.wait_for_url(lambda url: "/checkout/cart" not in url, timeout=15_000)
                    log.info("✓ ออกจาก cart แล้ว: %s", page.url)
                except PWTimeout:
                    log.warning("⚠️  ยังอยู่หน้า cart: %s", page.url)
            else:
                log.warning("⚠️  ไม่พบปุ่ม Checkout — URL ปัจจุบัน: %s", page.url)

        # รอ Vue/React render payment section
        log.info("รอ payment section โหลด (สูงสุด 20s)...")
        try:
            await page.wait_for_selector(
                'input[type="radio"], button:has-text("Place Order"), button:has-text("สั่งซื้อ")',
                state="attached",
                timeout=20_000
            )
            log.info("✓ payment section อยู่ใน DOM แล้ว")
        except PWTimeout:
            log.warning("⚠️  payment section ไม่ปรากฏใน 20s — ดำเนินการต่อ")

        # กด Escape ปิด popup ง่ายๆ ก่อน (ถ้ามี)
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(150)
        except:
            pass

        await save_screenshot(page, "debug_checkout.png", debug_screenshots)

        # ======== DEBUG: dump DOM payment section ========
        dom_info = await page.evaluate("""
            () => {
                const result = { radios: [], texts: [], allInputs: [] };

                // dump radio inputs ทั้งหมด
                document.querySelectorAll('input[type="radio"]').forEach((r, i) => {
                    result.radios.push({
                        index: i,
                        name: r.name,
                        value: r.value,
                        checked: r.checked,
                        id: r.id,
                        parentText: r.parentElement ? r.parentElement.innerText?.slice(0,80) : '',
                        grandparentText: r.parentElement?.parentElement
                            ? r.parentElement.parentElement.innerText?.slice(0,80) : ''
                    });
                });

                // dump text nodes ที่มี "promptpay" หรือ "payment"
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                while (walker.nextNode()) {
                    const t = walker.currentNode.textContent.trim();
                    if (t && (t.toLowerCase().includes('promptpay') || t.toLowerCase().includes('payment'))) {
                        result.texts.push({
                            text: t.slice(0, 100),
                            parentTag: walker.currentNode.parentElement?.tagName,
                            parentClass: walker.currentNode.parentElement?.className?.slice(0,60)
                        });
                    }
                }

                // dump input ทุกชนิด
                document.querySelectorAll('input').forEach((inp, i) => {
                    result.allInputs.push({ index: i, type: inp.type, name: inp.name, value: inp.value, id: inp.id });
                });

                return result;
            }
        """)
        log.info("=== DEBUG DOM DUMP ===")
        log.info("Radio inputs (%d total):", len(dom_info.get("radios", [])))
        for r in dom_info.get("radios", []):
            log.info("  [%d] name=%s value=%s checked=%s | parent: %s",
                     r["index"], r["name"], r["value"], r["checked"],
                     r["parentText"].replace("\n", " "))
        log.info("Text nodes with 'promptpay'/'payment' (%d total):", len(dom_info.get("texts", [])))
        for t in dom_info.get("texts", []):
            log.info("  '%s' in <%s class='%s'>", t["text"], t["parentTag"], t["parentClass"])
        log.info("All inputs (%d total):", len(dom_info.get("allInputs", [])))
        for inp in dom_info.get("allInputs", []):
            log.info("  [%d] type=%s name=%s value=%s id=%s",
                     inp["index"], inp["type"], inp["name"], inp["value"], inp["id"])
        log.info("=== END DEBUG DOM DUMP ===")

        # ======== JS inject: เลือก PromptPay (Vue custom component) ========
        # LINE Shopping ไม่ใช้ <input type="radio"> — ใช้ Vue custom UI
        # ต้องคลิก container ที่ wrap <P>PromptPay</P> แทน
        log.info("JS inject: เลือก PromptPay (Vue custom)...")
        pp_selected = await page.evaluate("""
            () => {
                // หา <P> ที่มีข้อความ "PromptPay" แล้วคลิก ancestor ที่ clickable
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                while (walker.nextNode()) {
                    const txt = walker.currentNode.textContent.trim();
                    if (txt === 'PromptPay' || txt.toLowerCase() === 'promptpay') {
                        let el = walker.currentNode.parentElement; // <P>
                        // walk up สูงสุด 8 ชั้น หา element ที่ clickable (มี onclick, role, cursor-pointer)
                        for (let i = 0; i < 8; i++) {
                            if (!el) break;
                            const style = window.getComputedStyle(el);
                            const hasClick = el.onclick
                                || el.getAttribute('role') === 'button'
                                || el.getAttribute('tabindex') != null
                                || style.cursor === 'pointer';
                            if (hasClick) {
                                el.click();
                                return { clicked: true, tag: el.tagName, cls: el.className?.slice(0,60) };
                            }
                            el = el.parentElement;
                        }
                        // ถ้าหา clickable ไม่เจอ ให้คลิก parent ตรงๆ (ชั้นที่ 3 จาก <P>)
                        let fallback = walker.currentNode.parentElement; // <P>
                        for (let j = 0; j < 3 && fallback?.parentElement; j++) fallback = fallback.parentElement;
                        if (fallback) {
                            fallback.click();
                            return { clicked: true, tag: fallback.tagName, cls: fallback.className?.slice(0,60), fallback: true };
                        }
                    }
                }
                return { clicked: false };
            }
        """)

        if pp_selected and pp_selected.get("clicked"):
            log.info("✓ คลิก PromptPay container: <%s class='%s'>%s",
                     pp_selected.get("tag"), pp_selected.get("cls"),
                     " (fallback)" if pp_selected.get("fallback") else "")
            await page.wait_for_timeout(500)  # รอ Vue update state
        else:
            log.warning("⚠️  ไม่พบ PromptPay — ลองปิด popup แล้ว retry...")
            await close_popup(page, "checkout-fast")
            await page.wait_for_timeout(400)
            pp_retry = await page.evaluate("""
                () => {
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                    while (walker.nextNode()) {
                        const txt = walker.currentNode.textContent.trim();
                        if (txt === 'PromptPay' || txt.toLowerCase() === 'promptpay') {
                            let el = walker.currentNode.parentElement;
                            for (let i = 0; i < 8; i++) {
                                if (!el) break;
                                const style = window.getComputedStyle(el);
                                if (el.onclick || el.getAttribute('role')==='button'
                                    || el.getAttribute('tabindex')!=null || style.cursor==='pointer') {
                                    el.click();
                                    return true;
                                }
                                el = el.parentElement;
                            }
                            // fallback: คลิก grandparent
                            let fb = walker.currentNode.parentElement?.parentElement?.parentElement;
                            if (fb) { fb.click(); return true; }
                        }
                    }
                    return false;
                }
            """)
            if pp_retry:
                log.info("✓ เลือก PromptPay สำเร็จ (หลังปิด popup)")
                await page.wait_for_timeout(500)
            else:
                log.warning("⚠️  ไม่พบ PromptPay — ดำเนินการต่อโดยไม่เลือก")

        # รอ state update หลังคลิก radio (สั้นมาก)
        await page.wait_for_timeout(300)

        await save_screenshot(page, "debug_after_select_promptpay.png", debug_screenshots)

        # ======== หา Place Order button แล้ว scroll ให้เห็น ========
        place_order_btn = page.locator(
            'button:has-text("Place Order"), button:has-text("สั่งซื้อ"), '
            '[role="button"]:has-text("Place Order"), [role="button"]:has-text("สั่งซื้อ")'
        ).first

        if await place_order_btn.count() == 0:
            log.error("✗ ไม่พบปุ่ม Place Order")
            await save_screenshot(page, "debug_no_place_order.png", debug_screenshots)
            return False

        try:
            await place_order_btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(150)
        except:
            pass

        log.info("✓ PromptPay เลือกแล้ว — พร้อม Place Order")
        return True

    except PWTimeout as e:
        log.error("⏱️  Timeout ตอนเลือก PromptPay: %s", e)
        await save_screenshot(page, "debug_promptpay_timeout.png", debug_screenshots)
        return False
    except Exception as e:
        log.error("❌ Error ตอนเลือก PromptPay: %s", e, exc_info=True)
        await save_screenshot(page, "debug_promptpay_error.png", debug_screenshots)
        return False


async def monitor_and_buy(playwright, product_url: str, preferred_sizes: list[str],
                          check_interval: int, session_file: str, headless: bool,
                          is_test: bool = False, debug_screenshots: bool = False) -> None:
    """วนตรวจสินค้าทุก CHECK_INTERVAL วินาที แล้วกดซื้อทันทีที่เปิดขาย"""
    session_kwargs = {}
    if Path(session_file).exists():
        session_kwargs["storage_state"] = session_file
        log.info("โหลด session จาก %s", session_file)
    else:
        log.warning("ไม่พบ session file — อาจต้องล็อกอินก่อน (รัน: python bot.py login)")

    # ข้อ 3: เพิ่ม browser flags เพื่อลด overhead
    browser = await playwright.chromium.launch(
        headless=headless,
        args=[
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-default-apps",
            "--no-first-run",
            "--disable-sync",
            "--disable-translate",
            "--disable-background-timer-throttling",
        ]
    )

    context = await browser.new_context(**session_kwargs)

    # Block ทรัพยากรที่ไม่จำเป็น รวม analytics/tracking (ข้อ 6)
    async def block_resources(route):
        resource_type = route.request.resource_type
        url = route.request.url
        if resource_type in ['image', 'stylesheet', 'font', 'media']:
            await route.abort()
        elif any(x in url for x in ['analytics', 'gtm', 'googletagmanager', 'facebook', 'hotjar', 'clarity', 'doubleclick']):
            await route.abort()
        else:
            await route.continue_()

    page = await context.new_page()
    await page.route("**/*", block_resources)

    log.info("เริ่มตรวจสินค้า: %s", product_url)
    log.info("ไซซ์ที่ต้องการ (ตามลำดับ): %s", preferred_sizes)
    if is_test:
        log.info("โหมดทดสอบ: จะหยุดที่หน้า checkout/cart (ไม่กด Place Order)")

    attempt = 0
    sold_out_count = 0
    do_exit = False

    while True:
        attempt += 1
        try:
            # ข้อ 4: ใช้ reload() ตั้งแต่รอบที่ 2 เป็นต้นไป (เร็วกว่า goto เพราะ reuse connection)
            if attempt == 1 or "/product/" not in page.url:
                await page.goto(product_url, wait_until="domcontentloaded", timeout=10_000)

                # ครั้งแรก: รอให้หน้าโหลดเสร็จจริง ๆ
                if attempt == 1:
                    log.info("โหลดหน้าครั้งแรก — รอให้หน้าโหลดเสร็จ...")
                    await page.wait_for_timeout(500)  # ลดจาก 1500 → 500ms
                else:
                    await page.wait_for_timeout(100)  # ลดจาก 400 → 100ms
            else:
                # รอบที่ 2+ ที่ยังอยู่หน้า product: reload แทน goto (เร็วกว่า)
                await page.reload(wait_until="domcontentloaded", timeout=8_000)
                await page.wait_for_timeout(100)

            available = await is_product_available(page)

            if not available:
                log.info("[ครั้งที่ %d] สถานะสินค้า: ยังไม่เปิดขาย", attempt)
                await asyncio.sleep(check_interval)
                continue

            # สินค้าเปิดขาย — เช็คสต็อกโดยไม่ต้องโหลดหน้าใหม่
            in_stock, available_size = await check_size_in_stock(page, preferred_sizes)

            if not in_stock:
                sold_out_count += 1
                log.info("[ครั้งที่ %d] ไซซ์ %s หมดทุกตัว — เช็คอีกครั้งในอีก %d วินาที",
                        attempt, preferred_sizes, check_interval)
                await asyncio.sleep(check_interval)
                continue

            # มีสต็อก — ซื้อเลย!
            log.info("[ครั้งที่ %d] พบสินค้ามีสต็อก (ไซซ์: %s) — เริ่มซื้อ!", attempt, available_size)

            # ปิด resource blocking เพื่อให้ checkout ทำงานปกติ
            await page.unroute("**/*")

            # ส่ง modal ที่เปิดอยู่แล้วเข้า proceed_to_checkout เลย (ลด race condition)
            success, reason = await proceed_to_checkout(
                page, preferred_sizes, debug_screenshots,
                modal_already_open=True, preselected_size=available_size
            )

            if success:
                paid = await select_promptpay(page, debug_screenshots)
                if paid:
                    log.info("=" * 60)
                    log.info("✓ ถึงหน้า Place Order พร้อม PromptPay แล้ว")
                    if is_test:
                        log.info("✓ [TEST MODE] กด Enter เพื่อยืนยัน Place Order หรือ Ctrl+C เพื่อยกเลิก")
                    else:
                        log.info("✓ กด Enter ใน console นี้เพื่อกด Place Order อัตโนมัติ")
                    log.info("=" * 60)
                    input(">>> กด Enter เพื่อ Place Order หรือ Ctrl+C เพื่อยกเลิก: ")
                    # ปิด popup ที่อาจบัง Place Order ก่อนกด
                    try:
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(300)
                    except:
                        pass
                    await close_popup(page, "before-place-order")
                    await page.wait_for_timeout(300)
                    # กด Place Order อัตโนมัติ
                    try:
                        place_order_btn = page.locator(
                            'button:has-text("Place Order"), button:has-text("สั่งซื้อ"), '
                            '[role="button"]:has-text("Place Order"), [role="button"]:has-text("สั่งซื้อ")'
                        ).first
                        await place_order_btn.click()
                        log.info("✅ กด Place Order สำเร็จ!")
                        await page.wait_for_timeout(3000)
                        log.info("URL หลัง Place Order: %s", page.url)
                    except Exception as e:
                        log.error("❌ กด Place Order ไม่สำเร็จ: %s", e)
                else:
                    log.warning("=" * 60)
                    log.warning("⚠️  มีปัญหาในขั้นตอนชำระเงิน")
                    log.warning("⚠️  กรุณาดำเนินการเองในหน้าต่างเบราว์เซอร์")
                    log.warning("=" * 60)
                do_exit = True
                break
            elif reason == "sold_out":
                # ไซส์หมด — เปิด blocking กลับมาแล้ววนลูปต่อ
                await page.route("**/*", block_resources)
                log.info("ไซซ์หมดระหว่างการซื้อ — ลองใหม่...")
                sold_out_count += 1
            else:
                # error อื่น ๆ
                await page.route("**/*", block_resources)
                log.warning("มี error ระหว่างการซื้อ — ลองใหม่...")

        except PWTimeout:
            log.warning("[ครั้งที่ %d] Timeout โหลดหน้า — ลองใหม่...", attempt)
        except Exception as e:
            log.error("[ครั้งที่ %d] Error: %s", attempt, e)

        if do_exit:
            break

        await asyncio.sleep(check_interval)

    # ถ้าออกจาก loop เพราะถึงหน้า checkout แล้ว — รอไม่จำกัดเวลา
    if do_exit:
        log.info("เบราว์เซอร์จะเปิดค้างไว้ — กด Ctrl+C เพื่อออกจากโปรแกรม")
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            log.info("ออกจากโปรแกรมตามคำสั่ง user")

    await browser.close()


async def main(mode: str = "buy") -> None:
    global config
    config = load_config()

    log.info("โหลดการตั้งค่าจาก %s", CONFIG_FILE)
    log.info("  - Product URL: %s", config["product_url"])
    log.info("  - Preferred Sizes: %s", config["preferred_sizes"])
    log.info("  - Check Interval: %d วินาที", config["check_interval"])
    log.info("  - Headless: %s", config["headless"])
    log.info("  - Test Mode: %s", config.get("is_test", False))
    log.info("  - Debug Screenshots: %s", config.get("debug_screenshots", False))

    async with async_playwright() as pw:
        if mode == "login":
            await do_login(pw, config["session_file"])
        else:
            await monitor_and_buy(
                pw,
                product_url=config["product_url"],
                preferred_sizes=config["preferred_sizes"],
                check_interval=config["check_interval"],
                session_file=config["session_file"],
                headless=config["headless"],
                is_test=config.get("is_test", False),
                debug_screenshots=config.get("debug_screenshots", False)
            )


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "buy"
    asyncio.run(main(mode))
