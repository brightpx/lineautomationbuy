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
    close_selectors = [
        "button:has-text('×')",
        "button:has-text('X')",
        "button:has-text('Close')",
        "button:has-text('ปิด')",
        "button:has-text('Got it')",
        "button:has-text('รับทราบ')",
        "button:has-text('ไม่ใช้')",
        "[data-testid*='close']",
        "[aria-label*='close' i]",
        "button[class*='close' i]",
        "[role='dialog'] button:has-text('Close')",
        "[role='dialog'] button:has-text('ปิด')",
    ]

    # ลอง Escape key ก่อน
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
    except:
        pass

    # ลองปิดด้วย selector (ลดจาก 6 รอบ → 2 รอบ)
    for attempt in range(2):
        for selector in close_selectors:
            try:
                close_btn = page.locator(selector).first
                if await close_btn.is_visible(timeout=500):
                    btn_text = await close_btn.text_content() or ""
                    log.info(f"  พบ popup ({context}) - ปิดด้วย: {selector}")

                    # เก็บ URL ก่อนคลิก
                    url_before = page.url
                    await close_btn.click(timeout=2000, force=True)
                    await page.wait_for_timeout(300)

                    # ตรวจสอบว่าไม่ redirect ไปที่อื่น
                    url_after = page.url
                    if "/checkout/cart" in url_before and "/checkout/cart" not in url_after:
                        log.warning(f"  ⚠️  ปุ่มนี้ redirect ไปที่: {url_after}")
                        if "/product/" in url_after:
                            return False  # ต้อง retry
                        continue

                    log.info(f"  ✓ ปิด popup สำเร็จ")
                    return True
            except:
                continue

        if attempt < 1:
            await page.wait_for_timeout(500)

    return False


async def select_size_button(page: Page, sizes: list[str]) -> tuple[bool, str]:
    """
    เลือกไซส์จาก modal/bottom-sheet

    Returns:
        (success: bool, selected_size: str)
    """
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

    log.info("กรุณาล็อกอินในหน้าต่างเบราว์เซอร์ จากนั้นรอระบบตรวจจับ...")
    # รอจนเห็น element ที่บ่งบอกว่าล็อกอินสำเร็จ (avatar หรือ MY page)
    try:
        await page.wait_for_selector(
            "[class*='avatar'], [class*='Avatar'], [href*='/my'], [data-testid*='my'], "
            "[class*='userProfile'], [class*='user-profile'], [class*='profileImage']",
            timeout=180_000,
            state="visible",
        )
        log.info("ตรวจพบว่าล็อกอินสำเร็จแล้ว!")
    except PWTimeout:
        log.warning("หมดเวลา 3 นาที — บันทึก session ตามที่มี")

    await save_session(context, session_file)
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

        # รอ modal ปรากฏและโหลดเสร็จ
        try:
            await page.wait_for_selector("button[role='button']:has-text('y'), button[role='button']:has-text('m')", timeout=800)
        except:
            pass

        await page.wait_for_timeout(50)

        # เลือกไซส์
        success, selected_size = await select_size_button(page, sizes)

        # ปิด modal
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(50)

        if success:
            log.info(f"✓ ไซส์ {selected_size} มีในสต็อก")
        else:
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
    """คืนค่า True ถ้าปุ่ม Buy Now ไม่ถูก disabled"""
    try:
        btn = page.get_by_role("button", name="Buy Now", exact=True)
        if await btn.count() == 0:
            btn = page.get_by_role("button", name="ซื้อเลย", exact=True)
        if await btn.count() == 0:
            return False
        return not await btn.first.is_disabled()
    except Exception:
        return False


async def proceed_to_checkout(page: Page, sizes: list[str], debug_screenshots: bool = False) -> tuple[bool, str]:
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

        # Step 1 — กด Buy Now เพื่อเปิด modal
        buy_btn = page.get_by_role("button", name="Buy Now", exact=True)
        btn_count = await buy_btn.count()
        log.info(f"พบปุ่ม Buy Now: {btn_count} ปุ่ม")

        if btn_count == 0:
            log.error("ไม่เจอปุ่ม Buy Now!")
            return False, "error"

        # คลิกปุ่มสุดท้าย (ปุ่มหลัก ไม่ใช่ปุ่มใน modal) ด้วย force=True
        try:
            await buy_btn.last.click(timeout=5_000, force=True)
            log.info("✓ กด Buy Now สำเร็จ (เปิด modal)...")
        except Exception as e:
            log.error(f"✗ ไม่สามารถกด Buy Now ได้: {e}")
            await save_screenshot(page, "debug_click_failed.png", debug_screenshots)
            return False, "error"

        # Step 2 — รอให้ปุ่มไซส์ปรากฏ (แสดงว่า modal เปิดแล้ว)
        try:
            await page.wait_for_selector(
                "button:has-text('y'), button:has-text('m'), [class*='size'] button, [class*='Size'] button",
                timeout=1_200,
                state="visible"
            )
            log.info("✓ Modal เปิดแล้ว — พบปุ่มไซส์")
        except:
            log.warning("Modal อาจยังโหลดไม่เสร็จ — ลองต่อ")

        await page.wait_for_timeout(150)

        # ปิด popup ที่อาจขวาง modal
        log.info("ตรวจสอบ popup ใน modal...")
        await close_popup(page, "modal")
        await page.wait_for_timeout(100)

        # ตรวจสอบ URL หลังกด Buy Now
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

        # Step 3 — เลือกไซซ์ตามลำดับ priority
        picked, selected_size = await select_size_button(page, sizes)

        if not picked:
            log.warning("ไซซ์ %s หมดทุกตัวแล้ว (sold out) — รอเติมของ...", sizes)

            # ปิด modal ก่อนออก
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

        # รอให้ URL เปลี่ยนจริง ๆ — ลดจาก 5 รอบ → 3 รอบ
        for attempt in range(3):
            await page.wait_for_timeout(400)
            current_url = page.url
            log.info(f"  ตรวจสอบครั้งที่ {attempt + 1}: {current_url}")

            if "/product/" not in current_url:
                log.info("✓ ถึงหน้า checkout/cart: %s", current_url)

                # รอให้หน้า checkout โหลดเสร็จ
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                await page.wait_for_timeout(400)
                final_url = page.url

                if "/product/" in final_url:
                    log.error("✗ ถูก redirect กลับหน้า product: %s", final_url)
                    await save_screenshot(page, "debug_redirect_back.png", debug_screenshots)
                    return False, "error"
                else:
                    log.info("✓ ยืนยันอยู่หน้า checkout: %s", final_url)
                    return True, "success"

            if attempt < 2:
                log.info(f"  ยังอยู่หน้า product — รอต่อ...")

        log.error("✗ Timeout — ไม่สามารถออกจากหน้า product ได้")
        return False, "error"

    except PWTimeout:
        log.error("⏱️  Timeout — URL ปัจจุบัน: %s", page.url)
        await save_screenshot(page, "debug_timeout.png", debug_screenshots)

        is_checkout = "/product/" not in page.url
        if is_checkout:
            log.info("✓ แม้ timeout แต่อยู่หน้า checkout แล้ว")
        else:
            log.error("✗ ยังอยู่หน้า product — การซื้อล้มเหลว")
        return is_checkout, "success" if is_checkout else "error"
    except Exception as e:
        log.error("❌ Error: %s", e, exc_info=True)
        await save_screenshot(page, "debug_error.png", debug_screenshots)
        return False, "error"


async def select_promptpay(page: Page, debug_screenshots: bool = False) -> bool:
    """
    ทำงานบน /checkout/cart (cart review):
      1. ปิด campaign modal
      2. เลือก PromptPay card (ที่หน้า cart)
      3. กด Place Order
      4. หยุดรอ — ให้ user ยืนยันการชำระเงิน
    """
    try:
        log.info("รอหน้า checkout โหลด...")
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        await page.wait_for_timeout(1000)

        log.info("URL ปัจจุบัน: %s", page.url)
        await save_screenshot(page, "debug_checkout.png", debug_screenshots)

        # ปิด popup/modal ที่หน้า checkout (ลดจาก 6 รอบ → 3 รอบ)
        log.info("ตรวจสอบ popup ที่หน้า checkout...")
        await page.wait_for_timeout(800)

        for attempt in range(3):
            popup_closed = await close_popup(page, "checkout")
            if popup_closed:
                await page.wait_for_timeout(500)
                break
            if attempt < 2:
                await page.wait_for_timeout(800)

        await save_screenshot(page, "debug_after_close_popup.png", debug_screenshots)

        # ตรวจสอบว่ายังอยู่หน้า checkout/cart หรือไม่
        current_url = page.url
        if "/checkout/cart" not in current_url:
            log.warning(f"⚠️  หลังปิด popup กลับไปหน้าอื่น: {current_url}")
            return False

        log.info(f"✓ ยังอยู่หน้า checkout/cart: {current_url}")

        # ======== เลือก PromptPay ก่อน (ที่หน้า cart) ========
        log.info("กำลังหา PromptPay option ที่หน้า cart...")

        # Scroll ลงมาที่ส่วน Payment ก่อน
        try:
            payment_section = page.locator("text=Payment").first
            if await payment_section.is_visible(timeout=2000):
                await payment_section.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)
                log.info("Scroll ลงมาที่ส่วน Payment แล้ว")
        except:
            pass

        await save_screenshot(page, "debug_before_promptpay.png", debug_screenshots)

        # ====== วิธีที่น่าเชื่อถือที่สุด ======
        # เริ่มจาก text node "PromptPay" แบบ exact → walk up หา radio ของ option นั้นโดยตรง
        # หลีกเลี่ยง:
        #   - data-testid='payment-option-0'  → อาจเป็น LINE Pay (default option แรก)
        #   - div:has-text('PromptPay')       → จับ wrapper div ทั้งส่วน, radio แรกใน div คือ LINE Pay
        selected = False
        try:
            pp_text = page.get_by_text("PromptPay", exact=True).first
            if await pp_text.is_visible(timeout=2000):
                log.info("พบ text 'PromptPay' (exact) — กำลัง walk up หา radio")
                await pp_text.scroll_into_view_if_needed()
                await page.wait_for_timeout(300)

                # walk up ancestor ทีละชั้น จนพบ radio button ของ option นี้
                radio_to_click = None
                for depth in range(1, 7):
                    xpath = "/".join([".."] * depth)
                    try:
                        candidate = pp_text.locator(f"xpath={xpath}//input[@type='radio']").first
                        if await candidate.count() > 0:
                            log.info(f"  พบ radio ที่ ancestor depth={depth}")
                            radio_to_click = candidate
                            break
                    except:
                        pass

                if radio_to_click is not None:
                    await radio_to_click.click(timeout=2000, force=True)
                    await page.wait_for_timeout(500)
                    try:
                        if await radio_to_click.is_checked(timeout=1000):
                            log.info("✓ เลือก PromptPay เรียบร้อย")
                            selected = True
                    except:
                        pass
                else:
                    # fallback: คลิกที่ตัวข้อความโดยตรง (กรณี custom UI ไม่ใช้ radio)
                    log.info("  ไม่เจอ radio — คลิกที่ text element")
                    await pp_text.click(timeout=2000, force=True)
                    await page.wait_for_timeout(500)
                    selected = True  # ถือว่าคลิกไปแล้ว

        except Exception as e:
            log.warning(f"get_by_text PromptPay ล้มเหลว: {e}")

        # fallback: ถ้า exact text ไม่เจอ ลอง label selector (ไม่ใช้ div/payment-option-0)
        if not selected:
            log.info("ลอง fallback selector: label:has-text('PromptPay')")
            try:
                lbl = page.locator("label:has-text('PromptPay')").first
                if await lbl.is_visible(timeout=1500):
                    await lbl.scroll_into_view_if_needed()
                    await page.wait_for_timeout(300)
                    radio = lbl.locator("input[type='radio']").first
                    if await radio.count() > 0:
                        await radio.click(timeout=2000, force=True)
                    else:
                        await lbl.click(timeout=2000, force=True)
                    await page.wait_for_timeout(500)
                    log.info("คลิก PromptPay label แล้ว (fallback)")
                    selected = True
            except Exception as e:
                log.warning(f"  label fallback ล้มเหลว: {e}")

        if not selected:
            log.warning("⚠️  ไม่พบ PromptPay option ที่หน้า cart หรือคลิกไม่สำเร็จ")

        await save_screenshot(page, "debug_after_select_promptpay.png", debug_screenshots)

        # ======== แล้วค่อยกด Place Order ========
        log.info("กำลังหาปุ่ม Place Order...")

        place_order_selectors = [
            'button:has-text("Place Order")',
            'button:has-text("สั่งซื้อ")',
            'button:has-text("ดำเนินการต่อ")',
            'button:has-text("Proceed")',
            '[role="button"]:has-text("Place Order")',
            '[role="button"]:has-text("สั่งซื้อ")',
        ]

        place_order_btn = None
        for selector in place_order_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.count() > 0 and await btn.is_visible(timeout=1000):
                    place_order_btn = btn
                    log.info(f"พบปุ่ม Place Order ด้วย selector: {selector}")
                    break
            except:
                continue

        if not place_order_btn:
            log.error("✗ ไม่พบปุ่ม Place Order")
            await save_screenshot(page, "debug_no_place_order.png", debug_screenshots)
            return False

        log.info("=" * 60)
        log.info("✓ ถึงหน้า Place Order พร้อม PromptPay แล้ว")
        log.info("✓ โปรแกรมจะหยุดที่นี่ — กรุณากด Place Order ในหน้าต่างเบราว์เซอร์")
        log.info("=" * 60)

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

    browser = await playwright.chromium.launch(headless=headless)

    # Block ทรัพยากรที่ไม่จำเป็น เพื่อโหลดหน้าเร็วขึ้น
    context = await browser.new_context(**session_kwargs)

    # Block ทรัพยากรที่ไม่จำเป็น
    async def block_resources(route):
        resource_type = route.request.resource_type
        if resource_type in ['image', 'stylesheet', 'font', 'media']:
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
            # โหลดหน้าครั้งแรก หรือเมื่อไม่ได้อยู่ที่หน้าสินค้า
            if attempt == 1 or "/product/" not in page.url:
                await page.goto(product_url, wait_until="domcontentloaded", timeout=10_000)

                # ครั้งแรก: รอให้หน้าโหลดเสร็จจริง ๆ
                if attempt == 1:
                    log.info("โหลดหน้าครั้งแรก — รอให้หน้าโหลดเสร็จ...")
                    await page.wait_for_timeout(1500)
                else:
                    await page.wait_for_timeout(400)

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

            # ปิด modal ที่ค้างอยู่จาก check_size_in_stock() ก่อน
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                log.info("ปิด modal ที่ค้างอยู่")
            except:
                pass

            # ปิด resource blocking เพื่อให้ checkout ทำงานปกติ
            await page.unroute("**/*")

            success, reason = await proceed_to_checkout(page, preferred_sizes, debug_screenshots)

            if success:
                # ปิด modal + กด Place Order + เลือก PromptPay แล้วหยุดรอ
                paid = await select_promptpay(page, debug_screenshots)
                if paid:
                    log.info("=" * 60)
                    log.info("✓ ถึงหน้า Place Order พร้อม PromptPay แล้ว")
                    log.info("✓ โปรแกรมจะหยุดที่นี่ — กรุณากด Place Order ในหน้าต่างเบราว์เซอร์")
                    log.info("=" * 60)
                else:
                    log.warning("=" * 60)
                    log.warning("⚠️  มีปัญหาในขั้นตอนชำระเงิน")
                    log.warning("⚠️  กรุณาดำเนินการเองในหน้าต่างเบราว์เซอร์")
                    log.warning("=" * 60)
                # ออกจาก loop ทันที — ไม่วนซ้ำ
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
