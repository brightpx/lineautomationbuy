"""
LINE Shopping — Direct Checkout Script
---------------------------------------
1. อ่าน config.json  (product_url + preferred_sizes)
2. เปิดหน้า product พร้อม intercept ทุก network response / script tag
3. ดึง productVariants จาก API JSON response ที่ URL มี productId ก่อน
4. ถ้าไม่ได้จาก API → scan script tags → window.__NUXT__ (กรองตาม productId)
5. หา variant ที่ตรงกับ size แล้วสร้าง Checkout URL และ navigate ทันที
"""

import asyncio
import json
import logging
import re
import urllib.parse
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, Page, Response, TimeoutError as PWTimeout

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("checkout_direct")

# ==================== CONFIG ====================
CONFIG_FILE = "config.json"


def load_config() -> dict:
    path = Path(CONFIG_FILE)
    if not path.exists():
        raise FileNotFoundError(f"ไม่พบ {CONFIG_FILE}")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_product_id(product_url: str) -> int:
    """แยก productId จาก URL เช่น .../product/1008243591 → 1008243591"""
    parts = product_url.rstrip("/").split("/")
    try:
        idx = parts.index("product")
        return int(parts[idx + 1])
    except (ValueError, IndexError, TypeError) as e:
        raise ValueError(f"ไม่สามารถแยก productId จาก URL: {product_url}") from e


def parse_shop_handle(product_url: str) -> str:
    """แยก shop handle เช่น .../@ be_bekids/... → @be_bekids"""
    parts = product_url.rstrip("/").split("/")
    for p in parts:
        if p.startswith("@"):
            return p
    raise ValueError(f"ไม่พบ shop handle ใน URL: {product_url}")


def build_checkout_url(shop_handle: str, product_id: int, variant_id: int, quantity: int = 1) -> str:
    """สร้าง Checkout URL พร้อม URL-encoded JSON"""
    data = {
        "items": [
            {
                "productId": product_id,
                "productVariantId": variant_id,
                "quantity": quantity,
            }
        ]
    }
    encoded = urllib.parse.quote(json.dumps(data, separators=(",", ":")), safe="")
    return f"https://shop.line.me/{shop_handle}/checkout/cart?data={encoded}"


# ==================== VARIANT EXTRACTION ====================

def _dedup(variants: list[dict]) -> list[dict]:
    seen: set[int] = set()
    out: list[dict] = []
    for v in variants:
        if v["id"] not in seen:
            seen.add(v["id"])
            out.append(v)
    return out


def _extract_variant_list(obj) -> list[dict]:
    """
    ดึง {id, name} จาก list ของ variant objects โดยตรง
    รองรับ key: productVariants, variants, skus
    """
    results: list[dict] = []
    if isinstance(obj, list):
        for item in obj:
            if (
                isinstance(item, dict)
                and isinstance(item.get("id"), (int, float))
                and isinstance(item.get("name"), str)
                and item.get("name")
            ):
                results.append({"id": int(item["id"]), "name": str(item["name"])})
    return results


def find_product_variants(obj, product_id: int, depth: int = 0, max_depth: int = 20) -> list[dict]:
    """
    Context-aware search:
    1. หา dict ที่มี 'id' == product_id ก่อน
    2. ดึง productVariants / variants / skus จาก dict นั้น
    3. ถ้าไม่พบ context ที่ตรง → fallback ค้นหา productVariants key ทั่วไป
    """
    if depth > max_depth:
        return []

    if isinstance(obj, dict):
        # ถ้า dict นี้คือ product object ที่เราต้องการ
        obj_id = obj.get("id") or obj.get("productId")
        if obj_id and int(obj_id) == product_id:
            for key in ("productVariants", "variants", "skus", "options"):
                if key in obj and isinstance(obj[key], list):
                    found = _extract_variant_list(obj[key])
                    if found:
                        return found
            # อาจซ้อนอยู่ลึกกว่า — ค้นต่อใน dict นี้
            for v in obj.values():
                found = find_product_variants(v, product_id, depth + 1, max_depth)
                if found:
                    return found

        # ค้นหา productVariants key ที่ลงไปอีก level
        for key in ("productVariants", "variants", "skus"):
            if key in obj and isinstance(obj[key], list):
                found = _extract_variant_list(obj[key])
                if found:
                    log.debug("  พบ '%s' key ที่ depth=%d (id context ไม่ match)", key, depth)
                    # เก็บไว้แต่อย่าคืนทันที — หา context ที่ดีกว่าก่อน
                    pass

        for v in obj.values():
            found = find_product_variants(v, product_id, depth + 1, max_depth)
            if found:
                return found

    elif isinstance(obj, list):
        for item in obj:
            found = find_product_variants(item, product_id, depth + 1, max_depth)
            if found:
                return found

    return []


def find_variants_anywhere(obj, depth: int = 0, max_depth: int = 20) -> list[dict]:
    """
    Fallback ระดับ 1: ค้นหา productVariants/variants/skus key ทุกที่
    """
    if depth > max_depth:
        return []

    results: list[dict] = []

    if isinstance(obj, dict):
        for key in ("productVariants", "variants", "skus"):
            if key in obj and isinstance(obj[key], list):
                found = _extract_variant_list(obj[key])
                results.extend(v for v in found if v["id"] > 10000)

        for v in obj.values():
            results.extend(find_variants_anywhere(v, depth + 1, max_depth))

    elif isinstance(obj, list):
        for item in obj:
            results.extend(find_variants_anywhere(item, depth + 1, max_depth))

    return results


def _recursive_id_name_search(obj, depth: int = 0, max_depth: int = 20) -> list[dict]:
    """
    Fallback ระดับ 2 (กว้างที่สุด): หา dict ที่มี 'id' (int) + 'name' (str) ทุกที่
    เหมือน search_variants_in_obj เดิม — ใช้เมื่อ Nuxt serialize เป็น flat array
    """
    if depth > max_depth:
        return []
    results: list[dict] = []
    if isinstance(obj, dict):
        if (
            "id" in obj and "name" in obj
            and isinstance(obj.get("id"), (int, float))
            and isinstance(obj.get("name"), str)
            and obj.get("name")
        ):
            results.append({"id": int(obj["id"]), "name": str(obj["name"])})
        for v in obj.values():
            results.extend(_recursive_id_name_search(v, depth + 1, max_depth))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_recursive_id_name_search(item, depth + 1, max_depth))
    return results


def parse_nuxt_flat_array(arr: list, product_id: int) -> list[dict]:
    """
    Nuxt serialize ข้อมูลเป็น flat array โดยค่าใน dict คือ INDEX ไปยัง element อื่น
    เช่น: {'id': 47, 'variantOptionValue1': 48}  →  id=arr[47], name=arr[48]
    
    ค้นหา dict ที่มี key 'id' และ 'variantOptionValue1' (pattern ของ LINE Shopping variant)
    แล้ว dereference ค่าออกมา
    """
    results: list[dict] = []
    n = len(arr)

    for item in arr:
        if not isinstance(item, dict):
            continue
        # LINE Shopping variant object มี key เหล่านี้
        if "id" not in item or "variantOptionValue1" not in item:
            continue

        id_ref = item.get("id")
        name_ref = item.get("variantOptionValue1")

        # ต้องเป็น index (int) ที่ valid
        if not isinstance(id_ref, int) or not isinstance(name_ref, int):
            continue
        if not (0 <= id_ref < n) or not (0 <= name_ref < n):
            continue

        actual_id = arr[id_ref]
        actual_name = arr[name_ref]

        if isinstance(actual_id, (int, float)) and isinstance(actual_name, str) and actual_name:
            results.append({"id": int(actual_id), "name": actual_name})

    return _dedup(results)


def extract_variants_from_json(data, product_id: int) -> list[dict]:
    """
    Main entry point — 4 ชั้น:
    0. Nuxt flat array dereference (LINE Shopping specific — __NUXT_DATA__)
    1. Context-aware: หา product object ที่ id == product_id แล้วดึง productVariants
    2. Key-based: ค้นหา productVariants/variants/skus key ทุกที่ (id > 10000)
    3. Broad: ค้นหา {id, name} ทุกที่ แล้ว filter id > 10000
    """
    # ชั้น 0 — Nuxt flat array (LINE Shopping __NUXT_DATA__ pattern)
    if isinstance(data, list):
        found = parse_nuxt_flat_array(data, product_id)
        if found:
            return found

    # ชั้น 1
    found = find_product_variants(data, product_id)
    if found:
        return _dedup(found)

    # ชั้น 2
    found = find_variants_anywhere(data)
    if found:
        return _dedup(found)

    # ชั้น 3 — broad fallback
    found = _recursive_id_name_search(data)
    found = [v for v in found if v["id"] > 10000]
    return _dedup(found)


async def get_variants_from_page(page: Page, product_id: int) -> list[dict]:
    """
    ดึง variants จากหน้าในลำดับต่อไปนี้ (เร็วสุด → ช้าสุด):
    1. __NUXT_DATA__ script tag  (embedded JSON — ไม่ต้องรอ network)
    2. window.__NUXT__ state object
    3. ค้นหาใน script tags ที่มีคำว่า productVariant และ productId
    """
    # --- วิธีที่ 1: __NUXT_DATA__ ---
    log.info("สแกน __NUXT_DATA__ script tag...")
    try:
        nuxt_raw = await page.evaluate(
            """() => {
                const el = document.getElementById('__NUXT_DATA__')
                    || document.querySelector('script[id*="nuxt"][type]')
                    || document.querySelector('script[type="application/json"]');
                return el ? el.textContent : null;
            }"""
        )
        if nuxt_raw:
            data = json.loads(nuxt_raw)
            variants = extract_variants_from_json(data, product_id)
            if variants:
                log.info("✓ ดึง variants จาก __NUXT_DATA__ ได้ %d รายการ", len(variants))
                return variants
    except Exception as e:
        log.debug("__NUXT_DATA__ ล้มเหลว: %s", e)

    # --- วิธีที่ 2: window.__NUXT__ ---
    log.info("สแกน window.__NUXT__...")
    try:
        raw = await page.evaluate(
            """() => {
                const n = window.__NUXT__ || window.__nuxt__;
                return n ? JSON.stringify(n) : null;
            }"""
        )
        if raw:
            data = json.loads(raw)
            variants = extract_variants_from_json(data, product_id)
            if variants:
                log.info("✓ ดึง variants จาก window.__NUXT__ ได้ %d รายการ", len(variants))
                return variants
    except Exception as e:
        log.debug("window.__NUXT__ ล้มเหลว: %s", e)

    # --- วิธีที่ 3: script tag scan (หา JSON ที่มี productId + productVariant) ---
    log.info("สแกน inline script tags...")
    try:
        scripts: list[str] = await page.evaluate(
            f"""() => Array.from(document.querySelectorAll('script:not([src])'))
                .map(s => s.textContent || '')
                .filter(t => t.includes('{product_id}') && t.includes('ariant'))"""
        )
        for text in scripts:
            # หา JSON object/array ที่ฝังอยู่ในตัวแปร JS
            for pattern in [
                r'(?:window\.\w+\s*=\s*)(\{[\s\S]+?\});?\s*\n',
                r'(?:__INITIAL_STATE__\s*=\s*)(\{[\s\S]+?\})',
                r'(\{".*?"productVariants?"[\s\S]*?\})',
            ]:
                for m in re.finditer(pattern, text):
                    try:
                        data = json.loads(m.group(1))
                        variants = extract_variants_from_json(data, product_id)
                        if variants:
                            log.info("✓ ดึง variants จาก script tag ได้ %d รายการ", len(variants))
                            return variants
                    except Exception:
                        continue
    except Exception as e:
        log.debug("script tag scan ล้มเหลว: %s", e)

    return []


def cookies_from_session(session_file: str) -> dict[str, str]:
    """แปลง Playwright storage_state cookies → dict สำหรับ httpx"""
    data = json.loads(Path(session_file).read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in data.get("cookies", [])}


async def fetch_variants_via_http(product_url: str, product_id: int, session_file: str) -> list[dict]:
    """
    ดึง variant จาก product page ผ่าน HTTP ธรรมดา (ไม่เปิด browser)
    ใช้ cookies จาก session_file เพื่อ auth
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    }
    cookies: dict[str, str] = {}
    if Path(session_file).exists():
        cookies = cookies_from_session(session_file)

    async with httpx.AsyncClient(
        headers=headers,
        cookies=cookies,
        follow_redirects=True,
        timeout=15.0,
    ) as client:
        log.info("ดึง HTML ผ่าน HTTP...")
        resp = await client.get(product_url)
        resp.raise_for_status()
        html = resp.text
        log.info("ได้รับ HTML %d chars (status %d)", len(html), resp.status_code)

    # Extract <script id="__NUXT_DATA__">...</script>
    m = re.search(r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>([\s\S]*?)</script>', html)
    if not m:
        # fallback: script type="application/json"
        m = re.search(r'<script[^>]+type=["\']application/json["\'][^>]*>([\s\S]*?)</script>', html)
    if not m:
        log.warning("ไม่พบ __NUXT_DATA__ ใน HTML")
        return []

    raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("parse __NUXT_DATA__ ล้มเหลว: %s", e)
        return []

    variants = extract_variants_from_json(data, product_id)
    if variants:
        log.info("✓ ดึง variants จาก HTTP+__NUXT_DATA__ ได้ %d รายการ", len(variants))
    return variants

# ==================== CHECKOUT HELPERS ====================

async def close_popup(page: Page, context: str = "") -> bool:
    """ปิด popup/modal ที่อาจขวาง"""
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(150)
    except Exception:
        pass

    combined_selector = (
        "button:has-text('×'), button:has-text('X'), button:has-text('Close'), "
        "button:has-text('ปิด'), button:has-text('Got it'), button:has-text('รับทราบ'), "
        "button:has-text('ไม่ใช้'), [data-testid*='close'], [aria-label*='close' i], "
        "button[class*='close' i]"
    )
    for _ in range(2):
        try:
            btn = page.locator(combined_selector).first
            if await btn.is_visible(timeout=500):
                log.info("  พบ popup (%s) — ปิด...", context)
                await btn.click(timeout=2000, force=True)
                await page.wait_for_timeout(200)
                log.info("  ✓ ปิด popup สำเร็จ")
                return True
        except Exception:
            pass
        await page.wait_for_timeout(300)
    return False


async def select_promptpay(page: Page) -> bool:
    """
    1. รอ cart page → กด Checkout ไปหน้า payment
    2. เลือก PromptPay ด้วย JS inject
    3. หา Place Order button แล้ว scroll ให้เห็น
    Returns True ถ้าพร้อม Place Order
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)

        # ── Step A: ถ้ายังอยู่หน้า cart → กด Checkout ──
        if "/checkout/cart" in page.url:
            log.info("URL มี /checkout/cart — ตรวจสอบว่าต้องกด Checkout หรือไม่...")

            # รอ Vue render เริ่มต้น
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except PWTimeout:
                pass
            await page.wait_for_timeout(500)

            # dump ปุ่มทั้งหมดที่เห็น เพื่อ debug
            btn_texts = await page.evaluate("""
                () => [...document.querySelectorAll('button,[role="button"]')]
                    .filter(b => b.offsetParent !== null)
                    .map(b => b.textContent.trim().slice(0, 40))
                    .filter(t => t.length > 0)
            """)
            log.info("ปุ่มที่เห็นบนหน้า: %s", btn_texts)

            # ถ้า PromptPay หรือ payment section โหลดแล้ว → ข้าม Step A
            pp_visible = await page.evaluate("""
                () => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null);
                    while (walker.nextNode()) {
                        if (walker.currentNode.textContent.trim().toLowerCase() === 'promptpay')
                            return true;
                    }
                    return false;
                }
            """)
            if pp_visible:
                log.info("✓ PromptPay ปรากฏแล้ว — ข้าม Step A")
            else:
                log.info("ยังไม่เห็น PromptPay — กด Checkout...")
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(200)
                except Exception:
                    pass
                await close_popup(page, "cart-checkout")

                checkout_selectors = [
                    'button:has-text("Checkout")',
                    'button:has-text("ชำระเงิน")',
                    'button:has-text("Place Order")',
                    'button:has-text("สั่งซื้อ")',
                    'button:has-text("Next")',
                    'button:has-text("ถัดไป")',
                    'button:has-text("Proceed")',
                    'button:has-text("Confirm")',
                ]
                clicked_checkout = False
                for sel in checkout_selectors:
                    try:
                        btn = page.locator(sel).first
                        if await btn.count() > 0:
                            disabled = await btn.is_disabled()
                            if not disabled:
                                await btn.click(timeout=3_000)
                                log.info("✓ กด '%s' สำเร็จ", sel)
                                clicked_checkout = True
                                break
                            else:
                                log.debug("  '%s' disabled — ข้าม", sel)
                    except Exception as e:
                        log.debug("  '%s' error: %s", sel, e)
                        continue

                if not clicked_checkout:
                    log.warning("⚠️  ไม่พบปุ่ม Checkout — ดำเนินการต่อ")

                # รอออกจาก /checkout/cart หรือรอ PromptPay ปรากฏ
                try:
                    await page.wait_for_function(
                        "() => !window.location.pathname.includes('/checkout/cart') || "
                        "      [...document.querySelectorAll('*')].some("
                        "          el => el.textContent.trim().toLowerCase() === 'promptpay'"
                        "      )",
                        timeout=15_000,
                    )
                    log.info("✓ ออกจาก cart หรือ PromptPay ปรากฏ: %s", page.url)
                except PWTimeout:
                    log.warning("⚠️  ยังอยู่หน้า cart หลัง 15s: %s", page.url)

        # ── Step B: รอ payment section ──
        log.info("รอ payment section โหลด...")
        try:
            await page.wait_for_selector(
                'input[type="radio"], button:has-text("Place Order"), button:has-text("สั่งซื้อ")',
                state="attached", timeout=20_000
            )
            log.info("✓ payment section พร้อมแล้ว")
        except PWTimeout:
            log.warning("⚠️  payment section ไม่ปรากฏใน 20s — ดำเนินการต่อ")

        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(150)
        except Exception:
            pass

        # ── Step C: เลือก PromptPay ──
        log.info("เลือก PromptPay...")
        pp_clicked = False

        # --- helper: ตรวจว่า PromptPay ถูก select อยู่หรือไม่ ---
        async def _is_pp_selected() -> bool | None:
            """คืน True=selected, False=deselected, None=ไม่รู้"""
            return await page.evaluate("""
                () => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null);
                    while (walker.nextNode()) {
                        if (walker.currentNode.textContent.trim().toLowerCase() !== 'promptpay')
                            continue;
                        let el = walker.currentNode.parentElement;
                        for (let i = 0; i < 10 && el && el !== document.body; i++) {
                            const cls = (el.className || '').toLowerCase();
                            const aria = el.getAttribute('aria-checked') || el.getAttribute('aria-selected') || '';
                            const dataSel = el.getAttribute('data-selected') || el.getAttribute('data-active') || '';
                            if (aria === 'true' || dataSel === 'true' || dataSel === '1' ||
                                cls.includes('selected') || cls.includes('--active') ||
                                cls.includes('is-active') || cls.includes('is-selected') ||
                                cls.includes('checked') || cls.includes('active')) {
                                return true;
                            }
                            el = el.parentElement;
                        }
                        return false;
                    }
                    return null;
                }
            """)

        # กลยุทธ์ 1: Playwright native click (simulate real mouse events) ระดับต่างๆ
        # เริ่มจาก grandparent 2-3 ชั้น ซึ่งมักเป็น payment-option card
        native_selectors = [
            'li:has-text("PromptPay")',
            '[role="listitem"]:has-text("PromptPay")',
            '[role="option"]:has-text("PromptPay")',
            '[role="button"]:has-text("PromptPay")',
            '[role="radio"]:has-text("PromptPay")',
            'label:has-text("PromptPay")',
        ]
        for sel in native_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0 or not await loc.is_visible(timeout=300):
                    continue
                await loc.click(timeout=2000, force=True)
                await page.wait_for_timeout(500)
                state = await _is_pp_selected()
                if state is True or state is None:
                    log.info("✓ PromptPay selected (%s)", sel)
                    pp_clicked = True
                    break
                log.debug("  %s → clicked แต่ยัง deselected", sel)
            except Exception:
                continue

        # กลยุทธ์ 2: เดิน ancestor ด้วย XPath (level 1-4 จาก text node)
        if not pp_clicked:
            pp_text = page.get_by_text("PromptPay", exact=True).first
            if await pp_text.count() > 0:
                for level, xpath in enumerate(
                    ["xpath=..", "xpath=../..", "xpath=../../..", "xpath=../../../.."], 1
                ):
                    try:
                        target = pp_text.locator(xpath)
                        if await target.count() == 0 or not await target.is_visible(timeout=200):
                            continue
                        await target.click(timeout=2000, force=True)
                        await page.wait_for_timeout(500)
                        state = await _is_pp_selected()
                        if state is True or state is None:
                            log.info("✓ PromptPay selected (ancestor level %d)", level)
                            pp_clicked = True
                            break
                        log.debug("  ancestor level %d → deselected ลองถัดไป", level)
                    except Exception:
                        continue

        # กลยุทธ์ 3: JS inject + dispatchEvent ครบชุด (Vue ต้องการ mousedown/up ด้วย)
        if not pp_clicked:
            log.info("ลอง JS dispatchEvent สำหรับ PromptPay...")
            pp3 = await page.evaluate("""
                () => {
                    function fireClick(el) {
                        ['mouseover','mousedown','mouseup','click'].forEach(type => {
                            el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true}));
                        });
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                    }
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null);
                    while (walker.nextNode()) {
                        const txt = walker.currentNode.textContent.trim();
                        if (txt.toLowerCase() !== 'promptpay') continue;
                        let el = walker.currentNode.parentElement;
                        for (let i = 0; i < 6; i++) {
                            if (!el || el === document.body) break;
                            const s = window.getComputedStyle(el);
                            const clickable = el.onclick
                                || el.getAttribute('role') === 'button'
                                || el.getAttribute('tabindex') != null
                                || s.cursor === 'pointer';
                            if (clickable) {
                                fireClick(el);
                                return { ok: true, tag: el.tagName, cls: el.className?.slice(0,60) };
                            }
                            el = el.parentElement;
                        }
                        // fallback: grandparent 3
                        let fb = walker.currentNode.parentElement;
                        for (let j = 0; j < 3 && fb?.parentElement; j++) fb = fb.parentElement;
                        if (fb) { fireClick(fb); return { ok: true, tag: fb.tagName, fallback: true }; }
                    }
                    return { ok: false };
                }
            """)
            if pp3 and pp3.get("ok"):
                await page.wait_for_timeout(600)
                log.info("✓ JS dispatchEvent PromptPay: <%s class='%s'>%s",
                         pp3.get("tag", "?"), pp3.get("cls", ""),
                         " (fallback)" if pp3.get("fallback") else "")
                pp_clicked = True
            else:
                log.warning("⚠️  ไม่พบ PromptPay text node ในหน้าเลย — ดำเนินการต่อ")

        await page.wait_for_timeout(300)

        # ── Step D: หา Place Order button ──
        place_order_btn = page.locator(
            'button:has-text("Place Order"), button:has-text("สั่งซื้อ"), '
            '[role="button"]:has-text("Place Order"), [role="button"]:has-text("สั่งซื้อ")'
        ).first

        if await place_order_btn.count() == 0:
            log.error("✗ ไม่พบปุ่ม Place Order")
            return False

        try:
            await place_order_btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(150)
        except Exception:
            pass

        log.info("✓ PromptPay เลือกแล้ว — พร้อม Place Order")
        return True

    except PWTimeout as e:
        log.error("⏱️  Timeout ตอนเลือก PromptPay: %s", e)
        return False
    except Exception as e:
        log.error("❌ Error ตอนเลือก PromptPay: %s", e, exc_info=True)
        return False


# ==================== MAIN FLOW ====================

async def run(config: dict) -> None:
    product_url: str = config["product_url"]
    sizes_raw = config.get("preferred_sizes") or config.get("size") or []
    preferred_sizes: list[str] = [sizes_raw] if isinstance(sizes_raw, str) else list(sizes_raw)
    headless: bool = config.get("headless", False)
    session_file: str = config.get("session_file", "line_session.json")

    if not preferred_sizes:
        raise ValueError("ต้องระบุ preferred_sizes หรือ size ใน config.json")

    product_id = parse_product_id(product_url)
    shop_handle = parse_shop_handle(product_url)

    log.info("Product URL   : %s", product_url)
    log.info("Product ID    : %d", product_id)
    log.info("Shop handle   : %s", shop_handle)
    log.info("Preferred sizes: %s", preferred_sizes)

    # ── ขั้นที่ 1: ดึง variants ผ่าน HTTP (ไม่เปิด browser) ──
    variants: list[dict] = []
    try:
        variants = await fetch_variants_via_http(product_url, product_id, session_file)
    except Exception as e:
        log.warning("HTTP fetch ล้มเหลว: %s — จะเปิด browser แทน", e)

    if not variants:
        log.error("❌ ไม่สามารถดึง variants ได้จาก HTTP")
        return

    log.info("=== Variants ที่พบ (%d รายการ) ===", len(variants))
    for v in variants:
        log.info("  id=%-12d  name='%s'", v["id"], v["name"])
    log.info("=====================================")

    # ── ขั้นที่ 2: หา variant ที่ตรง size ──
    matched_variant: dict | None = None
    matched_size: str = ""
    for size in preferred_sizes:
        for v in variants:
            if v["name"].strip().lower() == size.strip().lower():
                matched_variant = v
                matched_size = size
                break
        if matched_variant:
            break

    if not matched_variant:
        log.error("❌ ไม่พบ size %s ในรายการ variants", preferred_sizes)
        log.error("   Size ที่มีอยู่: %s", [v["name"] for v in variants])
        return

    log.info("✓ พบ variant: id=%d  name='%s'", matched_variant["id"], matched_size)

    # ── ขั้นที่ 3: สร้าง Checkout URL ──
    checkout_url = build_checkout_url(
        shop_handle=shop_handle,
        product_id=product_id,
        variant_id=matched_variant["id"],
    )
    log.info("Checkout URL: %s", checkout_url)

    # ── ขั้นที่ 4: เปิด browser เฉพาะตอน navigate ไป checkout ──
    session_kwargs: dict = {}
    if Path(session_file).exists():
        session_kwargs["storage_state"] = session_file

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-extensions",
                "--disable-default-apps",
                "--no-first-run",
                "--disable-sync",
                "--disable-translate",
            ],
        )
        context = await browser.new_context(**session_kwargs)
        page = await context.new_page()

        log.info("กำลังเปิด Checkout URL...")
        await page.goto(checkout_url, wait_until="domcontentloaded", timeout=15_000)
        log.info("✅ ถึงหน้า checkout: %s", page.url)

        # ── ขั้นที่ 5: เลือก PromptPay ──
        paid = await select_promptpay(page)
        if not paid:
            log.error("❌ เลือก PromptPay ไม่สำเร็จ — เบราว์เซอร์เปิดค้างไว้")
            try:
                await asyncio.Event().wait()
            except KeyboardInterrupt:
                pass
            await browser.close()
            return

        # ── ขั้นที่ 6: รอ Enter แล้ว Place Order ──
        print()
        input(">>> กด Enter เพื่อ Place Order หรือ Ctrl+C เพื่อยกเลิก: ")

        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await close_popup(page, "before-place-order")
        await page.wait_for_timeout(300)

        place_order_btn = page.locator(
            'button:has-text("Place Order"), button:has-text("สั่งซื้อ"), '
            '[role="button"]:has-text("Place Order"), [role="button"]:has-text("สั่งซื้อ")'
        ).first

        try:
            await place_order_btn.click(timeout=5_000)
            log.info("✅ กด Place Order สำเร็จ!")
        except Exception as e:
            log.error("❌ กด Place Order ล้มเหลว: %s", e)

        await page.wait_for_timeout(3000)
        log.info("URL หลัง Place Order: %s", page.url)

        log.info("เบราว์เซอร์เปิดค้างไว้ — กด Ctrl+C เพื่อออก")
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            log.info("ออกจากโปรแกรม")

        await browser.close()


async def main() -> None:
    config = load_config()
    log.info("โหลด config จาก %s", CONFIG_FILE)
    await run(config)


if __name__ == "__main__":
    asyncio.run(main())
