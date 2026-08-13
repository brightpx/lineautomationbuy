"""Debug why products aren't being extracted"""
import asyncio
import json
import re
from playwright.async_api import async_playwright
from pathlib import Path

async def main():
    shop_url = "https://shop.line.me/@sukiteenoi"
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        print(f"Loading {shop_url}...")
        await page.goto(shop_url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)
        
        html = await page.content()
        print(f"HTML length: {len(html)} chars")
        
        # Extract __NUXT_DATA__
        m = re.search(r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>([\s\S]*?)</script>', html)
        if m:
            print("✅ Found __NUXT_DATA__")
            data = json.loads(m.group(1).strip())
            print(f"Data type: {type(data)}")
            print(f"Data length: {len(data) if isinstance(data, list) else 'N/A'}")
            
            # Save to file
            with open("debug_sukiteenoi_data.json", "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print("✅ Saved to debug_sukiteenoi_data.json")
            
            # Search for products key
            if isinstance(data, list):
                for i, item in enumerate(data):
                    if isinstance(item, dict):
                        if 'products' in item:
                            print(f"\n🔍 Found 'products' key at index {i}")
                            print(f"   Value type: {type(item['products'])}")
                            print(f"   Value: {item['products']}")
                            if isinstance(item['products'], int):
                                print(f"   -> Points to index {item['products']}")
                                if item['products'] < len(data):
                                    products_array = data[item['products']]
                                    print(f"   -> Type at that index: {type(products_array)}")
                                    if isinstance(products_array, list):
                                        print(f"   -> Array length: {len(products_array)}")
                                        if products_array:
                                            print(f"   -> First item: {products_array[0]}")
        else:
            print("❌ __NUXT_DATA__ not found")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
