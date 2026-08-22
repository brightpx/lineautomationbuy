"""ทดสอบ API-first helpers ใน checkout_direct.py (add-to-cart + mark checkout + URL)"""
import asyncio

from checkout_direct import build_checkout_url_via_api


async def main():
    url = await build_checkout_url_via_api(
        shop_handle="@thelandofvava",
        product_id=1008243987,
        variant_id=27499188,
        quantity=1,
        session_file="line_session.json",
    )
    print(f"\ncheckout URL: {url}")
    assert url and "checkout/cart?id=" in url, "❌ ได้ URL ผิดรูปแบบ"
    print("✅ PASS")


asyncio.run(main())
