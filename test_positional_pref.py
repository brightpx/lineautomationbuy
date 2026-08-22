"""ทดสอบว่า preferred_1=["1"], preferred_2=["2"] เลือก option แรก/ที่สองถูกต้อง"""
import asyncio

from checkout_direct import (
    load_config,
    parse_product_id,
    fetch_variants_via_http,
    find_matching_variant,
    find_matching_variant_with_fallback,
)


async def main():
    config = load_config()
    url = config["product_url"]
    pid = parse_product_id(url)
    variants = await fetch_variants_via_http(url, pid, config["session_file"])

    print("Variants ทั้งหมด:")
    for i, v in enumerate(variants, 1):
        print(f"  [{i}] {v['name']} (option1={v.get('option1')}, option2={v.get('option2')})")

    m = find_matching_variant(variants, config.get("preferred_1"), config.get("preferred_2"))
    print()
    print("preferred_1 =", config.get("preferred_1"), "→ option แรก")
    print("preferred_2 =", config.get("preferred_2"), "→ option ที่สอง")
    print("Match (ตำแหน่งตรง):", m["name"] if m else "ไม่พบ")

    fb = find_matching_variant_with_fallback(
        variants, config.get("preferred_1"), config.get("preferred_2")
    )
    if fb:
        stock = fb.get("available", -1)
        status = "หมด" if stock == 0 else ("ไม่ทราบ" if stock == -1 else f"มี {stock} ชิ้น")
        print(f"Fallback (เลื่อนอัตโนมัติถ้าหมด): {fb['name']} — สต็อก: {status}")
    else:
        print("Fallback: ไม่พบ")


asyncio.run(main())
