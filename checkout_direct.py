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

from playwright.async_api import async_playwright, Page, Response

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


# ==================== MAIN FLOW ====================

async def run(config: dict) -> None:
    product_url: str = config["product_url"]
    # รองรับทั้ง "size" (string เดียว) และ "preferred_sizes" (list)
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

    session_kwargs = {}
    if Path(session_file).exists():
        session_kwargs["storage_state"] = session_file
        log.info("โหลด session จาก %s", session_file)
    else:
        log.warning("ไม่พบ session file '%s' — อาจต้องล็อกอินก่อน", session_file)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--no-first-run",
                "--disable-sync",
                "--disable-translate",
            ],
        )
        context = await browser.new_context(**session_kwargs)

        # ── Intercept API responses ก่อน goto() เพื่อไม่พลาด call แรก ──
        api_variants: list[dict] = []
        api_done = asyncio.Event()          # set เมื่อ intercept เจอ variants ที่ดี

        async def on_response(response: Response) -> None:
            """เก็บ variants จาก JSON API response — log ทุก JSON call เพื่อ debug"""
            url = response.url
            if response.status not in (200, 201):
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            # skip ไฟล์ static ที่ชัดเจน
            if any(x in url for x in (".js", ".css", ".png", ".ico", "fonts")):
                return
            try:
                body = await response.json()
                found = extract_variants_from_json(body, product_id)
                if found:
                    log.info("  🔌 intercepted %d variants จาก %s", len(found), url[:120])
                    for v in found:
                        if v["id"] not in {x["id"] for x in api_variants}:
                            api_variants.append(v)
                    api_done.set()
                else:
                    # log ทุก JSON call ที่ไม่ได้ variants (เพื่อดู API structure)
                    log.debug("  📡 JSON response (no variants): %s", url[:120])
            except Exception:
                pass

        page = await context.new_page()
        page.on("response", on_response)     # register ก่อน goto

        log.info("เปิดหน้า product (intercepting network)...")
        await page.goto(product_url, wait_until="domcontentloaded", timeout=15_000)

        # รอให้ Vue hydrate + network ระงับ (หรือแค่ 4 วิ)
        log.info("รอ Vue render / network...")
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            await page.wait_for_timeout(4_000)

        # ── เลือก source ที่ดีที่สุด ──
        variants: list[dict] = []

        # 1) API intercept (ข้อมูลตรงที่สุด)
        if api_variants:
            log.info("ใช้ variants จาก intercepted API (%d รายการ)", len(api_variants))
            variants = api_variants
        else:
            # 2) page-embedded data (script tags / window.__NUXT__)
            variants = await get_variants_from_page(page, product_id)

        if not variants:
            log.error("❌ ไม่สามารถดึง variants ได้ — ลองเปิดหน้าใหม่แบบรอ networkidle นานขึ้น")
            await browser.close()
            return

        log.info("=== Variants ที่พบทั้งหมด (%d รายการ) ===", len(variants))
        for v in variants:
            log.info("  id=%-12d  name='%s'", v["id"], v["name"])
        log.info("==========================================")

        # หา variant ที่ตรงกับ size
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
            available = [v["name"] for v in variants]
            log.error("❌ ไม่พบ size '%s' ในรายการ variants", preferred_sizes)
            log.error("   Size ที่มีอยู่: %s", available)
            await browser.close()
            return

        log.info("✓ พบ variant: id=%d  name='%s'", matched_variant["id"], matched_size)

        # สร้าง Checkout URL
        checkout_url = build_checkout_url(
            shop_handle=shop_handle,
            product_id=product_id,
            variant_id=matched_variant["id"],
        )
        log.info("Checkout URL: %s", checkout_url)

        # Navigate ไป Checkout URL ทันที
        log.info("กำลังเปิด Checkout URL...")
        await page.goto(checkout_url, wait_until="domcontentloaded", timeout=15_000)
        log.info("✅ ถึงหน้า checkout: %s", page.url)

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
