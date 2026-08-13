"""
ทดสอบกรณีไม่มี variants หรือมี variant เดียว
"""

# จำลองการทำงานของโค้ด

def test_variant_selection():
    print("=" * 60)
    print("🧪 ทดสอบ: กรณีไม่มี variants / variant เดียว")
    print("=" * 60)
    print()
    
    # กรณีที่ 1: ไม่มี variants เลย
    print("📋 กรณีที่ 1: ไม่มี variants")
    print("-" * 60)
    variants = []
    product_id = 1008243591
    
    if not variants:
        print("⚠️ ไม่พบ variants — ข้ามขั้นตอนเลือก variant")
        matched_variant = {"id": product_id, "name": "default", "available": 1}
        matched_size = "default"
        print(f"✅ ใช้ค่า default: ID={matched_variant['id']}, name='{matched_size}'")
    print()
    
    # กรณีที่ 2: มี variant เดียว
    print("📋 กรณีที่ 2: มี variant เดียว")
    print("-" * 60)
    variants = [
        {"id": 12345, "name": "One Size", "available": 1}
    ]
    
    if len(variants) == 1:
        matched_variant = variants[0]
        matched_size = matched_variant["name"]
        print(f"📦 Variant เดียว: {matched_size} (ID: {matched_variant['id']}) — เลือกอัตโนมัติ")
        print(f"✅ ข้ามขั้นตอนการค้นหา/เลือก → ไป checkout ทันที")
    print()
    
    # กรณีที่ 3: มีหลาย variants (ปกติ)
    print("📋 กรณีที่ 3: มีหลาย variants")
    print("-" * 60)
    variants = [
        {"id": 27496575, "name": "12-18m", "available": 0},
        {"id": 27496576, "name": "18-24m", "available": 1},
        {"id": 27496577, "name": "2-3y", "available": 1},
        {"id": 27496578, "name": "3-4y", "available": 1}
    ]
    preferred_sizes = ["18-24m"]
    
    variant_names = [v['name'] for v in variants]
    print(f"📦 Variants ({len(variants)}): {', '.join(variant_names)}")
    
    matched_variant = None
    matched_size = ""
    
    for size in preferred_sizes:
        for v in variants:
            if v["name"].strip().lower() == size.strip().lower():
                matched_variant = v
                matched_size = size
                break
        if matched_variant:
            break
    
    if matched_variant:
        stock = matched_variant.get("available", 0)
        if stock > 0:
            print(f"✅ เลือก: {matched_size} (ID: {matched_variant['id']}) — มีสต็อก: {stock}")
            print(f"✅ ดำเนินการ checkout ต่อ")
        else:
            print(f"❌ {matched_size} หมดสต็อก — วน loop รอ")
    
    print()
    print("=" * 60)
    print("✅ ทดสอบเสร็จสิ้น")
    print("=" * 60)
    print()
    print("📋 สรุป:")
    print("  • ไม่มี variants → ใช้ product_id เป็น variant_id")
    print("  • มี variant เดียว → เลือกอัตโนมัติ ไม่ต้องระบุ preferred_sizes")
    print("  • มีหลาย variants → ค้นหาตาม preferred_sizes และเช็คสต็อก")
    print()

if __name__ == "__main__":
    test_variant_selection()
