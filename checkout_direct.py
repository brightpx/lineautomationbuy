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
                "productVariantId": variant_id,  # None จะกลายเป็น null ใน JSON
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
    ดึง {id, name, available} จาก list ของ variant objects โดยตรง
    รองรับ key: productVariants, variants, skus
    available: จำนวนสต็อก (0 = หมด, >0 = มีของ)
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
                results.append({
                    "id": int(item["id"]),
                    "name": str(item["name"]),
                    "available": int(item.get("available", 0))
                })
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
            results.append({
                "id": int(obj["id"]),
                "name": str(obj["name"]),
                "available": int(obj.get("available", 0))
            })
        for v in obj.values():
            results.extend(_recursive_id_name_search(v, depth + 1, max_depth))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_recursive_id_name_search(item, depth + 1, max_depth))
    return results


def parse_nuxt_flat_array(arr: list, product_id: int) -> list[dict]:
    """
    Nuxt serialize ข้อมูลเป็น flat array โดยค่าใน dict คือ INDEX ไปยัง element อื่น
    เช่น: {'id': 47, 'variantOptionValue1': 48, 'available': 49}  →  id=arr[47], name=arr[48], available=arr[49]
    
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
        avail_ref = item.get("available")

        # ต้องเป็น index (int) ที่ valid
        if not isinstance(id_ref, int) or not isinstance(name_ref, int):
            continue
        if not (0 <= id_ref < n) or not (0 <= name_ref < n):
            continue

        actual_id = arr[id_ref]
        actual_name = arr[name_ref]
        
        # dereference available (อาจเป็น direct value หรือ index)
        actual_available = 0
        if isinstance(avail_ref, int):
            if 0 <= avail_ref < n:
                # เป็น index
                val = arr[avail_ref]
                if isinstance(val, (int, float)):
                    actual_available = int(val)
                else:
                    # avail_ref อาจเป็นค่าจริง
                    actual_available = avail_ref
            else:
                # เป็นค่าจริง (ไม่ใช่ index)
                actual_available = avail_ref

        if isinstance(actual_id, (int, float)) and isinstance(actual_name, str) and actual_name:
            results.append({
                "id": int(actual_id),
                "name": actual_name,
                "available": actual_available
            })

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


def extract_product_info(html: str) -> dict:
    """ดึงข้อมูล product name, price, images จาก HTML"""
    info = {"name": "", "price": "", "image_url": ""}
    
    # ดึง product name จาก title tag หรือ og:title
    m = re.search(r'<title>([^<]+)</title>', html)
    if m:
        info["name"] = m.group(1).strip()
    else:
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html)
        if m:
            info["name"] = m.group(1).strip()
    
    # ดึง price จาก __NUXT_DATA__ (ค่าจริง)
    m = re.search(r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>([\s\S]*?)</script>', html)
    if m:
        try:
            data = json.loads(m.group(1).strip())
            # ค้นหา price ใน __NUXT_DATA__ array
            for item in data if isinstance(data, list) else []:
                if isinstance(item, dict) and "price" in item:
                    price_val = item.get("price")
                    if isinstance(price_val, (int, float)):
                        info["price"] = str(int(price_val))
                        break
                    elif isinstance(price_val, int) and 0 <= price_val < len(data):
                        # dereference index
                        actual_price = data[price_val]
                        if isinstance(actual_price, (int, float)):
                            info["price"] = str(int(actual_price))
                            break
        except Exception:
            pass
    
    # ดึง image URL จาก og:image
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
    if m:
        info["image_url"] = m.group(1).strip()
    
    return info


async def fetch_product_info(product_url: str, session_file: str) -> dict:
    """ดึงข้อมูล product ผ่าน HTTP"""
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
        resp = await client.get(product_url)
        resp.raise_for_status()
        html = resp.text

    return extract_product_info(html)


# ==================== CHECKOUT HELPERS ====================

async def close_popup(page: Page, context: str = "") -> bool:
    """ปิด popup/modal ที่อาจขวาง"""
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_load_state("domcontentloaded", timeout=500)
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
                await page.wait_for_load_state("domcontentloaded", timeout=500)
                log.info("  ✓ ปิด popup สำเร็จ")
                return True
        except Exception:
            pass
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
                                clicked_checkout = True
                                break
                    except Exception as e:
                        continue

                if not clicked_checkout:
                    pass

                # รอออกจาก /checkout/cart หรือรอ PromptPay ปรากฏ
                try:
                    await page.wait_for_function(
                        "() => !window.location.pathname.includes('/checkout/cart') || "
                        "      [...document.querySelectorAll('*')].some("
                        "          el => el.textContent.trim().toLowerCase() === 'promptpay'"
                        "      )",
                        timeout=15_000,
                    )
                except PWTimeout:
                    pass

        # ── Step B: รอ payment section ──
        try:
            await page.wait_for_selector(
                'input[type="radio"], button:has-text("Place Order"), button:has-text("สั่งซื้อ")',
                state="attached", timeout=20_000
            )
        except PWTimeout:
            pass

        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

        # ── Step C: ปิด popup/overlay ก่อน แล้วเลือก PromptPay ──
        # กด Escape + ปิดปุ่มปิดทุกแบบ
        for _ in range(3):
            await close_popup(page, "before-promptpay")

        # ปิด overlay โดยตรงผ่าน JS (บาง popup ไม่มีปุ่มปิด)
        dismissed = await page.evaluate("""
            () => {
                const dismissed = [];
                // ซ่อน modal backdrop / overlay
                document.querySelectorAll(
                    '[class*="modal"],[class*="overlay"],[class*="backdrop"],[class*="dialog"]'
                ).forEach(el => {
                    const s = window.getComputedStyle(el);
                    if (s.position === 'fixed' || s.position === 'absolute') {
                        el.style.display = 'none';
                        dismissed.push(el.className.slice(0,40));
                    }
                });
                // คลิกปุ่ม Got it / T&C / รับทราบ
                const closeTexts = ['got it','รับทราบ','ตกลง','ok','close','ปิด','ยืนยัน','confirm'];
                document.querySelectorAll('button,[role="button"]').forEach(btn => {
                    const t = btn.textContent.trim().toLowerCase();
                    if (closeTexts.some(c => t === c || t.includes(c))) {
                        btn.click();
                        dismissed.push('btn:' + btn.textContent.trim().slice(0,20));
                    }
                });
                return dismissed;
            }
        """)
        if dismissed:
            log.info("  ปิด popup/overlay: %s", dismissed)

        log.info("เลือก PromptPay...")
        pp_clicked = False

        # Vue reactive update: dispatch full event chain + set aria-checked
        pp_result = await page.evaluate("""
            () => {
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null);
                while (walker.nextNode()) {
                    if (walker.currentNode.textContent.trim().toLowerCase() !== 'promptpay')
                        continue;
                    let el = walker.currentNode.parentElement;
                    for (let i = 0; i < 6 && el && el !== document.body; i++) {
                        const cls = (el.className || '').toLowerCase();
                        if (cls.includes('cursor-pointer') || cls.includes('select') || cls.includes('option')) {
                            // Simulate full Vue click chain
                            el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
                            el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
                            el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            // ตั้ง aria-checked
                            el.setAttribute('aria-checked', 'true');
                            let parent = el.parentElement;
                            for (let j = 0; j < 3 && parent; j++) {
                                parent.setAttribute('aria-checked', 'true');
                                parent = parent.parentElement;
                            }
                            return {ok: true, tag: el.tagName, cls: el.className.slice(0,60)};
                        }
                        el = el.parentElement;
                    }
                }
                return {ok: false};
            }
        """)
        
        if pp_result and pp_result.get("ok"):
            # ตรวจสอบ aria-checked
            after_state = await page.evaluate("""
                () => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null);
                    while (walker.nextNode()) {
                        if (walker.currentNode.textContent.trim().toLowerCase() !== 'promptpay')
                            continue;
                        let el = walker.currentNode.parentElement;
                        for (let i = 0; i < 8 && el && el !== document.body; i++) {
                            const ac = el.getAttribute('aria-checked');
                            if (ac !== null) return ac;
                            el = el.parentElement;
                        }
                        return 'not-found';
                    }
                    return 'no-text';
                }
            """)
            log.info("aria-checked = '%s'", after_state)
            pp_clicked = (after_state == 'true')
        else:
            log.warning("⚠️  ไม่พบ PromptPay element")

        # ── Step D: ตรวจผลและหา Place Order button ──
        if not pp_clicked:
            log.warning("⚠️  เลือก PromptPay ไม่สำเร็จ")
            return False
            
        place_order_btn = page.locator(
            'button:has-text("Place Order"), button:has-text("สั่งซื้อ"), '
            '[role="button"]:has-text("Place Order"), [role="button"]:has-text("สั่งซื้อ")'
        ).first

        if await place_order_btn.count() == 0:
            log.error("✗ ไม่พบปุ่ม Place Order")
            return False

        try:
            await place_order_btn.scroll_into_view_if_needed()
        except Exception:
            pass

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

    log.info("🛍️  Product: %s (ID: %d)", shop_handle, product_id)
    log.info("📏 Sizes: %s", preferred_sizes)

    # ── ขั้นที่ 1: ดึง product info และ variants ผ่าน HTTP (ไม่เปิด browser) ──
    variants: list[dict] = []
    product_info: dict = {}
    try:
        variants = await fetch_variants_via_http(product_url, product_id, session_file)
        product_info = await fetch_product_info(product_url, session_file)
    except Exception as e:
        log.warning("HTTP fetch ล้มเหลว: %s — จะเปิด browser แทน", e)

    # ถ้าไม่มี variants หรือมีแค่ variant เดียว → ข้ามการเลือก
    if not variants:
        log.warning("⚠️ ไม่พบ variants — ข้ามขั้นตอนเลือก variant")
        # ส่ง variant_id = None (จะกลายเป็น null ใน JSON)
        matched_variant = {"id": None, "name": "default", "available": 1}
        matched_size = "default"
    elif len(variants) == 1:
        # มี variant เดียว → เลือกอัตโนมัติ
        matched_variant = variants[0]
        matched_size = matched_variant["name"]
        log.info("📦 Variant เดียว: %s (ID: %d) — เลือกอัตโนมัติ", matched_size, matched_variant["id"])
    else:
        # มีหลาย variants → ต้องเลือก
        variant_names = [v['name'] for v in variants]
        log.info("📦 Variants (%d): %s", len(variants), ', '.join(variant_names))

        # ── ขั้นที่ 2: หา variant ที่ตรง size ──
        check_interval = config.get("check_interval_seconds", 30)
        
        while True:
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

            # ตรวจสอบสต็อก
            stock = matched_variant.get("available", 0)
            
            if stock > 0:
                log.info("✅ เลือก: %s (ID: %d) — มีสต็อก: %d", matched_size, matched_variant["id"], stock)
                break
            else:
                log.warning("⏳ Size '%s' หมดสต็อก — รอตรวจสอบอีกครั้งใน %d วินาที...", matched_size, check_interval)
                await asyncio.sleep(check_interval)
                
                # ดึง variants ใหม่
                try:
                    variants = await fetch_variants_via_http(product_url, product_id, session_file)
                    if not variants:
                        log.error("❌ ไม่สามารถดึง variants ได้ — หยุดการตรวจสอบ")
                        return
                except Exception as e:
                    log.warning("HTTP fetch ล้มเหลว: %s — ข้ามรอบนี้", e)
                    continue

    # ── ขั้นที่ 3: สร้าง Checkout URL ──
    checkout_url = build_checkout_url(
        shop_handle=shop_handle,
        product_id=product_id,
        variant_id=matched_variant["id"],
    )

    # ── ขั้นที่ 4: เปิด browser (headless) และทำ checkout โดยอัตโนมัติ ──
    session_kwargs: dict = {}
    if Path(session_file).exists():
        session_kwargs["storage_state"] = session_file

    log.info("🌐 เปิด browser...")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-extensions",
                "--disable-default-apps",
                "--no-first-run",
                "--disable-sync",
                "--disable-translate",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(**session_kwargs)
        
        # Block unnecessary resources for faster page load
        await context.route("**/*", lambda route: (
            route.abort() if route.request.resource_type in ["image", "font", "media", "stylesheet"]
            else route.continue_()
        ))
        
        page = await context.new_page()
        page.set_default_timeout(5000)

        await page.goto(checkout_url, wait_until="domcontentloaded", timeout=15_000)
        log.info("✅ ถึงหน้า checkout")
        log.info("🔗 URL: %s", page.url)

        # ── ขั้นที่ 5: เลือก PromptPay ──
        paid = await select_promptpay(page)
        if not paid:
            log.error("❌ เลือก PromptPay ไม่สำเร็จ")
            await browser.close()
            return

        log.info("💳 เลือก PromptPay แล้ว")

        # ปิด popup ก่อน Place Order
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await close_popup(page, "before-place-order")

        # ── ขั้นที่ 6: ดึงราคาจริงจากหน้า checkout ──
        actual_price = ""
        try:
            import re
            
            # DEBUG: dump HTML เพื่อตรวจสอบ
            html_content = await page.content()
            with open("debug_checkout_price.html", "w", encoding="utf-8") as f:
                f.write(html_content)
            log.info("📝 Dump HTML to debug_checkout_price.html")
            
            # หาราคาทั้งหมดที่มีในหน้า
            all_prices = re.findall(r'฿\s*([0-9,]+(?:\.[0-9]{2})?)', html_content)
            price_values = []
            for p in all_prices:
                val_str = p.replace(',', '')
                val = int(float(val_str)) if '.' in val_str else int(val_str)
                price_values.append(val)
            
            log.info("🔍 ราคาทั้งหมดในหน้า (raw): %s", sorted(set(price_values), reverse=True))
            
            # กรองเฉพาะราคาที่มีนัยสำคัญ (≥100)
            significant_prices = sorted([p for p in set(price_values) if p >= 100], reverse=True)
            log.info("💰 ราคาที่ ≥100: %s", significant_prices)
            
            if significant_prices:
                actual_price = str(significant_prices[0])
                log.info("✅ เลือกราคา: ฿%s", actual_price)
                
        except Exception as e:
            log.warning("ไม่สามารถดึงราคาจากหน้า checkout: %s", e)

        # ── ขั้นที่ 7: แสดงข้อมูลและรอยืนยันก่อน Place Order ──
        print()
        print("=" * 60)
        print("📦 พร้อม Place Order")
        print("=" * 60)
        if product_info.get("name"):
            print(f"สินค้า: {product_info['name']}")
        if actual_price:
            print(f"ราคา: ฿{actual_price}")
        elif product_info.get("price"):
            print(f"ราคา: ฿{product_info['price']}")
        print(f"ตัวเลือก: {matched_size}")
        print(f"จำนวน: 1")
        print(f"ร้านค้า: {shop_handle}")
        print(f"วิธีชำระเงิน: PromptPay")
        print("=" * 60)
        print()

        response = input(">>> กด Enter เพื่อ Place Order หรือ Ctrl+C เพื่อยกเลิก: ")

        # ── ขั้นที่ 8: Place Order ──
        place_order_btn = page.locator(
            'button:has-text("Place Order"), button:has-text("สั่งซื้อ"), '
            '[role="button"]:has-text("Place Order"), [role="button"]:has-text("สั่งซื้อ")'
        ).first

        try:
            url_before = page.url
            
            await place_order_btn.click(timeout=5_000)
            log.info("🛒 กด Place Order...")
            
            # รอให้มีการ navigate หรือ response
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            
            url_after = page.url
            
            # ตรวจสอบว่า URL เปลี่ยนหรือไม่
            if url_before == url_after:
                
                # หา error messages (กรองคำที่ไม่ใช่ error)
                error_selectors = [
                    'text=/error/i',
                    'text=/ผิดพลาด/i',
                    '[role="alert"]',
                    '[class*="error"]',
                    '[class*="Error"]',
                ]
                
                # คำที่ไม่ใช่ error - ต้องกรองออก
                ignore_texts = ['ดูทั้งหมด', 'รวมทั้งหมด', 'total', 'view all', 'see all']
                
                found_errors = []
                for sel in error_selectors:
                    try:
                        errors = await page.locator(sel).all_text_contents()
                        if errors:
                            # กรองเฉพาะข้อความที่ไม่ใช่คำทั่วไป
                            filtered = [
                                e.strip() for e in errors 
                                if e.strip() and e.strip().lower() not in ignore_texts
                            ]
                            found_errors.extend(filtered)
                    except Exception:
                        pass
                
                if found_errors:
                    log.error("❌ พบ error: %s", found_errors)
                    print()
                    print("=" * 60)
                    print("❌ ไม่สามารถ Place Order ได้")
                    print("=" * 60)
                    for err in found_errors:
                        print(f"  • {err}")
                    print("=" * 60)
                    print()
                else:
                    log.warning("ไม่พบ error message - แต่ URL ไม่เปลี่ยน")
                
                # ถ่าย screenshot เพื่อ debug
                try:
                    await page.screenshot(path="debug_place_order_failed.png")
                    log.info("บันทึก screenshot: debug_place_order_failed.png")
                except Exception:
                    pass
                
                # ปิด browser และจบโปรแกรม
                log.info("ปิด browser")
                await browser.close()
                return
            else:
                log.info("✅ URL เปลี่ยนแล้ว - น่าจะ submit สำเร็จ")
                log.info("🔗 URL หลัง Place Order: %s", url_after)
                
                # ตรวจสอบว่าไป payment page หรือ success page หรือไม่
                if "payment" in url_after or "success" in url_after or "order" in url_after:
                    log.info("✅ ไปหน้า payment/success/order แล้ว")
                else:
                    log.warning("⚠️ URL ไม่ใช่หน้า payment/success - ตรวจสอบ: %s", url_after)
                
        except Exception as e:
            log.error("❌ กด Place Order ล้มเหลว: %s", e)
            try:
                await page.screenshot(path="debug_place_order_error.png")
                log.info("บันทึก screenshot: debug_place_order_error.png")
            except Exception:
                pass
                
                # รอ QR code หรือ payment page
                log.info("รอ QR code หรือ payment page...")
                try:
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
                
                try:
                    await page.screenshot(path="debug_payment_page.png")
                    log.info("บันทึก screenshot: debug_payment_page.png")
                except Exception:
                    pass

        log.info("✅ เสร็จสิ้น - ปิด browser")
        await browser.close()


async def main() -> None:
    config = load_config()
    log.info("โหลด config จาก %s", CONFIG_FILE)
    await run(config)


if __name__ == "__main__":
    asyncio.run(main())
