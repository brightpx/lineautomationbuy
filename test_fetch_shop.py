"""Test fetch_shop_products with Playwright"""
import asyncio
from checkout_direct import fetch_shop_products

async def main():
    print("Testing fetch_shop_products...")
    result = await fetch_shop_products('https://shop.line.me/@mergeth', 'line_session.json')
    print(f'\n✅ Found {len(result)} products:\n')
    for p in result:
        print(f'  [{p["id"]}] {p["name"]}')

if __name__ == "__main__":
    asyncio.run(main())
