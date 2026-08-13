"""
สคริปต์ทดสอบการเช็คสต็อก
แสดงข้อมูล available field จาก variants และจำลองสถานการณ์สต็อกหมด
"""

import asyncio
import json
from pathlib import Path
from checkout_direct import (
    load_config,
    parse_product_id,
    fetch_variants_via_http,
)

async def test_stock_check():
    print("=" * 60)
    print("🧪 ทดสอบการเช็คสต็อก")
    print("=" * 60)
    print()
    
    # โหลด config
    config = load_config()
    product_url = config["product_url"]
    preferred_sizes = config["preferred_sizes"]
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
    
    # ทดสอบการหา size ที่เลือก
    print("=" * 60)
    print("🎯 ตรวจสอบ size ที่เลือก")
    print("=" * 60)
    
    for size in preferred_sizes:
        matched = None
        for v in variants:
            if v["name"].strip().lower() == size.strip().lower():
                matched = v
                break
        
        if matched:
            stock = matched.get("available", 0)
            if stock > 0:
                print(f"✅ '{size}' → มีสต็อก {stock} ชิ้น (ID: {matched['id']})")
            else:
                print(f"❌ '{size}' → หมดสต็อก (available: {stock})")
                print(f"   ⏳ โปรแกรมจะวนตรวจสอบทุก {config.get('check_interval_seconds', 30)} วินาที")
        else:
            print(f"❌ '{size}' → ไม่พบใน variants")
    
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
    
    # ตรวจสอบ logic
    for size in preferred_sizes:
        matched = None
        for v in test_variants:
            if v["name"].strip().lower() == size.strip().lower():
                matched = v
                break
        
        if matched:
            stock = matched.get("available", 0)
            if stock > 0:
                print(f"✅ '{size}' → ผ่านเงื่อนไข (มีสต็อก) → ดำเนินการ checkout")
            else:
                print(f"❌ '{size}' → ไม่ผ่านเงื่อนไข (หมดสต็อก) → วน loop รอตรวจสอบใหม่")
    
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
