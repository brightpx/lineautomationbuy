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
            "is_test": False
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

        # รอ modal ปรากฏและโหลดเสร็จ — ลด timeout จาก 2000 → 1000
        try:
            await page.wait_for_selector("button[role='button']:has-text('y'), button[role='button']:has-text('m')", timeout=1_000)
        except:
            pass

        # ลด wait จาก 200ms → 50ms
        await page.wait_for_timeout(50)

        # เช็คไซส์ตามลำดับ priority
        for size in sizes:
            # ลองหลาย selector
            # Playwright ใช้ :text("...") สำหรับ exact match
            selectors = [
                f'button:text("{size}")',           # button exact text
                f'[role="button"]:text("{size}")',  # role=button exact text
                f'div:text("{size}")',              # div exact text
                f"button:has-text('{size}')",       # button contains (fallback)
            ]

            matching_buttons = None
            count = 0

            for selector in selectors:
                try:
                    matching_buttons = page.locator(selector)
                    count = await matching_buttons.count()
                    if count > 0:
                        log.info(f"  พบปุ่ม '{size}' ด้วย selector: {selector} ({count} ปุ่ม)")
                        break
                except Exception as e:
                    log.warning(f"  Selector '{selector}' ล้มเหลว: {e}")
                    continue

            # ถ้าไม่เจอเลย รอเพิ่ม — ลดจาก 300ms → 100ms
            if count == 0:
                await page.wait_for_timeout(100)
                for selector in selectors:
                    try:
                        matching_buttons = page.locator(selector)
                        count = await matching_buttons.count()
                        if count > 0:
                            log.info(f"  หลังรอ: พบปุ่ม '{size}' ด้วย selector: {selector} ({count} ปุ่ม)")
                            break
                    except:
                        continue

            if count == 0:
                continue

            for i in range(count):
                btn = matching_buttons.nth(i)

                try:
                    btn_text = await btn.text_content()
                    btn_text_clean = btn_text.strip() if btn_text else ""
                except:
                    continue

                # ตรวจสอบว่าเป็นไซส์เดียว
                if btn_text_clean.lower() != size.lower():
                    log.info(f"    ข้าม: '{btn_text_clean}' (ไม่ตรงกับ '{size}')")
                    continue

                try:
                    is_disabled = await btn.is_disabled()
                    is_visible = await btn.is_visible()
                except:
                    continue

                log.info(f"    ปุ่ม {i}: '{btn_text_clean}' (disabled={is_disabled}, visible={is_visible})")

                # เลือกปุ่มที่ไม่ disabled และ visible
                if not is_disabled and is_visible:
                    await btn.click(force=True)
                    log.info(f"✓ เลือกไซส์: {size} (text='{btn_text_clean}')")
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(50)
                    return True, size

        # ปิด modal
        log.warning(f"ไม่พบไซส์ {sizes} ที่ใช้งานได้")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(50)
        return False, ""

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


async def proceed_to_checkout(page: Page, sizes: list[str]) -> tuple[bool, str]:
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

        # Debug: ตรวจสอบแต่ละปุ่ม
        for i in range(btn_count):
            btn = buy_btn.nth(i)
            is_visible = await btn.is_visible()
            is_disabled = await btn.is_disabled()
            log.info(f"  ปุ่ม Buy Now {i}: visible={is_visible}, disabled={is_disabled}")

        # คลิกปุ่มสุดท้าย (ปุ่มหลัก ไม่ใช่ปุ่มใน modal) ด้วย force=True
        try:
            await buy_btn.last.click(timeout=5_000, force=True)
            log.info("✓ กด Buy Now สำเร็จ (เปิด modal)...")
        except Exception as e:
            log.error(f"✗ ไม่สามารถกด Buy Now ได้: {e}")
            await page.screenshot(path="debug_click_failed.png")
            return False, "error"

        # Step 2 — รอให้ปุ่มไซส์ปรากฏ (แสดงว่า modal เปิดแล้ว)
        try:
            await page.wait_for_selector(
                "button:has-text('y'), button:has-text('m'), [class*='size'] button, [class*='Size'] button",
                timeout=1_500,
                state="visible"
            )
            log.info("✓ Modal เปิดแล้ว — พบปุ่มไซส์")
        except:
            log.warning("Modal อาจยังโหลดไม่เสร็จ — ลองต่อ")

        await page.wait_for_timeout(200)

        # ปิด popup ที่อาจขวาง modal (LINE POINTS, โปรโมชั่น)
        log.info("ตรวจสอบ popup ใน modal...")
        modal_popup_selectors = [
            "button:has-text('Close')",
            "button:has-text('ปิด')",
            "button:has-text('ไม่ใช้')",
            "button:has-text('ข้าม')",
            "button:has-text('×')",
            "[data-testid*='close']",
            "[aria-label*='close' i]",
            "[role='dialog'] button:has-text('Close')",
            "[role='dialog'] button:has-text('ปิด')",
        ]

        for selector in modal_popup_selectors:
            try:
                popup_btn = page.locator(selector).first
                if await popup_btn.is_visible(timeout=500):
                    await popup_btn.click(timeout=1500, force=True)
                    log.info(f"✓ ปิด popup ใน modal ด้วย: {selector}")
                    await page.wait_for_timeout(300)  # เพิ่ม wait หลังปิด popup
                    break
            except:
                continue

        # รอให้ modal stabilize หลังปิด popup
        await page.wait_for_timeout(200)

        # ตรวจสอบ URL หลังกด Buy Now
        current_url = page.url
        log.info(f"URL หลังกด Buy Now: {current_url}")

        # ถ้าไปหน้า checkout/cart แล้ว = ระบบเพิ่มสินค้าเข้าตะกร้าอัตโนมัติ (สำเร็จ)
        if "/checkout/cart" in current_url:
            log.info("✓ ระบบเพิ่มสินค้าเข้าตะกร้าอัตโนมัติแล้ว — ข้ามขั้นตอนเลือกไซส์")

            # ปิด popup ที่หน้า cart ทันที (LINE POINTS, โปรโมชั่น)
            log.info("ปิด popup ที่หน้า cart...")
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                log.info("✓ กด Escape")
            except:
                pass

            # ลองหาปุ่มปิด popup
            close_selectors = [
                "button:has-text('Close')",
                "button:has-text('ปิด')",
                "button:has-text('×')",
                "button:has-text('X')",
                "button:has-text('ไม่ใช้')",
                "[aria-label*='close' i]",
                "[data-testid*='close']",
            ]

            for selector in close_selectors:
                try:
                    close_btn = page.locator(selector).first
                    if await close_btn.is_visible(timeout=500):
                        await close_btn.click(timeout=1000, force=True)
                        log.info(f"✓ ปิด popup ด้วย: {selector}")
                        await page.wait_for_timeout(300)
                        break
                except:
                    continue

            return True, "success"

        # ถ้าออกจากหน้า product ไปที่อื่น (ไม่ใช่ checkout) = error
        if "/product/" not in current_url:
            log.error(f"ออกจากหน้า product ไปที่ไม่คาดคิด: {current_url}")
            await page.screenshot(path="debug_wrong_page.png")
            return False, "error"

        # Debug: Screenshot modal หลังเปิด
        try:
            await page.screenshot(path="debug_modal.png")
            log.info("Screenshot modal: debug_modal.png")
        except:
            pass

        # Step 3 — เลือกไซซ์ตามลำดับ priority
        picked = False
        selected_size = ""

        # ลองหาปุ่ม dropdown ที่แสดงรายการไซส์ทั้งหมด (เช่น "12m, 2y, 3y, ...")
        # ถ้าเจอ = ต้องกดเพื่อเปิด dropdown ก่อน
        try:
            dropdown_selectors = [
                "button:has-text(',')",  # ปุ่มที่มี comma = รายการหลายไซส์
                "[role='button']:has-text(',')",
                "div:has-text(',')[role='button']",
            ]
            for selector in dropdown_selectors:
                dropdown = page.locator(selector).first
                if await dropdown.count() > 0 and await dropdown.is_visible(timeout=500):
                    dropdown_text = await dropdown.text_content()
                    log.info(f"พบ dropdown ไซส์: '{dropdown_text.strip()}' — กดเพื่อเปิด")
                    await dropdown.click(force=True)
                    await page.wait_for_timeout(500)
                    break
        except Exception as e:
            log.warning(f"ไม่มี dropdown หรือไม่สามารถกดได้: {e}")

        for size in sizes:
            log.info(f"กำลังหาไซส์: {size}")

            # ลองหลาย selector เพราะ LINE Shopping อาจใช้ div หรือ button
            selectors = [
                f'button:text("{size}")',           # button exact text
                f'[role="button"]:text("{size}")',  # role=button exact text
                f'div:text("{size}")',              # div exact text
                f"button:has-text('{size}')",       # button contains (fallback)
            ]

            matching_buttons = None
            count = 0

            for selector in selectors:
                try:
                    matching_buttons = page.locator(selector)
                    count = await matching_buttons.count()
                    if count > 0:
                        log.info(f"  พบปุ่ม '{size}' ด้วย selector: {selector} ({count} ปุ่ม)")
                        break
                except Exception as e:
                    log.warning(f"  Selector '{selector}' ล้มเหลว: {e}")
                    continue

            # ถ้าไม่เจอเลย ให้รอเพิ่มอีกนิด แล้วลองใหม่
            if count == 0:
                log.info(f"  ไม่เจอปุ่ม '{size}' — รอ modal โหลดเพิ่ม...")
                await page.wait_for_timeout(300)

                # ลองอีกครั้งหลังรอ
                for selector in selectors:
                    try:
                        matching_buttons = page.locator(selector)
                        count = await matching_buttons.count()
                        if count > 0:
                            log.info(f"  พบปุ่ม '{size}' หลังรอ: {selector} ({count} ปุ่ม)")
                            break
                    except Exception as e:
                        continue

                for selector in selectors:
                    try:
                        matching_buttons = page.locator(selector)
                        count = await matching_buttons.count()
                        if count > 0:
                            log.info(f"  หลังรอ: พบปุ่ม '{size}' ด้วย selector: {selector} ({count} ปุ่ม)")
                            break
                    except:
                        continue

            if count == 0:
                log.info(f"  ไม่พบปุ่ม '{size}' เลย")
                continue

            for i in range(count):
                btn = matching_buttons.nth(i)

                try:
                    btn_text = await btn.text_content()
                    btn_text_clean = btn_text.strip() if btn_text else ""
                except:
                    btn_text_clean = ""

                # ตรวจสอบว่าเป็นไซส์เดียว (ไม่ใช่รายการหลายไซส์)
                if btn_text_clean.lower() != size.lower():
                    log.info(f"    ข้าม: '{btn_text_clean}' (ไม่ตรงกับไซส์ {size})")
                    continue

                try:
                    is_disabled = await btn.is_disabled()
                    is_visible = await btn.is_visible()
                except:
                    is_disabled = True
                    is_visible = False

                log.info(f"    ปุ่ม {i}: '{btn_text_clean}' (disabled={is_disabled}, visible={is_visible})")

                # เลือกปุ่มที่ไม่ disabled และ visible
                if not is_disabled and is_visible:
                    try:
                        await btn.click(force=True, timeout=2000)
                        selected_size = size
                        picked = True
                        log.info(f"✓ เลือกไซส์: {size} (text='{btn_text_clean}')")
                        break
                    except Exception as e:
                        log.warning(f"    ไม่สามารถกดปุ่ม {i} ได้: {e}")
                        continue

            if picked:
                break

        if not picked:
            log.warning("ไซซ์ %s หมดทุกตัวแล้ว (sold out) — รอเติมของ...", sizes)

            # ปิด modal ก่อนออก
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(100)
            except Exception:
                pass
            return False, "sold_out"

        # ลด wait จาก 200ms → 100ms
        await page.wait_for_timeout(100)

        # Step 4 — กด Buy Now ใน modal (ปุ่มแรกใน modal ที่ไม่ disabled)
        # หาปุ่ม Buy Now ทั้งหมด แล้วเลือกปุ่มที่อยู่ใน modal (visible)
        buy_now_buttons = page.get_by_role("button", name="Buy Now", exact=True)
        count = await buy_now_buttons.count()
        log.info(f"พบปุ่ม Buy Now {count} ปุ่ม")

        # Debug: Screenshot ก่อนกดปุ่ม
        try:
            await page.screenshot(path="debug_before_place_order.png")
            log.info("บันทึก screenshot: debug_before_place_order.png")
        except:
            pass

        # ลองกดปุ่ม Buy Now ที่ visible ทีละปุ่ม โดยใช้ force=True
        clicked = False
        for i in range(count):
            btn = buy_now_buttons.nth(i)
            try:
                is_visible = await btn.is_visible()
                is_disabled = await btn.is_disabled()

                # Debug: แสดงข้อมูลเพิ่มเติม
                try:
                    bbox = await btn.bounding_box()
                    log.info(f"  ปุ่ม Buy Now {i}: visible={is_visible}, disabled={is_disabled}, bbox={bbox}")
                except:
                    log.info(f"  ปุ่ม Buy Now {i}: visible={is_visible}, disabled={is_disabled}, bbox=None")

                if is_visible and not is_disabled:
                    # ลองกดและดูว่า URL เปลี่ยนหรือไม่
                    log.info(f"  พยายามกดปุ่มที่ {i}...")
                    await btn.click(timeout=2_000, force=True)  # ลด timeout จาก 3000 → 2000
                    log.info(f"  ✓ กด Buy Now ปุ่มที่ {i} สำเร็จ")
                    clicked = True

                    # ลดการรอจาก 500ms → 200ms
                    await page.wait_for_timeout(200)
                    log.info(f"  URL หลังกด: {page.url}")
                    break
            except Exception as e:
                log.warning(f"  ปุ่ม {i} กดไม่ได้: {e}")
                continue

        if not clicked:
            log.error("ไม่สามารถกดปุ่ม Buy Now ใน modal ได้")

            # Debug: Screenshot เมื่อกดไม่ได้
            try:
                await page.screenshot(path="debug_failed_click.png")
                log.info("บันทึก screenshot เมื่อกดไม่ได้: debug_failed_click.png")
            except:
                pass

            await page.keyboard.press("Escape")
            return False, "error"

        log.info("กด Buy Now ใน modal...")

        # Step 5 — รอออกจากหน้า product
        log.info("รอเปลี่ยนหน้า (ออกจาก /product/)...")

        # รอให้ URL เปลี่ยนจริง ๆ — ลดการรอจาก 1000ms → 500ms
        for attempt in range(5):
            await page.wait_for_timeout(500)
            current_url = page.url
            log.info(f"  ตรวจสอบครั้งที่ {attempt + 1}: {current_url}")

            if "/product/" not in current_url:
                log.info("✓ ถึงหน้า checkout/cart: %s", current_url)

                # รอให้หน้า checkout โหลดเสร็จ
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)

                # ลดการรอจาก 1000ms → 500ms
                await page.wait_for_timeout(500)
                final_url = page.url

                if "/product/" in final_url:
                    log.error("✗ ถูก redirect กลับหน้า product: %s", final_url)

                    # Debug: Screenshot
                    try:
                        await page.screenshot(path="debug_redirect_back.png")
                        log.info("บันทึก screenshot redirect: debug_redirect_back.png")
                    except:
                        pass

                    return False, "error"
                else:
                    log.info("✓ ยืนยันอยู่หน้า checkout: %s", final_url)
                    return True, "success"

            if attempt < 4:
                log.info(f"  ยังอยู่หน้า product — รอต่อ...")

        log.error("✗ Timeout — ไม่สามารถออกจากหน้า product ได้")
        return False, "error"

    except PWTimeout:
        log.error("⏱️  Timeout — URL ปัจจุบัน: %s", page.url)

        # Debug: Screenshot เมื่อ timeout
        try:
            await page.screenshot(path="debug_timeout.png")
            log.info("บันทึก screenshot timeout: debug_timeout.png")
        except:
            pass

        is_checkout = "/product/" not in page.url
        if is_checkout:
            log.info("✓ แม้ timeout แต่อยู่หน้า checkout แล้ว")
        else:
            log.error("✗ ยังอยู่หน้า product — การซื้อล้มเหลว")
        return is_checkout, "success" if is_checkout else "error"
    except Exception as e:
        log.error("❌ Error: %s", e, exc_info=True)

        # Debug: Screenshot เมื่อเกิด error
        try:
            await page.screenshot(path="debug_error.png")
            log.info("บันทึก screenshot error: debug_error.png")
        except:
            pass

        return False, "error"


async def select_promptpay(page: Page) -> bool:
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
        await page.wait_for_timeout(2000)

        log.info("URL ปัจจุบัน: %s", page.url)

        # Debug: Screenshot หน้า checkout ก่อนปิด popup
        try:
            await page.screenshot(path="debug_checkout.png")
            log.info("บันทึก screenshot checkout: debug_checkout.png")
        except:
            pass

        # ปิด popup/modal ที่หน้า checkout (LINE POINTS, LINE Pay, โปรโมชั่น)
        log.info("ตรวจสอบ popup ที่หน้า checkout...")

        # รอให้ popup โหลดเสร็จก่อน (ถ้ามี)
        await page.wait_for_timeout(1000)

        # ลอง Escape key ก่อน (เร็วที่สุด)
        for _ in range(3):
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(400)
                log.info("✓ กด Escape เพื่อปิด popup")
            except:
                pass

        close_selectors = [
            # ปุ่มปิด LINE POINTS popup โดยเฉพาะ — ลองทุกรูปแบบ
            "button:has-text('Shop, earn, and use LINE POINTS!')",
            "button:has-text('LINE POINTS')",
            "div:has-text('Shop, earn, and use LINE POINTS!')",
            "[role='button']:has-text('LINE POINTS')",
            "[role='button']:has-text('Shop')",
            "button:has-text('Got it')",
            "button:has-text('รับทราบ')",
            "button:has-text('เข้าใจแล้ว')",
            "button:has-text('OK')",
            "button:has-text('Close')",
            "button:has-text('ปิด')",
            "button:has-text('ไม่ใช้')",
            "button:has-text('ไม่ใช้คะแนน')",
            "button:has-text('ข้าม')",
            "button:has-text('Skip')",
            "button:has-text('×')",
            "button:has-text('X')",
            "button:has-text('✕')",
            "[data-testid='campaign-close-button']",
            "[data-testid*='close']",
            "[data-testid*='dismiss']",
            "[data-testid*='modal-close']",
            "[data-testid*='popup-close']",
            "[aria-label*='close' i]",
            "[aria-label*='Close' i]",
            "[aria-label*='ปิด' i]",
            "[aria-label*='dismiss' i]",
            "button[class*='close' i]",
            "button[class*='Close' i]",
            "[class*='modal'] button:has-text('×')",
            "[class*='Modal'] button:has-text('×')",
            "[class*='popup'] button:has-text('×')",
            "[class*='dialog'] button:has-text('×')",
            "[role='dialog'] button:has-text('Close')",
            "[role='dialog'] button:has-text('ปิด')",
            "[role='dialog'] button[aria-label*='close' i]",
            # ลอง click ที่มุมบนขวาของ modal (ปกติปุ่มปิดอยู่ที่นี่)
            "[class*='modal'] [class*='header'] button",
            "[class*='Modal'] [class*='Header'] button",
            # ลองหา SVG icon ปิด
            "button:has(svg)",
            "[role='button']:has(svg)",
        ]

        popup_closed = False
        for attempt in range(5):  # เพิ่มจาก 3 → 5 รอบ
            if popup_closed:
                break

            log.info(f"  ครั้งที่ {attempt + 1}: ค้นหา popup...")
            for selector in close_selectors:
                try:
                    close_btn = page.locator(selector).first
                    if await close_btn.is_visible(timeout=1000):  # เพิ่ม timeout จาก 800 → 1000
                        btn_text = await close_btn.text_content() or ""
                        await close_btn.click(timeout=2000, force=True)  # เพิ่ม timeout จาก 1500 → 2000
                        log.info(f"✓ ปิด popup ด้วย selector: {selector} (text='{btn_text.strip()}')")
                        await page.wait_for_timeout(800)  # เพิ่มจาก 500 → 800
                        popup_closed = True
                        break
                except Exception as e:
                    continue

            # ถ้ายังไม่ปิด ลอง Escape อีกครั้ง
            if not popup_closed:
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(400)
                    log.info("  กด Escape อีกครั้ง")
                except:
                    pass

            if not popup_closed and attempt < 4:  # เปลี่ยนจาก < 2 → < 4
                await page.wait_for_timeout(1000)  # เพิ่มจาก 800 → 1000

        if not popup_closed:
            log.warning("  ⚠️  ไม่พบ popup หรือปิดไม่สำเร็จ — ลองดำเนินการต่อ")
        else:
            log.info("  ✓ ปิด popup สำเร็จ")

        # Debug: Screenshot หลังปิด popup
        try:
            await page.screenshot(path="debug_after_close_popup.png")
            log.info("บันทึก screenshot หลังปิด popup: debug_after_close_popup.png")
        except:
            pass

        # ======== เลือก PromptPay ก่อน (ที่หน้า cart) ========
        log.info("กำลังหา PromptPay option ที่หน้า cart...")

        promptpay_selectors = [
            "[data-testid='payment-option-0']",
            "button:has-text('PromptPay')",
            "div:has-text('PromptPay')",
            "[data-testid*='promptpay']",
            "[data-testid*='qr']",
            "[class*='payment']:has-text('PromptPay')",
        ]

        selected = False
        for selector in promptpay_selectors:
            try:
                card = page.locator(selector).first
                if await card.is_visible(timeout=2000):
                    log.info(f"พบ PromptPay ด้วย selector: {selector}")
                    await card.click(timeout=3000, force=True)
                    log.info("✓ เลือก PromptPay เรียบร้อย")
                    await page.wait_for_timeout(500)
                    selected = True
                    break
            except:
                continue

        if not selected:
            log.warning("⚠️  ไม่พบ PromptPay option ที่หน้า cart")

        # Debug: Screenshot หลังเลือก PromptPay
        try:
            await page.screenshot(path="debug_after_select_promptpay.png")
            log.info("บันทึก screenshot หลังเลือก PromptPay")
        except:
            pass

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
            await page.screenshot(path="debug_no_place_order.png")
            return False

        log.info("=" * 60)
        log.info("✓ ถึงหน้า Place Order พร้อม PromptPay แล้ว")
        log.info("✓ โปรแกรมจะหยุดที่นี่ — กรุณากด Place Order ในหน้าต่างเบราว์เซอร์")
        log.info("=" * 60)

        return True

    except PWTimeout as e:
        log.error("⏱️  Timeout ตอนเลือก PromptPay: %s", e)
        try:
            await page.screenshot(path="debug_promptpay_timeout.png")
        except:
            pass
        return False
    except Exception as e:
        log.error("❌ Error ตอนเลือก PromptPay: %s", e, exc_info=True)
        try:
            await page.screenshot(path="debug_promptpay_error.png")
        except:
            pass
        return False


async def monitor_and_buy(playwright, product_url: str, preferred_sizes: list[str],
                          check_interval: int, session_file: str, headless: bool, is_test: bool = False) -> None:
    """วนตรวจสินค้าทุก CHECK_INTERVAL วินาที แล้วกดซื้อทันทีที่เปิดขาย"""
    session_kwargs = {}
    if Path(session_file).exists():
        session_kwargs["storage_state"] = session_file
        log.info("โหลด session จาก %s", session_file)
    else:
        log.warning("ไม่พบ session file — อาจต้องล็อกอินก่อน (รัน: python bot.py login)")

    browser = await playwright.chromium.launch(headless=headless)

    # Block ทรัพยากรที่ไม่จำเป็น เพื่อโหลดหน้าเร็วขึ้น
    context = await browser.new_context(
        **session_kwargs,
        # Block images, fonts, media เพื่อความเร็ว
        # เก็บเฉพาะ document, script, xhr สำหรับ API calls
    )

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
                # ใช้ domcontentloaded แทน networkidle เพื่อความเร็ว
                await page.goto(product_url, wait_until="domcontentloaded", timeout=10_000)

                # ครั้งแรก: รอให้หน้าโหลดเสร็จจริง ๆ (2 วินาที)
                # ครั้งถัดไป: รอสั้น ๆ (500ms)
                if attempt == 1:
                    log.info("โหลดหน้าครั้งแรก — รอ 2 วินาทีให้หน้าโหลดเสร็จ...")
                    await page.wait_for_timeout(2000)
                else:
                    await page.wait_for_timeout(500)

            available = await is_product_available(page)

            if not available:
                log.info("[ครั้งที่ %d] สถานะสินค้า: ยังไม่เปิดขาย", attempt)
                await asyncio.sleep(check_interval)
                continue

            # สินค้าเปิดขาย — เช็คสต็อกโดยไม่ต้องโหลดหน้าใหม่
            in_stock, available_size = await check_size_in_stock(page, preferred_sizes)

            if not in_stock:
                sold_out_count += 1
                # โหลดหน้าใหม่ทุก 20 ครั้งเพื่อ refresh (เพิ่มจาก 10 → 20)
                if sold_out_count % 20 == 0:
                    log.info("[ครั้งที่ %d] ไซซ์ %s หมดทุกตัว — รีเฟรชหน้า...", attempt, preferred_sizes)
                    await page.reload(wait_until="domcontentloaded", timeout=8_000)
                    await page.wait_for_timeout(500)
                else:
                    log.info("[ครั้งที่ %d] ไซซ์ %s หมดทุกตัว — เช็คอีกครั้งในอีก %d วินาที",
                            attempt, preferred_sizes, check_interval)
                await asyncio.sleep(check_interval)
                continue

            # มีสต็อก — ซื้อเลย!
            log.info("[ครั้งที่ %d] พบสินค้ามีสต็อก (ไซซ์: %s) — เริ่มซื้อ!", attempt, available_size)

            # ปิด modal ที่ค้างอยู่จาก check_size_in_stock() ก่อน
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
                log.info("ปิด modal ที่ค้างอยู่")
            except:
                pass

            # ปิด resource blocking เพื่อให้ checkout ทำงานปกติ
            await page.unroute("**/*")

            success, reason = await proceed_to_checkout(page, preferred_sizes)

            if success:
                # ปิด modal + กด Place Order + เลือก PromptPay แล้วหยุดรอ
                paid = await select_promptpay(page)
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
                break  # ออกจาก loop ทันทีโดยไม่รอ asyncio.sleep()
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

        # break อยู่นอก try/except — exception ใดๆ ไม่สามารถทำให้วนลูปต่อได้
        if do_exit:
            break

        await asyncio.sleep(check_interval)

    # ถ้าออกจาก loop เพราะถึงหน้า checkout แล้ว — รอไม่จำกัดเวลา
    if do_exit:
        log.info("เบราว์เซอร์จะเปิดค้างไว้ — กด Ctrl+C เพื่อออกจากโปรแกรม")
        try:
            # รอไม่จำกัดเวลา — user จะปิดเองหรือกด Ctrl+C
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
                is_test=config.get("is_test", False)
            )


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "buy"
    asyncio.run(main(mode))
