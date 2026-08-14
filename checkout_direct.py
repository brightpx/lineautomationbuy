"""
LINE Shopping — Direct Checkout Script
---------------------------------------
1. อ่าน config.json  (product_url + preferred_1/preferred_2)
2. เปิดหน้า product พร้อม intercept ทุก network response / script tag
3. ดึง productVariants จาก API JSON response ที่ URL มี productId ก่อน
4. ถ้าไม่ได้จาก API → scan script tags → window.__NUXT__ (กรองตาม productId)
5. หา variant ที่ตรงกับ size แล้วสร้าง Checkout URL และ navigate ทันที
"""

import asyncio
import json
import logging
import re
import sys
import time
import urllib.parse
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from playwright.async_api import async_playwright, Page, Response, TimeoutError as PWTimeout, Browser, BrowserContext

# Windows keyboard detection
if sys.platform == 'win32':
    import msvcrt

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


# ==================== CHECKOUT URL BUILDER ====================

CHECKOUT_ENCODING_MODES = ("auto", "full", "quote_only", "none")


def build_checkout_payload(
    product_id: int,
    variant_id: Optional[int] = None,
    quantity: int = 1,
) -> dict:
    """
    สร้าง payload dict สำหรับ query string `data` ของ LINE Shopping checkout

    variant_id = None  → productVariantId: null  (สินค้าไม่มี variant)
    """
    if quantity is None or int(quantity) < 1:
        quantity = 1
    return {
        "items": [
            {
                "productId": int(product_id),
                "productVariantId": int(variant_id) if variant_id is not None else None,
                "quantity": int(quantity),
            }
        ]
    }


def _resolve_encoding_mode(mode: Optional[str], variant_id: Optional[int]) -> str:
    """
    แปลง encoding_mode → mode จริงที่จะใช้

    auto:
      - ไม่มี variant (productVariantId = null) → full   (ตรงกับ pattern ที่ LINE ส่งจริง)
      - มี variant                              → quote_only
    """
    normalized = (mode or "auto").strip().lower()
    if normalized in ("full", "quote_only", "none"):
        return normalized
    if normalized not in CHECKOUT_ENCODING_MODES:
        log.warning("checkout_encoding='%s' ไม่รู้จัก — ใช้ 'auto'", mode)
    return "full" if variant_id is None else "quote_only"


def encode_checkout_data(
    payload: dict,
    encoding_mode: str = "auto",
    variant_id: Optional[int] = None,
) -> str:
    """
    encode payload → string สำหรับใส่หลัง ?data=

    full       : %7B%22items%22%3A%5B...  (encode ทุกตัวอักษรพิเศษ)
    quote_only : {%22items%22:[{...}]}    (encode เฉพาะ " — { } [ ] : , ปล่อยไว้)
    none       : {"items":[{...}]}        (raw JSON)
    """
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    mode = _resolve_encoding_mode(encoding_mode, variant_id)

    if mode == "none":
        return raw
    if mode == "quote_only":
        # encode เฉพาะอักขระที่ browser/LINE ต้องการจริง ๆ — คง { } [ ] : , ไว้
        safe_chars = "{}[]:,-_.~*'()!$&+;=/?@"
        return urllib.parse.quote(raw, safe=safe_chars)
    return urllib.parse.quote(raw, safe="")


def build_checkout_url(
    shop_handle: str,
    product_id: int,
    variant_id: Optional[int] = None,
    quantity: int = 1,
    encoding_mode: str = "auto",
) -> str:
    """
    คืน URL string เท่านั้น (ไม่ใช่ HTML <a>)
    เช่น https://shop.line.me/@shop/checkout/cart?data=...
    """
    handle = shop_handle if shop_handle.startswith("@") else f"@{shop_handle}"
    payload = build_checkout_payload(product_id, variant_id, quantity)
    encoded = encode_checkout_data(payload, encoding_mode, variant_id)
    return f"https://shop.line.me/{handle}/checkout/cart?data={encoded}"


def build_checkout_url_candidates(
    shop_handle: str,
    product_id: int,
    variant_id: Optional[int] = None,
    quantity: int = 1,
    encoding_mode: str = "auto",
) -> list[str]:
    """
    คืน list ของ URL ทุก encoding mode โดยเรียง mode ที่เลือกไว้ก่อน
    ใช้เป็น fallback ถ้า mode แรก LINE ไม่รับ
    """
    primary = _resolve_encoding_mode(encoding_mode, variant_id)
    order = [primary] + [m for m in ("full", "quote_only", "none") if m != primary]
    urls: list[str] = []
    for mode in order:
        url = build_checkout_url(shop_handle, product_id, variant_id, quantity, mode)
        if url not in urls:
            urls.append(url)
    return urls


# ==================== VARIANT EXTRACTION ====================

def _dedup(variants: list[dict]) -> list[dict]:
    seen: set = set()
    out: list[dict] = []
    for v in variants:
        key = v["id"]
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def normalize_variant(item: dict, resolve: Callable[[Any], Any] = lambda x: x) -> Optional[dict]:
    """
    แปลง dict ของ variant (จาก API หรือ __NUXT_DATA__ flat array) ให้เป็นรูปแบบมาตรฐาน:

        {
            "id": int | None,
            "name": str,
            "option1": str | None,   # ค่าตัวเลือกแรก เช่น สี
            "option2": str | None,   # ค่าตัวเลือกสอง เช่น ขนาด
            "available": int         # -1 = ไม่รู้ (ถือว่ามี), 0 = หมด, >0 = มีสต็อก
        }

    resolve: callable สำหรับ dereference ค่า index ใน Nuxt flat array
             (ส่ง lambda x: arr[x] if isinstance(x,int) and x<len(arr) else x)
             ถ้าไม่ได้ใช้ Nuxt flat mode → ส่ง lambda x: x (default)
    """
    if not isinstance(item, dict):
        return None

    # ─── id ───────────────────────────────────────────────
    raw_id = item.get("id") or item.get("productVariantId") or item.get("variantId")
    resolved_id = resolve(raw_id) if raw_id is not None else None
    if resolved_id is None:
        return None
    if not isinstance(resolved_id, (int, float)):
        return None
    variant_id = int(resolved_id)
    if variant_id <= 0:
        return None

    # ─── option1 / option2 ────────────────────────────────
    # ลำดับการหาค่า: field โดยตรงก่อน แล้ว resolve index
    _option_keys1 = (
        "variantOptionValue1", "optionValue1", "option1",
        "color", "colour", "size", "selectedOption1",
    )
    _option_keys2 = (
        "variantOptionValue2", "optionValue2", "option2",
        "size", "selectedOption2",
    )

    def _pick_option(keys: tuple) -> Optional[str]:
        for k in keys:
            raw = item.get(k)
            if raw is None:
                continue
            val = resolve(raw)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, (int, float)) and val != resolved_id:
                # เป็นค่าจำนวน ไม่ใช่ string → ข้าม
                pass
        return None

    option1 = _pick_option(_option_keys1)
    option2 = _pick_option(_option_keys2)

    # ─── name ─────────────────────────────────────────────
    raw_name = item.get("name") or item.get("variantName") or item.get("title")
    resolved_name = resolve(raw_name) if raw_name is not None else None
    if isinstance(resolved_name, str) and resolved_name.strip():
        name = resolved_name.strip()
    elif option1 and option2:
        name = f"{option1} / {option2}"
    elif option1:
        name = option1
    elif option2:
        name = option2
    else:
        # ไม่มี name และไม่มี option → ไม่สามารถ normalize ได้
        return None

    # ถ้าได้ name แต่ยังไม่มี option → แยก name ออกเป็น option1/option2
    if name and option1 is None and option2 is None:
        # แยกด้วย " / ", " - ", " _ " เฉพาะเมื่อมีจำนวนพอดี 2 ส่วน
        split_val: Optional[list[str]] = None
        for sep in (" / ", " - ", "/", " _ "):
            parts = [p.strip() for p in name.split(sep) if p.strip()]
            if len(parts) == 2:
                split_val = parts
                break
        if split_val:
            option1, option2 = split_val

    # ─── available ────────────────────────────────────────
    raw_avail = item.get("available") or item.get("stock") or item.get("qty") or item.get("quantity")
    if raw_avail is None:
        available = -1   # ไม่รู้ → ถือว่ามีของ
    else:
        resolved_avail = resolve(raw_avail)
        if isinstance(resolved_avail, (int, float)):
            available = int(resolved_avail)
        else:
            available = -1

    return {
        "id": variant_id,
        "name": name,
        "option1": option1,
        "option2": option2,
        "available": available,
    }


def _extract_variant_list(
    obj: Any,
    resolve: Callable[[Any], Any] = lambda x: x,
) -> list[dict]:
    """
    ดึง normalized variants จาก list ของ variant objects
    รองรับสินค้า 0 / 1 / 2 variant options
    """
    if not isinstance(obj, list):
        return []
    results: list[dict] = []
    for item in obj:
        v = normalize_variant(item, resolve)
        if v is not None:
            results.append(v)
    return results


def find_product_variants(obj, product_id: int, depth: int = 0, max_depth: int = 20) -> list[dict]:
    """
    Context-aware search:
    1. หา dict ที่มี 'id' == product_id ก่อน
    2. ดึง productVariants / variants / skus จาก dict นั้น
    3. ถ้าไม่พบ context ที่ตรง → fallback ค้นหา productVariants key ทั่วไป
    รองรับ 0 / 1 / 2 variant options ผ่าน normalize_variant()
    """
    if depth > max_depth:
        return []

    _VARIANT_KEYS = ("productVariants", "variants", "skus")

    if isinstance(obj, dict):
        obj_id = obj.get("id") or obj.get("productId")
        if obj_id is not None:
            try:
                obj_id_int = int(obj_id)
            except (ValueError, TypeError):
                obj_id_int = -1

            if obj_id_int == product_id:
                # ตรงกับ product ที่ต้องการ — ดึง variant list จาก key ที่รู้จัก
                for key in _VARIANT_KEYS:
                    if key in obj and isinstance(obj[key], list):
                        found = _extract_variant_list(obj[key])
                        if found:
                            return found
                # variant ฝังลึกกว่า — ค้นต่อใน dict นี้
                for v in obj.values():
                    found = find_product_variants(v, product_id, depth + 1, max_depth)
                    if found:
                        return found

        # รวบรวม fallback จาก key ที่รู้จัก (คืนผล fallback เฉพาะถ้าไม่พบ context ที่ดีกว่า)
        fallback: list[dict] = []
        for key in _VARIANT_KEYS:
            if key in obj and isinstance(obj[key], list):
                cands = _extract_variant_list(obj[key])
                if cands:
                    fallback.extend(cands)

        for v in obj.values():
            found = find_product_variants(v, product_id, depth + 1, max_depth)
            if found:
                return found

        if fallback:
            return fallback

    elif isinstance(obj, list):
        for item in obj:
            found = find_product_variants(item, product_id, depth + 1, max_depth)
            if found:
                return found

    return []


def find_variants_anywhere(obj, depth: int = 0, max_depth: int = 20) -> list[dict]:
    """
    Fallback ระดับ 1: ค้นหา productVariants/variants/skus key ทุกที่
    กรอง id > 10000 เพื่อลด false positive
    รองรับ 0 / 1 / 2 variant options ผ่าน normalize_variant()
    """
    if depth > max_depth:
        return []

    _VARIANT_KEYS = ("productVariants", "variants", "skus")
    results: list[dict] = []

    if isinstance(obj, dict):
        for key in _VARIANT_KEYS:
            if key in obj and isinstance(obj[key], list):
                for item in obj[key]:
                    v = normalize_variant(item)
                    if v is not None and v["id"] > 10000:
                        results.append(v)

        for v in obj.values():
            results.extend(find_variants_anywhere(v, depth + 1, max_depth))

    elif isinstance(obj, list):
        for item in obj:
            results.extend(find_variants_anywhere(item, depth + 1, max_depth))

    return results


def _recursive_id_name_search(obj, depth: int = 0, max_depth: int = 20) -> list[dict]:
    """
    Fallback ระดับ 2 (กว้างที่สุด): หา dict ที่มี 'id' (int) + name/option ทุกที่
    ใช้ normalize_variant() เพื่อรองรับ option1/option2
    """
    if depth > max_depth:
        return []
    results: list[dict] = []
    if isinstance(obj, dict):
        v = normalize_variant(obj)
        if v is not None:
            results.append(v)
        for val in obj.values():
            results.extend(_recursive_id_name_search(val, depth + 1, max_depth))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_recursive_id_name_search(item, depth + 1, max_depth))
    return results


def parse_nuxt_flat_array(arr: list, product_id: int) -> list[dict]:
    """
    Nuxt serialize ข้อมูลเป็น flat array โดยค่าใน dict คือ INDEX ไปยัง element อื่น
    เช่น: {'id': 47, 'variantOptionValue1': 48, 'available': 49}  →  id=arr[47], name=arr[48], available=arr[49]

    ค้นหา dict ที่มี key 'id' และ variantOptionValue1/variantOptionValue2
    แล้ว dereference ค่าออกมาผ่าน normalize_variant()
    รองรับสินค้า 1 variant (มีเฉพาะ variantOptionValue1)
    และสินค้า 2 variants (มีทั้ง variantOptionValue1 + variantOptionValue2)
    """
    if not isinstance(arr, list):
        return []
    n = len(arr)

    def resolve(val: Any) -> Any:
        """dereference index → ค่าจริง ถ้าไม่ใช่ index ที่ valid → คืนค่าเดิม"""
        if isinstance(val, int) and 0 <= val < n:
            return arr[val]
        return val

    results: list[dict] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        # ต้องมี 'id' และ variantOptionValue1 หรือ variantOptionValue2 อย่างน้อยหนึ่งอย่าง
        has_id = "id" in item
        has_opt = (
            "variantOptionValue1" in item
            or "variantOptionValue2" in item
            or "optionValue1" in item
        )
        if not (has_id and has_opt):
            continue

        v = normalize_variant(item, resolve)
        if v is not None:
            results.append(v)

    return _dedup(results)


def extract_variants_from_json(data, product_id: int) -> list[dict]:
    """
    Main entry point — 4 ชั้น:
    0. Nuxt flat array dereference (LINE Shopping specific — __NUXT_DATA__)
    1. Context-aware: หา product object ที่ id == product_id แล้วดึง productVariants
    2. Key-based: ค้นหา productVariants/variants/skus key ทุกที่ (id > 10000)
    3. Broad: ค้นหา {id, name/option} ทุกที่ แล้ว filter id > 10000
    รองรับ 0 / 1 / 2 variant options ผ่าน normalize_variant()
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
    found = [v for v in found if v["id"] is not None and v["id"] > 10000]
    return _dedup(found)


# ==================== MATCHING ENGINE ====================


def find_matching_variant(
    variants: list[dict],
    preferred_1: Optional[list[str]] = None,
    preferred_2: Optional[list[str]] = None,
) -> Optional[dict]:
    """
    หา variant ที่ตรงกับ preferred_1 และ/หรือ preferred_2

    ลำดับความสำคัญ:
      1. option1 + option2 match ทั้งคู่   (pref1+pref2 หรือ pref2+pref1)
      2. name match ทั้ง pref1 และ pref2    (fallback สำหรับ name compound)
      3. option1 หรือ option2 match pref1 only
      4. option1 หรือ option2 match pref2 only
      5. name match สำหรับ pref1 หรือ pref2 อย่างใดอย่างหนึ่ง

    เปรียบเทียบแบบ case-insensitive, trim whitespace
    Config เก่าที่มี preferred_sizes/preferred_colors ยังใช้งานได้ (backward-compatible)
    """
    if not variants:
        return None

    pref1 = [c.strip().lower() for c in (preferred_1 or []) if c]
    pref2 = [s.strip().lower() for s in (preferred_2 or []) if s]

    def _match_val(val: Optional[str], targets: list[str]) -> bool:
        if not val or not targets:
            return False
        v = val.strip().lower()
        return any(t == v for t in targets)

    def _match_name(name: str, targets: list[str]) -> bool:
        if not name or not targets:
            return False
        n = name.strip().lower()
        return any(t in n or n == t for t in targets)

    # ลำดับที่ 1: option1 + option2 ทั้งคู่ (ลอง pref1=opt1/pref2=opt2 และ pref1=opt2/pref2=opt1)
    if pref1 and pref2:
        for v in variants:
            c1 = _match_val(v.get("option1"), pref1)
            s2 = _match_val(v.get("option2"), pref2)
            if c1 and s2:
                return v
        for v in variants:
            s1 = _match_val(v.get("option1"), pref2)
            c2 = _match_val(v.get("option2"), pref1)
            if s1 and c2:
                return v

    # ลำดับที่ 2: name ประกอบด้วยทั้ง pref1 และ pref2
    if pref1 and pref2:
        for v in variants:
            name = v.get("name", "")
            if _match_name(name, pref1) and _match_name(name, pref2):
                return v

    # ลำดับที่ 3: pref1 only (option1 หรือ option2 หรือ name)
    if pref1:
        for v in variants:
            if _match_val(v.get("option1"), pref1) or _match_val(v.get("option2"), pref1):
                return v
        for v in variants:
            if _match_name(v.get("name", ""), pref1):
                return v

    # ลำดับที่ 4: pref2 only (option1 หรือ option2 หรือ name)
    if pref2:
        for v in variants:
            if _match_val(v.get("option1"), pref2) or _match_val(v.get("option2"), pref2):
                return v
        for v in variants:
            if _match_name(v.get("name", ""), pref2):
                return v

    return None


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


# ==================== SHOP MONITOR MODE ====================

def parse_sale_time(time_str: str) -> dt_time:
    """แปลง sale_start_time string → datetime.time object
    รองรับ HH:MM:SS, HH:MM, H:MM
    """
    parts = time_str.strip().split(":")
    if len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    elif len(parts) == 2:
        h, m, s = int(parts[0]), int(parts[1]), 0
    else:
        raise ValueError(f"sale_start_time format ไม่ถูกต้อง: {time_str}")
    return dt_time(h, m, s)


async def fetch_shop_products(
    shop_url: str, 
    session_file: str,
    browser: Optional[Browser] = None,
    context: Optional[BrowserContext] = None
) -> list[dict]:
    """
    ดึงรายการสินค้าทั้งหมดจากหน้าร้าน (ใช้ Playwright เพื่อรอ JavaScript โหลด)
    browser/context: ถ้าส่งมา = ใช้ instance เดียวกัน (เร็วขึ้นมาก)
    คืน list[{"id": int, "url": str, "name": str}]
    """
    from playwright.async_api import async_playwright
    
    products: list[dict] = []
    shop_handle = parse_shop_handle(shop_url)
    
    # ถ้ามี browser/context ส่งมาแล้ว = ใช้เลย (เร็วขึ้น)
    if browser and context:
        page = await context.new_page()
        try:
            await page.goto(shop_url, wait_until="domcontentloaded", timeout=10000)
            try:
                await page.wait_for_selector('a[href*="/product/"]', timeout=3000)
            except Exception:
                log.debug("ไม่พบ product links ภายใน 3 วินาที")
            
            products = await _extract_products_from_page(page, shop_handle)
        finally:
            await page.close()
        return products
    
    # ถ้าไม่มี browser/context = เปิด-ปิดใหม่ (ช้า)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        
        # Load session cookies if available
        if Path(session_file).exists():
            with open(session_file, encoding="utf-8") as f:
                session = json.load(f)
                if "cookies" in session:
                    await context.add_cookies(session["cookies"])
        
        page = await context.new_page()
        
        try:
            # Navigate and wait for DOM ready (faster than networkidle)
            await page.goto(shop_url, wait_until="domcontentloaded", timeout=10000)
            
            # Wait for product cards to appear (or timeout after 3 seconds)
            try:
                await page.wait_for_selector('a[href*="/product/"]', timeout=3000)
            except Exception:
                log.debug("ไม่พบ product links ภายใน 3 วินาที")
            
            products = await _extract_products_from_page(page, shop_handle)
        
        finally:
            await browser.close()
    
    return products


async def _extract_products_from_page(page: Page, shop_handle: str) -> list[dict]:
    """ดึง products จาก DOM ของ page ที่เปิดอยู่แล้ว"""
    products: list[dict] = []
    product_links = await page.query_selector_all('a[href*="/product/"]')
    seen_ids: set[int] = set()
    
    for link in product_links:
        try:
            href = await link.get_attribute('href')
            if not href:
                continue
            
            # Extract product ID from URL
            # Format: /@shop_handle/product/123456 or /product/123456
            m = re.search(r'/product/(\d+)', href)
            if not m:
                continue
            
            product_id = int(m.group(1))
            if product_id <= 10000 or product_id in seen_ids:
                continue
            
            # Get product name from link text or image alt
            name = await link.text_content() or ""
            name = name.strip()
            
            # If no text, try to get from img alt
            if not name:
                img = await link.query_selector('img')
                if img:
                    name = await img.get_attribute('alt') or ""
                    name = name.strip()
            
            if not name:
                name = f"Product {product_id}"
            
            seen_ids.add(product_id)
            
            # Build full URL
            if href.startswith('http'):
                url = href
            elif href.startswith('/'):
                url = f"https://shop.line.me{href}"
            else:
                url = f"https://shop.line.me/{shop_handle}/product/{product_id}"
            
            products.append({
                "id": product_id,
                "url": url,
                "name": name,
            })
        
        except Exception as e:
            log.debug("ข้าม product link: %s", e)
            continue
    
    return products


def _extract_products_from_data(data: Any, shop_handle: str) -> list[dict]:
    """ค้นหา product objects ใน JSON data และคืน list ของ products"""
    products: list[dict] = []
    seen_ids: set[int] = set()

    def _search(obj, depth: int = 0):
        if depth > 25:
            return
        if isinstance(obj, dict):
            # ตรวจสอบว่าเป็น product object หรือไม่
            if "id" in obj or "productId" in obj:
                pid = obj.get("id") or obj.get("productId")
                if isinstance(pid, int) and pid > 10000 and pid not in seen_ids:
                    name = obj.get("name") or obj.get("productName") or obj.get("title") or ""
                    if isinstance(name, str):
                        seen_ids.add(pid)
                        products.append({
                            "id": pid,
                            "url": f"https://shop.line.me/{shop_handle}/product/{pid}",
                            "name": name.strip(),
                        })
            for v in obj.values():
                _search(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _search(item, depth + 1)

    _search(data)
    return products


def select_first_available_variant(variants: list[dict]) -> Optional[dict]:
    """
    เลือก variant แรกที่ available != 0
    ถ้าไม่มี variant ที่มีสต็อก → คืน variant แรก (available == -1)
    """
    if not variants:
        return None

    # ลองหา variant ที่ available > 0 หรือ -1 (ไม่ทราบสต็อก)
    for v in variants:
        stock = v.get("available", -1)
        if stock != 0:
            return v

    # ถ้าทุก variant หมด → คืน variant แรก
    return variants[0]


async def monitor_shop_for_new_products(
    shop_url: str,
    sale_start_time: str,
    check_interval_ms: int,
    session_file: str,
    product_name_pattern: Optional[str] = None,
    force_polling: bool = False,
) -> dict:
    """
    Monitor ร้านและคืน product ใหม่ที่โผล่ขึ้นมา
    force_polling: ถ้า True จะข้ามการรอเวลาและเริ่ม polling ทันที
    Returns: {"id": int, "url": str, "name": str}
    """
    log.info("📡 โหลด baseline products...")
    baseline = await fetch_shop_products(shop_url, session_file)
    baseline_ids = {p["id"] for p in baseline}
    log.info("📡 Baseline products: %d รายการ", len(baseline_ids))
    if baseline:
        log.info("📦 สินค้าที่เจอ:")
        for p in baseline:
            log.info("   • [%d] %s", p["id"], p["name"])
    else:
        log.warning("⚠️  ไม่พบสินค้าใด (อาจเป็นเพราะร้านยังไม่เปิดขาย)")

    # รอเวลา sale_start_time (ยกเว้นถ้า force_polling=True)
    if not force_polling:
        target_time = parse_sale_time(sale_start_time)
        now = datetime.now()
        target_dt = datetime.combine(now.date(), target_time)

        # ถ้าเวลาผ่านไปแล้ว → ใช้วันถัดไป
        if target_dt < now:
            from datetime import timedelta
            target_dt += timedelta(days=1)

        wait_seconds = (target_dt - datetime.now()).total_seconds()
        if wait_seconds > 0:
            log.info("⏰ รอจนถึงเวลาขาย %s (อีก %.1f วินาที)...", sale_start_time, wait_seconds)
            await asyncio.sleep(wait_seconds)
    else:
        log.info("⚡ force_polling=True — เริ่ม polling ทันทีโดยไม่รอเวลาขาย")

    interval_sec = check_interval_ms / 1000.0
    pattern = re.compile(product_name_pattern) if product_name_pattern else None
    poll_count = 0
    
    log.info("🔍 เริ่ม polling shop...")
    log.info("   ⏱️  Interval: %.2f วินาที", interval_sec)
    log.info("   🎯 Pattern: %s", product_name_pattern or "(ทุกสินค้า)")
    print("\n💡 กดปุ่มใดก็ได้เพื่อหยุด polling และเลือกสินค้าด้วย arrow keys\n")

    # สร้าง browser instance เดียวสำหรับ polling ทั้งหมด (เร็วขึ้นมาก)
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    poll_browser = await pw.chromium.launch(headless=True)
    poll_context = await poll_browser.new_context()
    
    # Load session cookies if available
    if Path(session_file).exists():
        with open(session_file, encoding="utf-8") as f:
            session = json.load(f)
            if "cookies" in session:
                await poll_context.add_cookies(session["cookies"])

    try:
        while True:
            try:
                # ตรวจจับ keyboard interrupt (Windows)
                if sys.platform == 'win32' and msvcrt.kbhit():
                    msvcrt.getch()  # consume the key
                    log.info("⏸️  ตรวจพบการกดปุ่ม — หยุด polling ชั่วคราว")
                    print("\n⏸️  หยุด polling ชั่วคราว...\n")
                    
                    # fetch products ปัจจุบัน
                    current = await fetch_shop_products(shop_url, session_file, poll_browser, poll_context)
                    if not current:
                        print("❌ ไม่พบสินค้าในขณะนี้ — กลับไป polling\n")
                        log.warning("ไม่พบสินค้า — กลับไป polling")
                        await asyncio.sleep(1)
                        continue
                    
                    # แสดง interactive menu
                    print(f"📦 พบสินค้าทั้งหมด: {len(current)} รายการ\n")
                    selected = await select_product_interactive(current)
                    
                    print(f"\n✅ เลือก: \033[96m{selected['name']}\033[0m")
                    print(f"   ID: {selected['id']}")
                    print(f"   URL: {selected['url']}\n")
                    log.info("✅ เลือกสินค้า: %s", selected["name"])
                    log.info("   ID: %d", selected["id"])
                    log.info("   URL: %s", selected["url"])
                    return selected
                
                poll_count += 1
                poll_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                log.info("🔄 Poll #%d [%s] กำลังตรวจสอบร้าน...", poll_count, poll_time)
                
                current = await fetch_shop_products(shop_url, session_file, poll_browser, poll_context)
                current_ids = {p["id"] for p in current}
                new_ids = current_ids - baseline_ids
                
                log.info("   📊 สินค้าทั้งหมด: %d | ใหม่: %d", len(current_ids), len(new_ids))

                if new_ids:
                    new_products = [p for p in current if p["id"] in new_ids]
                    print("\n" + "="*60)
                    print("🆕 ตรวจพบสินค้าใหม่ %d รายการ:" % len(new_products))
                    print("="*60)
                    for p in new_products:
                        print(f"\033[92m   ✨ [{p['id']}] {p['name']}\033[0m")
                    print("="*60 + "\n")
                    log.info("🆕 ตรวจพบสินค้าใหม่ %d รายการ:", len(new_products))
                    for p in new_products:
                        log.info("   • [%d] %s", p["id"], p["name"])

                    # filter ตาม pattern ถ้ามี
                    if pattern:
                        filtered = [p for p in new_products if pattern.search(p["name"])]
                        if filtered:
                            selected = filtered[0]
                            log.info("✓ เลือกสินค้าที่ตรง pattern: %s", selected["name"])
                            print(f"\033[93m✓ เลือกสินค้าที่ตรง pattern: {selected['name']}\033[0m")
                        else:
                            log.warning("⚠️  สินค้าใหม่ไม่ตรงกับ pattern '%s' — เลือกตัวแรก", product_name_pattern)
                            selected = new_products[0]
                    else:
                        selected = new_products[0]
                        log.info("✓ เลือกสินค้าแรก: %s", selected["name"])
                        print(f"\033[93m✓ เลือกสินค้าแรก: {selected['name']}\033[0m")

                    print("\n" + "🚀 เริ่ม checkout flow")
                    print(f"   ID: {selected['id']}")
                    print(f"   Name: \033[96m{selected['name']}\033[0m")
                    print(f"   URL: {selected['url']}\n")
                    log.info("🚀 เริ่ม checkout flow")
                    log.info("   ID: %d", selected["id"])
                    log.info("   Name: %s", selected["name"])
                    log.info("   URL: %s", selected["url"])
                    return selected

            except Exception as e:
                log.warning("⚠️  Poll error: %s — ลองใหม่...", e)

            await asyncio.sleep(interval_sec)
    
    finally:
        # ปิด browser เมื่อจบ polling
        await poll_browser.close()
        await pw.stop()


async def select_product_interactive(products: list[dict]) -> dict:
    """แสดง interactive menu ให้เลือก product ด้วย arrow keys"""
    try:
        from pick import pick
    except ImportError:
        log.warning("⚠️  ไม่พบ 'pick' library — ติดตั้งด้วย: pip install pick")
        log.warning("   ใช้สินค้าตัวแรกแทน")
        return products[0]
    
    title = "🎯 เลือกสินค้าที่ต้องการ checkout (ใช้ ↑↓ เลือก, Enter ยืนยัน):"
    options = [f"[{p['id']}] {p['name']}" for p in products]
    
    try:
        selected_text, index = pick(options, title, indicator="→")
        return products[index]
    except (KeyboardInterrupt, Exception) as e:
        log.warning("⚠️  ยกเลิกการเลือก: %s — ใช้สินค้าตัวแรก", e)
        return products[0]


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
    mode = config.get("mode", "").strip().lower()

    # ==================== SHOP MONITOR MODE ====================
    if mode == "shop_monitor":
        shop_url = config.get("shop_url")
        if not shop_url:
            raise ValueError("mode=shop_monitor ต้องระบุ shop_url")

        sale_start_time = config.get("sale_start_time", "00:00:00")
        check_interval_ms = int(config.get("check_interval_ms", 500))
        session_file = config.get("session_file", "line_session.json")
        product_name_pattern = config.get("product_name_pattern")
        force_polling = bool(config.get("force_polling", False))
        auto_pick_first_variant = config.get("auto_pick_first_variant", True)
        prewarm_browser = config.get("prewarm_browser", False)
        quantity = int(config.get("quantity", 1))
        headless = bool(config.get("headless", False))
        auto_confirm = bool(config.get("auto_confirm", False))
        encoding_mode = config.get("checkout_encoding", "auto")

        log.info("🔵 โหมด: SHOP MONITOR")
        log.info("🛍️  Shop: %s", shop_url)
        log.info("⏰ Sale Time: %s", sale_start_time)
        log.info("⚡ Check Interval: %d ms", check_interval_ms)

        # Prewarm browser ถ้าเปิดใช้
        prewarmed_browser: Optional[Browser] = None
        prewarmed_context: Optional[BrowserContext] = None
        prewarmed_page: Optional[Page] = None

        if prewarm_browser:
            log.info("🔥 Prewarming browser...")
            pw = await async_playwright().start()
            session_kwargs = {}
            if Path(session_file).exists():
                session_kwargs["storage_state"] = session_file

            prewarmed_browser = await pw.chromium.launch(
                headless=headless,
                args=[
                    "--disable-extensions",
                    "--disable-default-apps",
                    "--no-first-run",
                    "--disable-sync",
                    "--disable-translate",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            prewarmed_context = await prewarmed_browser.new_context(**session_kwargs)
            await prewarmed_context.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in ["image", "font", "media", "stylesheet"]
                    else route.continue_()
                ),
            )
            prewarmed_page = await prewarmed_context.new_page()
            prewarmed_page.set_default_timeout(5_000)
            log.info("✅ Browser prewarmed")

        # Monitor ร้านจนเจอสินค้าใหม่
        new_product = await monitor_shop_for_new_products(
            shop_url=shop_url,
            sale_start_time=sale_start_time,
            check_interval_ms=check_interval_ms,
            session_file=session_file,
            product_name_pattern=product_name_pattern,
            force_polling=force_polling,
        )

        product_url = new_product["url"]
        product_id = new_product["id"]
        shop_handle = parse_shop_handle(shop_url)

        log.info("📦 Product: %s (ID: %d)", shop_handle, product_id)

        # ดึง variants
        variants: list[dict] = []
        try:
            variants = await fetch_variants_via_http(product_url, product_id, session_file)
        except Exception as e:
            log.warning("HTTP fetch variants ล้มเหลว: %s", e)

        # เลือก variant
        matched_variant: Optional[dict] = None
        matched_label = "default"

        if not variants:
            log.info("📦 ไม่มี variant — checkout โดยตรง (productVariantId=null)")
            matched_variant = {"id": None, "name": "default", "option1": None, "option2": None, "available": -1}
        elif len(variants) == 1 or auto_pick_first_variant:
            matched_variant = variants[0] if len(variants) == 1 else select_first_available_variant(variants)
            if matched_variant:
                matched_label = matched_variant["name"]
                vid = matched_variant["id"]
                log.info(
                    "📦 เลือก variant แรก: %s (ID: %s)",
                    matched_label, vid if vid is not None else "null",
                )
        else:
            # มีหลาย variant แต่ไม่ auto_pick
            log.error("❌ สินค้ามีหลาย variants แต่ไม่ได้เปิด auto_pick_first_variant")
            if prewarmed_browser:
                await prewarmed_browser.close()
            return

        if not matched_variant:
            log.error("❌ ไม่สามารถเลือก variant ได้")
            if prewarmed_browser:
                await prewarmed_browser.close()
            return

        # สร้าง checkout URL
        variant_id_for_url = matched_variant["id"]
        checkout_url = build_checkout_url(
            shop_handle=shop_handle,
            product_id=product_id,
            variant_id=variant_id_for_url,
            quantity=quantity,
            encoding_mode=encoding_mode,
        )
        log.info("🔗 Checkout URL: %s", checkout_url)

        # ใช้ prewarmed browser หรือเปิดใหม่
        if prewarmed_browser and prewarmed_page:
            page = prewarmed_page
            browser = prewarmed_browser
            context = prewarmed_context
            log.info("🌐 ใช้ prewarmed browser")
        else:
            log.info("🌐 เปิด browser...")
            pw = await async_playwright().start()
            session_kwargs = {}
            if Path(session_file).exists():
                session_kwargs["storage_state"] = session_file

            browser = await pw.chromium.launch(
                headless=headless,
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
            await context.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in ["image", "font", "media", "stylesheet"]
                    else route.continue_()
                ),
            )
            page = await context.new_page()
            page.set_default_timeout(5_000)

        await page.goto(checkout_url, wait_until="domcontentloaded", timeout=15_000)
        log.info("✅ ถึงหน้า checkout — %s", page.url)

        # เลือก PromptPay
        paid = await select_promptpay(page)
        if not paid:
            log.error("❌ เลือก PromptPay ไม่สำเร็จ")
            await browser.close()
            return

        log.info("💳 เลือก PromptPay แล้ว")

        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await close_popup(page, "before-place-order")

        # ดึงราคา
        actual_price = ""
        try:
            html_content = await page.content()
            all_prices = re.findall(r"฿\s*([0-9,]+(?:\.[0-9]{2})?)", html_content)
            price_values: list[int] = []
            for p in all_prices:
                val_str = p.replace(",", "")
                price_values.append(int(float(val_str)) if "." in val_str else int(val_str))
            significant = sorted([p for p in set(price_values) if p >= 100], reverse=True)
            if significant:
                actual_price = str(significant[0])
                log.info("✅ ราคา: ฿%s", actual_price)
        except Exception as price_err:
            log.warning("ดึงราคาจาก checkout ไม่ได้: %s", price_err)

        # แสดงสรุป
        print()
        print("=" * 60)
        print("📦 พร้อม Place Order (SHOP MONITOR MODE)")
        print("=" * 60)
        print(f"สินค้า : {new_product['name']}")
        if actual_price:
            print(f"ราคา   : ฿{actual_price}")
        print(f"ตัวเลือก: {matched_label}")
        print(f"จำนวน  : {quantity}")
        print(f"ร้านค้า : {shop_handle}")
        print(f"ชำระเงิน: PromptPay")
        print("=" * 60)
        print()

        if not auto_confirm:
            await asyncio.to_thread(
                input, ">>> กด Enter เพื่อ Place Order หรือ Ctrl+C เพื่อยกเลิก: "
            )

        # Place Order
        place_order_btn = page.locator(
            'button:has-text("Place Order"), button:has-text("สั่งซื้อ"), '
            '[role="button"]:has-text("Place Order"), [role="button"]:has-text("สั่งซื้อ")'
        ).first

        try:
            url_before = page.url
            await place_order_btn.click(timeout=5_000)
            log.info("🛒 กด Place Order...")

            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

            url_after = page.url

            # ── เช็ค Sold Out popup ก่อน (ใช้เวลาน้อย) ──
            sold_out_selectors = [
                "text=/sold out/i",
                "text=/หมดแล้ว/",
                "text=/สินค้าหมด/",
                "text=/out of stock/i",
                "text=/ขายหมดแล้ว/",
                '[class*="sold-out"]',
                '[class*="out-of-stock"]',
                '[class*="soldout"]',
            ]
            found_sold_out: list[str] = []
            for sel in sold_out_selectors:
                try:
                    msgs = await page.locator(sel).all_text_contents()
                    found_sold_out.extend(m.strip() for m in msgs if m.strip())
                except Exception:
                    pass

            if found_sold_out:
                log.error("❌ สินค้าหมด (Sold Out): %s", found_sold_out)
                print("\n" + "=" * 60)
                print("❌ สินค้าหมดแล้ว (SOLD OUT)")
                print("=" * 60)
                for msg in found_sold_out:
                    print(f"  • {msg}")
                print("=" * 60 + "\n")
                try:
                    await page.screenshot(path="debug_sold_out.png")
                    log.info("บันทึก screenshot: debug_sold_out.png")
                except Exception:
                    pass
                # ไม่ต้อง check error อื่นแล้ว — จบที่นี่
                log.info("✅ เสร็จสิ้น — ปิด browser")
                await browser.close()
                return

            if url_before == url_after:
                ignore_texts = {"ดูทั้งหมด", "รวมทั้งหมด", "total", "view all", "see all"}
                error_selectors = [
                    "text=/error/i",
                    "text=/ผิดพลาด/i",
                    '[role="alert"]',
                    '[class*="error"]',
                    '[class*="Error"]',
                ]
                found_errors: list[str] = []
                for sel in error_selectors:
                    try:
                        msgs = await page.locator(sel).all_text_contents()
                        found_errors.extend(
                            m.strip()
                            for m in msgs
                            if m.strip() and m.strip().lower() not in ignore_texts
                        )
                    except Exception:
                        pass

                if found_errors:
                    log.error("❌ พบ error: %s", found_errors)
                    print("\n" + "=" * 60)
                    print("❌ ไม่สามารถ Place Order ได้")
                    print("=" * 60)
                    for err in found_errors:
                        print(f"  • {err}")
                    print("=" * 60 + "\n")
                else:
                    log.warning("⚠️  URL ไม่เปลี่ยน และไม่พบ error message")

                try:
                    await page.screenshot(path="debug_place_order_failed.png")
                    log.info("บันทึก screenshot: debug_place_order_failed.png")
                except Exception:
                    pass

            else:
                log.info("✅ URL เปลี่ยน → %s", url_after)
                if any(k in url_after for k in ("payment", "success", "order")):
                    log.info("✅ ไปหน้า payment/success/order แล้ว")
                else:
                    log.warning("⚠️  URL ไม่ใช่หน้า payment/success: %s", url_after)

                log.info("รอ QR code / payment page...")
                try:
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass

                try:
                    await page.screenshot(path="debug_payment_page.png")
                    log.info("บันทึก screenshot: debug_payment_page.png")
                except Exception:
                    pass

        except Exception as place_err:
            log.error("❌ กด Place Order ล้มเหลว: %s", place_err)
            try:
                await page.screenshot(path="debug_place_order_error.png")
                log.info("บันทึก screenshot: debug_place_order_error.png")
            except Exception:
                pass

        log.info("✅ เสร็จสิ้น — ปิด browser")
        await browser.close()
        return

    # ==================== PRODUCT URL MODE (เดิม) ====================
    product_url: str = config["product_url"]
    quantity: int = int(config.get("quantity", 1))

    # ── preferred_1 / preferred_2 (backward-compatible with old keys) ──
    pref1_raw = (
        config.get("preferred_1")
        or config.get("preferred_sizes")
        or config.get("size")
        or []
    )
    preferred_1: list[str] = (
        [pref1_raw] if isinstance(pref1_raw, str) else list(pref1_raw)
    )
    pref2_raw = (
        config.get("preferred_2")
        or config.get("preferred_colors")
        or config.get("color")
        or []
    )
    preferred_2: list[str] = (
        [pref2_raw] if isinstance(pref2_raw, str) else list(pref2_raw)
    )

    headless: bool = bool(config.get("headless", False))           # Bug B fix
    session_file: str = config.get("session_file", "line_session.json")
    encoding_mode: str = config.get("checkout_encoding", "auto")
    check_interval: int = int(config.get("check_interval_seconds", 30))
    max_stock_checks: int = int(config.get("max_stock_checks", 120))  # Bug C fix
    auto_confirm: bool = bool(config.get("auto_confirm", False))

    product_id = parse_product_id(product_url)
    shop_handle = parse_shop_handle(product_url)

    log.info("🛍️  Product: %s (ID: %d)", shop_handle, product_id)
    if preferred_1:
        log.info("🔹 Preferred #1: %s", preferred_1)
    if preferred_2:
        log.info("🔹 Preferred #2: %s", preferred_2)

    # ── ขั้นที่ 1: ดึง product info และ variants ผ่าน HTTP ──
    variants: list[dict] = []
    product_info: dict = {}
    try:
        variants, product_info = await asyncio.gather(
            fetch_variants_via_http(product_url, product_id, session_file),
            fetch_product_info(product_url, session_file),
        )
    except Exception as e:
        log.warning("HTTP fetch ล้มเหลว: %s", e)

    # ── ขั้นที่ 2: เลือก variant ──
    matched_variant: Optional[dict] = None
    matched_label: str = "default"

    if not variants:
        # 0 variant: สินค้าไม่มีตัวเลือก → productVariantId = null
        log.info("📦 ไม่มี variant — checkout โดยตรง (productVariantId=null)")
        matched_variant = {"id": None, "name": "default", "option1": None, "option2": None, "available": -1}
        matched_label = "default"

    elif len(variants) == 1:
        # 1 variant: เลือกอัตโนมัติ ไม่ว่าสต็อกจะเป็นเท่าไร
        matched_variant = variants[0]
        matched_label = matched_variant["name"]
        vid = matched_variant["id"]
        log.info(
            "📦 Variant เดียว: %s (ID: %s) — เลือกอัตโนมัติ",
            matched_label, vid if vid is not None else "null",
        )

    else:
        # 2+ variants: ต้องหา variant ที่ตรงกับ preferred_1 / preferred_2
        variant_names = [v["name"] for v in variants]
        log.info("📦 Variants (%d): %s", len(variants), ", ".join(variant_names))

        if not preferred_1 and not preferred_2:
            log.error(
                "❌ สินค้ามี %d variants แต่ไม่ได้ระบุ preferred_1 หรือ preferred_2",
                len(variants),
            )
            log.error("   Variants ที่มี: %s", variant_names)
            return

        # ── stock-check loop พร้อม max_stock_checks ──
        checks_done = 0
        while checks_done < max_stock_checks:
            candidate = find_matching_variant(variants, preferred_1, preferred_2)

            if candidate is None:
                log.error("❌ ไม่พบ variant ที่ตรงกับ pref1=%s pref2=%s", preferred_1, preferred_2)
                log.error("   Variants ที่มี: %s", [v["name"] for v in variants])
                return

            stock = candidate.get("available", -1)
            # available == -1 หมายถึงไม่ทราบสต็อก → ถือว่ามีของ
            if stock != 0:
                matched_variant = candidate
                matched_label = candidate["name"]
                vid = candidate["id"]
                log.info(
                    "✅ เลือก: %s (ID: %s) สต็อก: %s",
                    matched_label,
                    vid if vid is not None else "null",
                    "ไม่ทราบ" if stock == -1 else str(stock),
                )
                break

            checks_done += 1
            remaining = max_stock_checks - checks_done
            log.warning(
                "⏳ '%s' หมดสต็อก — รอ %d วินาที... (เหลือ %d ครั้ง)",
                candidate["name"], check_interval, remaining,
            )
            if remaining <= 0:
                log.error("❌ หมดจำนวนครั้งตรวจสต็อก (%d) — หยุด", max_stock_checks)
                return

            await asyncio.sleep(check_interval)

            # ดึง variants ใหม่
            try:
                variants = await fetch_variants_via_http(product_url, product_id, session_file)
                if not variants:
                    log.error("❌ ดึง variants ไม่ได้ — หยุด")
                    return
            except Exception as fetch_err:
                log.warning("HTTP fetch ล้มเหลว: %s — ข้ามรอบนี้", fetch_err)
                continue

        if matched_variant is None:
            return

    # ── ขั้นที่ 3: สร้าง Checkout URL ──
    variant_id_for_url = matched_variant["id"]
    checkout_url = build_checkout_url(
        shop_handle=shop_handle,
        product_id=product_id,
        variant_id=variant_id_for_url,
        quantity=quantity,
        encoding_mode=encoding_mode,
    )
    log.info("🔗 Checkout URL: %s", checkout_url)

    # ── ขั้นที่ 4: เปิด browser ──
    session_kwargs: dict = {}
    if Path(session_file).exists():
        session_kwargs["storage_state"] = session_file

    log.info("🌐 เปิด browser (headless=%s)...", headless)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,                                       # Bug B fix
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
        await context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ["image", "font", "media", "stylesheet"]
                else route.continue_()
            ),
        )

        page = await context.new_page()
        page.set_default_timeout(5_000)

        await page.goto(checkout_url, wait_until="domcontentloaded", timeout=15_000)
        log.info("✅ ถึงหน้า checkout — %s", page.url)

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
            html_content = await page.content()
            with open("debug_checkout_price.html", "w", encoding="utf-8") as f:
                f.write(html_content)
            log.info("📝 Dump HTML → debug_checkout_price.html")

            all_prices = re.findall(r"฿\s*([0-9,]+(?:\.[0-9]{2})?)", html_content)
            price_values: list[int] = []
            for p in all_prices:
                val_str = p.replace(",", "")
                price_values.append(int(float(val_str)) if "." in val_str else int(val_str))

            log.info("🔍 ราคาทั้งหมด (raw): %s", sorted(set(price_values), reverse=True))
            significant = sorted([p for p in set(price_values) if p >= 100], reverse=True)
            log.info("💰 ราคา ≥100: %s", significant)

            if significant:
                actual_price = str(significant[0])
                log.info("✅ ราคา: ฿%s", actual_price)
        except Exception as price_err:
            log.warning("ดึงราคาจาก checkout ไม่ได้: %s", price_err)

        # ── ขั้นที่ 7: แสดงสรุป และรอยืนยัน ──
        print()
        print("=" * 60)
        print("📦 พร้อม Place Order")
        print("=" * 60)
        if product_info.get("name"):
            print(f"สินค้า : {product_info['name']}")
        display_price = actual_price or product_info.get("price", "")
        if display_price:
            print(f"ราคา   : ฿{display_price}")
        print(f"ตัวเลือก: {matched_label}")
        print(f"จำนวน  : {quantity}")
        print(f"ร้านค้า : {shop_handle}")
        print(f"ชำระเงิน: PromptPay")
        print("=" * 60)
        print()

        if not auto_confirm:
            await asyncio.to_thread(
                input, ">>> กด Enter เพื่อ Place Order หรือ Ctrl+C เพื่อยกเลิก: "
            )

        # ── ขั้นที่ 8: Place Order ──
        place_order_btn = page.locator(
            'button:has-text("Place Order"), button:has-text("สั่งซื้อ"), '
            '[role="button"]:has-text("Place Order"), [role="button"]:has-text("สั่งซื้อ")'
        ).first

        try:
            url_before = page.url
            await place_order_btn.click(timeout=5_000)
            log.info("🛒 กด Place Order...")

            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

            url_after = page.url

            # ── เช็ค Sold Out popup ก่อน (ใช้เวลาน้อย) ──
            sold_out_selectors = [
                "text=/sold out/i",
                "text=/หมดแล้ว/",
                "text=/สินค้าหมด/",
                "text=/out of stock/i",
                "text=/ขายหมดแล้ว/",
                '[class*="sold-out"]',
                '[class*="out-of-stock"]',
                '[class*="soldout"]',
            ]
            found_sold_out: list[str] = []
            for sel in sold_out_selectors:
                try:
                    msgs = await page.locator(sel).all_text_contents()
                    found_sold_out.extend(m.strip() for m in msgs if m.strip())
                except Exception:
                    pass

            if found_sold_out:
                log.error("❌ สินค้าหมด (Sold Out): %s", found_sold_out)
                print("\n" + "=" * 60)
                print("❌ สินค้าหมดแล้ว (SOLD OUT)")
                print("=" * 60)
                for msg in found_sold_out:
                    print(f"  • {msg}")
                print("=" * 60 + "\n")
                try:
                    await page.screenshot(path="debug_sold_out.png")
                    log.info("บันทึก screenshot: debug_sold_out.png")
                except Exception:
                    pass
                # ไม่ต้อง check error อื่นแล้ว — จบที่นี่
                log.info("✅ เสร็จสิ้น — ปิด browser")
                await browser.close()
                return

            if url_before == url_after:
                # URL ไม่เปลี่ยน — ตรวจหา error message
                ignore_texts = {"ดูทั้งหมด", "รวมทั้งหมด", "total", "view all", "see all"}
                error_selectors = [
                    "text=/error/i",
                    "text=/ผิดพลาด/i",
                    '[role="alert"]',
                    '[class*="error"]',
                    '[class*="Error"]',
                ]
                found_errors: list[str] = []
                for sel in error_selectors:
                    try:
                        msgs = await page.locator(sel).all_text_contents()
                        found_errors.extend(
                            m.strip()
                            for m in msgs
                            if m.strip() and m.strip().lower() not in ignore_texts
                        )
                    except Exception:
                        pass

                if found_errors:
                    log.error("❌ พบ error: %s", found_errors)
                    print("\n" + "=" * 60)
                    print("❌ ไม่สามารถ Place Order ได้")
                    print("=" * 60)
                    for err in found_errors:
                        print(f"  • {err}")
                    print("=" * 60 + "\n")
                else:
                    log.warning("⚠️  URL ไม่เปลี่ยน และไม่พบ error message")

                try:
                    await page.screenshot(path="debug_place_order_failed.png")
                    log.info("บันทึก screenshot: debug_place_order_failed.png")
                except Exception:
                    pass

            else:
                log.info("✅ URL เปลี่ยน → %s", url_after)
                if any(k in url_after for k in ("payment", "success", "order")):
                    log.info("✅ ไปหน้า payment/success/order แล้ว")
                else:
                    log.warning("⚠️  URL ไม่ใช่หน้า payment/success: %s", url_after)

                # รอ QR code หรือ payment page
                log.info("รอ QR code / payment page...")
                try:
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass

                try:
                    await page.screenshot(path="debug_payment_page.png")
                    log.info("บันทึก screenshot: debug_payment_page.png")
                except Exception:
                    pass

        except Exception as place_err:
            # Bug D fix: unreachable code ย้ายมาอยู่ใน except อย่างถูกต้อง
            log.error("❌ กด Place Order ล้มเหลว: %s", place_err)
            try:
                await page.screenshot(path="debug_place_order_error.png")
                log.info("บันทึก screenshot: debug_place_order_error.png")
            except Exception:
                pass

        log.info("✅ เสร็จสิ้น — ปิด browser")
        await browser.close()


async def main() -> None:
    config = load_config()
    log.info("โหลด config จาก %s", CONFIG_FILE)
    await run(config)


if __name__ == "__main__":
    asyncio.run(main())
