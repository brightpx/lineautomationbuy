"""
ทดสอบ logic รอเติมสต็อก (mock ทั้งหมด ไม่ยิงเว็บจริง):
1. poll_variant_stock: หมด→หมด→กลับมามีของ = True
2. poll_variant_stock: หมดตลอดจนครบ max_checks = False
3. place_order_with_restock_retry: sold_out → รอเติม → placed
4. place_order_with_restock_retry: placed ครั้งแรก (ไม่ retry)
5. place_order_with_restock_retry: error ไม่ retry
6. variant_id=None → True ทันที

Run: & .venv\\Scripts\\python.exe test_restock_flow.py
"""
import asyncio
import sys
from unittest.mock import AsyncMock, patch

import checkout_direct as cd


def make_variant(vid, name, available):
    return {"id": vid, "name": name, "option1": None, "option2": None,
            "available": available}


async def main() -> int:
    failures = []

    # ── Test 1: หมด → หมด → กลับมามีของ ──
    seq = [
        [make_variant(27499188, "4y", 0)],
        [make_variant(27499188, "4y", 0)],
        [make_variant(27499188, "4y", 3)],
    ]
    with patch.object(cd, "fetch_variants_via_http", side_effect=seq), \
         patch.object(cd.asyncio, "sleep", new=AsyncMock()):
        ok = await cd.poll_variant_stock(
            "https://shop.line.me/@x/product/1", 1, 27499188,
            "line_session.json", check_interval=0, max_checks=10)
    print(("PASS" if ok else "FAIL") + " — Test 1: restock กลับมา → True")
    if not ok:
        failures.append("test1")

    # ── Test 2: หมดตลอด → False ──
    async def always_sold_out(*a, **k):
        return [make_variant(27499188, "4y", 0)]
    with patch.object(cd, "fetch_variants_via_http", side_effect=always_sold_out), \
         patch.object(cd.asyncio, "sleep", new=AsyncMock()):
        ok = await cd.poll_variant_stock(
            "https://shop.line.me/@x/product/1", 1, 27499188,
            "line_session.json", check_interval=0, max_checks=5)
    print(("PASS" if not ok else "FAIL") + " — Test 2: หมดตลอด → False")
    if ok:
        failures.append("test2")

    # ── Test 3: sold_out → รอเติม → retry → placed ──
    click_results = [cd.PO_SOLD_OUT, cd.PO_PLACED]
    click_calls = {"n": 0}

    async def fake_click(p):
        r = click_results[min(click_calls["n"], len(click_results) - 1)]
        click_calls["n"] += 1
        return r

    with patch.object(cd, "click_place_order_and_verify", side_effect=fake_click), \
         patch.object(cd, "confirm_place_order", new=AsyncMock()), \
         patch.object(cd, "poll_variant_stock", return_value=True), \
         patch.object(cd, "build_checkout_url_via_api",
                      new=AsyncMock(return_value="https://shop.line.me/@x/checkout/cart?id=c1")), \
         patch.object(cd, "finalize_checkout", new=AsyncMock(return_value=True)), \
         patch.object(cd, "extract_price_from_page", new=AsyncMock(return_value="350")), \
         patch.object(cd, "print_order_summary"):
        status = await cd.place_order_with_restock_retry(
            page=None, auto_confirm=True,
            product_url="u", product_id=1, product_name="n", shop_handle="@s",
            variant_id=27499188, variant_label="4y", quantity=1,
            session_file="f", encoding_mode="auto", use_api_first=True,
            check_interval=0, max_stock_checks=5)
    print(("PASS" if status == cd.PO_PLACED else "FAIL")
          + f" — Test 3: sold_out → restock → retry → {status}")
    if status != cd.PO_PLACED:
        failures.append("test3")

    # ── Test 4: placed ครั้งแรก ไม่ retry ──
    calls = {"n": 0}
    async def once_placed(p):
        calls["n"] += 1
        return cd.PO_PLACED
    with patch.object(cd, "click_place_order_and_verify", side_effect=once_placed), \
         patch.object(cd, "confirm_place_order", new=AsyncMock()), \
         patch.object(cd, "poll_variant_stock",
                      new=AsyncMock(side_effect=AssertionError("must not poll"))):
        status = await cd.place_order_with_restock_retry(
            page=None, auto_confirm=True,
            product_url="u", product_id=1, product_name="n", shop_handle="@s",
            variant_id=27499188, variant_label="4y", quantity=1,
            session_file="f", encoding_mode="auto", use_api_first=True,
            check_interval=0, max_stock_checks=5)
    print(("PASS" if status == cd.PO_PLACED and calls["n"] == 1 else "FAIL")
          + f" — Test 4: placed ครั้งเดียว (calls={calls['n']})")
    if status != cd.PO_PLACED or calls["n"] != 1:
        failures.append("test4")

    # ── Test 5: error → ไม่ retry ──
    with patch.object(cd, "click_place_order_and_verify",
                      new=AsyncMock(return_value=cd.PO_ERROR)), \
         patch.object(cd, "confirm_place_order", new=AsyncMock()), \
         patch.object(cd, "poll_variant_stock",
                      new=AsyncMock(side_effect=AssertionError("must not poll"))):
        status = await cd.place_order_with_restock_retry(
            page=None, auto_confirm=True,
            product_url="u", product_id=1, product_name="n", shop_handle="@s",
            variant_id=27499188, variant_label="4y", quantity=1,
            session_file="f", encoding_mode="auto", use_api_first=True,
            check_interval=0, max_stock_checks=5)
    print(("PASS" if status == cd.PO_ERROR else "FAIL")
          + f" — Test 5: error → {status} (no retry)")
    if status != cd.PO_ERROR:
        failures.append("test5")

    # ── Test 6: variant_id=None → poll True ทันที ──
    ok = await cd.poll_variant_stock(
        "u", 1, None, "f", check_interval=0, max_checks=5)
    print(("PASS" if ok else "FAIL") + " — Test 6: variant_id=None → True")
    if not ok:
        failures.append("test6")

    print()
    if failures:
        print(f"❌ FAILED: {failures}")
        return 1
    print("✅ ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
