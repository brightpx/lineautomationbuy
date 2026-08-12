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
        await buy_btn.last.click(timeout=2_500, force=True)

        # รอ modal ปรากฏและโหลดเสร็จ
        try:
            await page.wait_for_selector("button[role='button']:has-text('y'), button[role='button']:has-text('m')", timeout=3_000)
            log.info("Modal โหลดเสร็จ")
        except:
            log.warning("Modal อาจยังโหลดไม่เสร็จ — ลองต่อ")

        await page.wait_for_timeout(500)

        # เช็คไซส์ตามลำดับ priority
        for size in sizes:
            # หาปุ่มที่มีข้อความตรงกับไซส์ exact (ไม่รวมปุ่มที่มีหลายไซส์)
            matching_buttons = page.locator(f"button:has-text('{size}')").filter(has_text=f"{size}")
            count = await matching_buttons.count()

            # ถ้าไม่เจอเลย รอเพิ่ม
            if count == 0:
                await page.wait_for_timeout(800)
                count = await matching_buttons.count()

            for i in range(count):
                btn = matching_buttons.nth(i)
                btn_text = await btn.text_content()
                btn_text_clean = btn_text.strip()

                # ต้องเป็นไซส์เดียว ไม่ใช่รายการหลายไซส์ (เช่น "12m, 2y, 3y...")
                # และไม่มี "Buy Now", "Add to cart", "Chat"
                if (',' in btn_text_clean or
                    'Buy Now' in btn_text_clean or
                    'Add to cart' in btn_text_clean or
                    'Chat' in btn_text_clean or
                    len(btn_text_clean) > 10):  # ไซส์ปกติไม่เกิน 10 ตัวอักษร
                    continue

                is_disabled = await btn.is_disabled()

                # เลือกปุ่มที่ไม่ disabled และเป็นไซส์เดียว
                if not is_disabled and btn_text_clean.lower() == size.lower():
                    await btn.click(force=True)
                    log.info(f"✓ เลือกไซส์: {size} (text='{btn_text_clean}')")
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(150)
                    return True, size

        # ปิด modal
        log.warning(f"ไม่พบไซส์ {sizes} ที่ใช้งานได้")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(150)
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
        # Step 1 — กด Buy Now เพื่อเปิด modal (ปุ่มเดียวก่อน modal = .last)
        # ใช้ force=True เพื่อบังคับคลิกแม้มี overlay blocking
        buy_btn = page.get_by_role("button", name="Buy Now", exact=True)
        await buy_btn.last.click(timeout=5_000, force=True)
        log.info("กด Buy Now (เปิด modal)...")

        # Step 2 — รอ modal ปรากฏและโหลดเสร็จ
        # รอให้ modal แสดง (รอ element ที่บ่งบอกว่า modal เปิดแล้ว)
        try:
            # รอให้มีปุ่มไซส์ปรากฏ (ไม่ใช่แค่ปุ่ม Buy Now)
            await page.wait_for_selector("button[role='button']:has-text('y'), button[role='button']:has-text('m')", timeout=3_000)
            log.info("Modal โหลดเสร็จ — พบปุ่มไซส์")
        except:
            log.warning("Modal อาจยังโหลดไม่เสร็จ — ลองต่อ")

        await page.wait_for_timeout(500)

        # Step 3 — เลือกไซซ์ตามลำดับ priority
        picked = False
        selected_size = ""

        for size in sizes:
            log.info(f"กำลังหาไซส์: {size}")

            # หาปุ่มที่มีข้อความตรงกับไซส์
            matching_buttons = page.locator(f"button:has-text('{size}')").filter(has_text=f"{size}")
            count = await matching_buttons.count()
            log.info(f"  พบปุ่มที่มีข้อความ '{size}': {count} ปุ่ม")

            # ถ้าไม่เจอเลย ให้รอเพิ่มอีกนิด (modal อาจยังโหลดไม่เสร็จ)
            if count == 0:
                log.info(f"  ไม่เจอปุ่ม '{size}' — รอ modal โหลดเพิ่ม...")
                await page.wait_for_timeout(800)
                count = await matching_buttons.count()
                log.info(f"  หลังรอ: พบปุ่ม '{size}': {count} ปุ่ม")

            for i in range(count):
                btn = matching_buttons.nth(i)
                btn_text = await btn.text_content()
                btn_text_clean = btn_text.strip()

                # กรองปุ่มที่ไม่ใช่ปุ่มเลือกไซส์
                # - มี comma (เช่น "12m, 2y, 3y...")
                # - มีคำว่า Buy Now, Add to cart, Chat
                # - ยาวเกินไป (ไซส์ปกติไม่เกิน 10 ตัวอักษร)
                if (',' in btn_text_clean or
                    'Buy Now' in btn_text_clean or
                    'Add to cart' in btn_text_clean or
                    'Chat' in btn_text_clean or
                    len(btn_text_clean) > 10):
                    log.info(f"    ข้าม: '{btn_text_clean}' (ไม่ใช่ปุ่มเลือกไซส์)")
                    continue

                is_disabled = await btn.is_disabled()
                log.info(f"    ปุ่ม {i}: '{btn_text_clean}' (disabled={is_disabled})")

                # เลือกปุ่มที่ไม่ disabled และเป็นไซส์เดียว
                if not is_disabled and btn_text_clean.lower() == size.lower():
                    await btn.click(force=True)
                    selected_size = size
                    picked = True
                    log.info(f"✓ เลือกไซส์: {size} (text='{btn_text_clean}')")
                    break

            if picked:
                break

        if not picked:
            log.warning("ไซซ์ %s หมดทุกตัวแล้ว (sold out) — รอเติมของ...", sizes)

            # ปิด modal ก่อนออก
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
            except Exception:
                pass
            return False, "sold_out"

        await page.wait_for_timeout(400)

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
                    await btn.click(timeout=3_000, force=True)  # ใช้ force=True
                    log.info(f"  ✓ กด Buy Now ปุ่มที่ {i} สำเร็จ")
                    clicked = True

                    # รอให้ URL เริ่มเปลี่ยน (อย่างน้อย 1 วินาที)
                    await page.wait_for_timeout(1000)
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

        # รอให้ URL เปลี่ยนจริง ๆ
        for attempt in range(5):
            await page.wait_for_timeout(2000)
            current_url = page.url
            log.info(f"  ตรวจสอบครั้งที่ {attempt + 1}: {current_url}")

            if "/product/" not in current_url:
                log.info("✓ ถึงหน้า checkout/cart: %s", current_url)

                # รอให้หน้า checkout โหลดเสร็จ
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)

                # ตรวจสอบว่ายังอยู่หน้า checkout จริง ๆ (ไม่ถูก redirect กลับ)
                await page.wait_for_timeout(2000)
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
      2. กด Place Order → ไปหน้า payment selection
      3. เลือก PromptPay card
      4. หยุดรอ — ให้ user กด Place Order เอง
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)

        # ปิด campaign modal ถ้ามี
        close_btn = page.locator("[data-testid='campaign-close-button']")
        try:
            await close_btn.click(timeout=3_000)
            log.info("ปิด campaign modal แล้ว")
            await page.wait_for_timeout(400)
        except Exception:
            pass

        # กด Place Order (cart → payment selection page)
        await page.get_by_role("button", name="Place Order").click(timeout=8_000)
        log.info("กด Place Order — รอหน้า payment...")
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        await page.wait_for_timeout(1_000)

        # เลือก PromptPay card บนหน้า payment selection
        promptpay_card = page.locator("[data-testid='payment-option-0']")
        await promptpay_card.wait_for(state="visible", timeout=8_000)
        await promptpay_card.click()
        log.info("เลือก PromptPay เรียบร้อย — รอคุณกด Place Order เพื่อยืนยันการชำระเงิน")
        return True

    except PWTimeout as e:
        log.error("Timeout ตอนเลือก PromptPay: %s", e)
        return False
    except Exception as e:
        log.error("Error ตอนเลือก PromptPay: %s", e)
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
                # ลดเวลารอจาก 1.5s → 0.8s
                await page.wait_for_timeout(800)

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

            # ปิด resource blocking เพื่อให้ checkout ทำงานปกติ
            await page.unroute("**/*")

            success, reason = await proceed_to_checkout(page, preferred_sizes)

            if success:
                # ปิด modal + กด Place Order + เลือก PromptPay แล้วหยุดรอ
                paid = await select_promptpay(page)
                if paid:
                    log.info("เลือก PromptPay เรียบร้อย — กรุณากด Place Order เพื่อยืนยันคำสั่งซื้อในหน้าต่างเบราว์เซอร์")
                else:
                    log.warning("มีปัญหาในขั้นตอนชำระเงิน — กรุณาดำเนินการเองในหน้าต่างเบราว์เซอร์")
                # ตั้ง flag ออกลูป — อยู่นอก try เพื่อป้องกัน exception วนซ้ำ
                do_exit = True
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
