"""Test fetch_shop_products with a shop that has visible products"""
import asyncio
from checkout_direct import fetch_shop_products

async def main():
    # Test with @sukiteenoi (should have products visible)
    print("Testing with @sukiteenoi shop...")
    result = await fetch_shop_products('https://shop.line.me/@sukiteenoi', 'line_session.json')
    print(f'\n✅ Found {len(result)} products:\n')
    for p in result[:10]:  # Show first 10
        print(f'  [{p["id"]}] {p["name"]}')
    
    print("\n" + "="*60)
    print("Testing with @mergeth shop (may be empty)...")
    result2 = await fetch_shop_products('https://shop.line.me/@mergeth', 'line_session.json')
    print(f'\n✅ Found {len(result2)} products')
    if result2:
        for p in result2[:10]:
            print(f'  [{p["id"]}] {p["name"]}')
    else:
        print("  (ร้านนี้อาจซ่อนสินค้าไว้จนกว่าจะถึงเวลาขาย)")

if __name__ == "__main__":
    asyncio.run(main())
