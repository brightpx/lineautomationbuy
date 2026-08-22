"""
สคริปต์ทดสอบการเช็คสต็อก
แสดงข้อมูล available field จาก variants และจำลองสถานการณ์สต็อกหมด
"""

import asyncio
from checkout_direct import (
    load_config,
    parse_product_id,
    fetch_variants_via_http,
    find_matching_variant,
)

async def test_stock_check():
    print("=" * 60)
    print("🧪 ทดสอบการเช็คสต็อก")
    print("=" * 60)
    print()
    
    # โหลด config
    config = load_config()

    # ต้องเป็นโหมด Product URL เท่านั้น (shop_monitor ไม่มี product_url)
    if config.get("mode", "").strip().lower() == "shop_monitor":
        print("❌ config.json อยู่ในโหมด shop_monitor — ไม่มี product_url")
        print("   ลบ/เปลี่ยน key 'mode' ใน config.json แล้วรันใหม่")
        return

    product_url = config["product_url"]
    # รองรับทั้ง key ใหม่ (preferred_1) และเก่า (preferred_sizes)
    preferred_sizes = (
        config.get("preferred_1")
        or config.get("preferred_sizes")
        or config.get("size")
        or []
    )
    if isinstance(preferred_sizes, str):
        preferred_sizes = [preferred_sizes]
    session_file = config.get("session_file", "line_session.json")
    
    product_id = parse_product_id(product_url)
    
    print(f"📦 Product ID: {product_id}")
    print(f"🎯 ต้องการ size: {preferred_sizes}")
    print()
    
    # ดึง variants
    print("🔍 กำลังดึงข้อมูล variants...")
    variants = await fetch_variants_via_http(product_url, product_id, session_file)
    
    if not variants:
        print("❌ ไม่สามารถดึง variants ได้")
        return
    
    print(f"✅ ดึงได้ {len(variants)} variants")
    print()
    
    # แสดงข้อมูลสต็อกทั้งหมด
    print("=" * 60)
    print("📊 สถานะสต็อกทั้งหมด")
    print("=" * 60)
    
    for v in variants:
        stock = v.get("available", 0)
        status = "✅ มีสต็อก" if stock > 0 else "❌ หมดสต็อก"
        print(f"  {v['name']:15s} (ID: {v['id']:8d}) → available: {stock:3d}  {status}")
    
    print()
    
    # ทดสอบการหา size ที่เลือก — ใช้ logic เดียวกับบอทจริง
    # (รองรับทั้งชื่อ option ตรงๆ และตำแหน่ง "1"/"2")
    print("=" * 60)
    print("🎯 ตรวจสอบ size ที่เลือก")
    print("=" * 60)

    matched = find_matching_variant(variants, preferred_sizes, config.get("preferred_2"))

    if matched:
        stock = matched.get("available", 0)
        status = "ไม่ทราบ" if stock == -1 else str(stock)
        if stock != 0:
            print(f"✅ '{matched['name']}' → มีสต็อก {status} ชิ้น (ID: {matched['id']})")
        else:
            print(f"❌ '{matched['name']}' → หมดสต็อก (available: {stock})")
            print(f"   ⏳ โปรแกรมจะวนตรวจสอบทุก {config.get('check_interval_seconds', 30)} วินาที")
    else:
        print(f"❌ {preferred_sizes} → ไม่พบ variant ที่ตรง")
    
    print()
    
    # จำลองสถานการณ์หมดสต็อก
    print("=" * 60)
    print("🧪 จำลองสถานการณ์หมดสต็อก")
    print("=" * 60)
    print()
    
    # สร้าง variant ปลอมที่หมดสต็อก
    test_variants = []
    for v in variants:
        test_v = v.copy()
        # จำลองให้ทุก size หมดสต็อก
        test_v["available"] = 0
        test_variants.append(test_v)
    
    print("จำลองให้ทุก size หมดสต็อก (available = 0):")
    print()
    
    for v in test_variants:
        stock = v.get("available", 0)
        status = "✅ มีสต็อก" if stock > 0 else "❌ หมดสต็อก"
        print(f"  {v['name']:15s} → available: {stock:3d}  {status}")
    
    print()
    
    # ตรวจสอบ logic — ใช้ find_matching_variant กับ variants จำลอง (หมดสต็อก)
    matched = find_matching_variant(test_variants, preferred_sizes, config.get("preferred_2"))

    if matched:
        stock = matched.get("available", 0)
        if stock > 0:
            print(f"✅ '{matched['name']}' → ผ่านเงื่อนไข (มีสต็อก) → ดำเนินการ checkout")
        else:
            print(f"❌ '{matched['name']}' → ไม่ผ่านเงื่อนไข (หมดสต็อก) → วน loop รอตรวจสอบใหม่")
    else:
        print(f"❌ {preferred_sizes} → ไม่พบ variant ที่ตรง")
    
    print()
    print("=" * 60)
    print("✅ ทดสอบเสร็จสิ้น")
    print("=" * 60)
    print()
    print("📋 สรุป:")
    print("  • โปรแกรมดึง field 'available' จาก variants ได้จริง")
    print("  • เมื่อ available = 0 → จะวน loop รอตรวจสอบใหม่")
    print("  • เมื่อ available > 0 → จะดำเนินการ checkout ต่อทันที")
    print()

if __name__ == "__main__":
    asyncio.run(test_stock_check())
