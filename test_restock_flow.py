"""
ทดสอบ logic รอเติมสต็อก (mock ทั้งหมด ไม่ยิงเว็บจริง):
1. poll_variant_stock: หมด→หมด→กลับมามีของ = คืน variant dict
2. poll_variant_stock: หมดตลอดจนครบ max_checks = None
3. place_order_with_restock_retry: sold_out → รอเติม → placed
4. place_order_with_restock_retry: placed ครั้งแรก (ไม่ retry)
5. place_order_with_restock_retry: error ไม่ retry
6. variant_id=None → sentinel dict
7. fallback: poll คืน variant ตัวใหม่ → retry ใช้ตัวใหม่
8. poll ระหว่างรอ: fallback on → เลื่อนไป option ที่มีของ
9. poll ระหว่างรอ: fallback off → ไม่เลื่อน → None
"""
import asyncio
import sys
from unittest.mock import AsyncMock, patch

import checkout_direct as cd


def make_variant(vid, name, available, o1=None, o2=None):
    return {"id": vid, "name": name, "option1": o1, "option2": o2,
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
         patch.object(cd, "poll_variant_stock", return_value=make_variant(27499188, "4y", 3)), \
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
            check_interval=0, max_stock_checks=5,
            preferred_1=["1"], preferred_2=["1"],
            fallback_enabled=True, max_fallback_steps=2)
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

    # ── Test 6: variant_id=None → sentinel ทันที ──
    got = await cd.poll_variant_stock("u", 1, None, "f", check_interval=0, max_checks=5)
    ok = got is not None and got.get("id") is None
    print(("PASS" if ok else f"FAIL (got={got})") + " — Test 6: variant_id=None → sentinel")
    if not ok:
        failures.append("test6")

    # ── Test 7: fallback — poll คืน variant ตัวใหม่ → retry ใช้ตัวใหม่ ──
    api_calls: list[dict] = []

    async def fake_api(**kw):
        api_calls.append(kw)
        return "https://shop.line.me/@x/checkout/cart?id=c2"

    summaries: list[dict] = []
    click_results7 = [cd.PO_SOLD_OUT, cd.PO_PLACED]
    click_calls7 = {"n": 0}

    async def fake_click7(p):
        r = click_results7[min(click_calls7["n"], len(click_results7) - 1)]
        click_calls7["n"] += 1
        return r

    with patch.object(cd, "click_place_order_and_verify", side_effect=fake_click7), \
         patch.object(cd, "confirm_place_order", new=AsyncMock()), \
         patch.object(cd, "poll_variant_stock",
                      return_value=make_variant(999, "5y", 2)), \
         patch.object(cd, "build_checkout_url_via_api", side_effect=fake_api), \
         patch.object(cd, "finalize_checkout", new=AsyncMock(return_value=True)), \
         patch.object(cd, "extract_price_from_page", new=AsyncMock(return_value="350")), \
         patch.object(cd, "print_order_summary",
                      side_effect=lambda **kw: summaries.append(kw)):
        status = await cd.place_order_with_restock_retry(
            page=None, auto_confirm=True,
            product_url="u", product_id=1, product_name="n", shop_handle="@s",
            variant_id=27499188, variant_label="4y", quantity=1,
            session_file="f", encoding_mode="auto", use_api_first=True,
            check_interval=0, max_stock_checks=5,
            preferred_1=["1"], preferred_2=["1"],
            fallback_enabled=True, max_fallback_steps=2)
    used_new = bool(api_calls) and api_calls[0].get("variant_id") == 999
    label_new = bool(summaries) and summaries[-1].get("variant_label") == "5y"
    ok = status == cd.PO_PLACED and used_new and label_new
    print(("PASS" if ok else f"FAIL (status={status}, api={api_calls}, sum={summaries})")
          + " — Test 7: fallback เลื่อน option → checkout ตัวใหม่")
    if not ok:
        failures.append("test7")

    # ── Test 8: poll ระหว่างรอ — ตัวเดิมหมด แต่ option ถัดไปมีของ → เลื่อน ──
    stock_rows = [
        [make_variant(101, "S", 0, o1="sz", o2="S"),
         make_variant(102, "M", 5, o1="sz", o2="M")],
    ]
    with patch.object(cd, "fetch_variants_via_http",
                      side_effect=stock_rows * 10), \
         patch.object(cd.asyncio, "sleep", new=AsyncMock()):
        got = await cd.poll_variant_stock(
            "u", 1, 101, "f", check_interval=0, max_checks=5,
            preferred_1=["1"], preferred_2=["1"],
            fallback_enabled=True, max_fallback_steps=1)
    ok = got is not None and got.get("id") == 102
    print(("PASS" if ok else f"FAIL (got={got})")
          + " — Test 8: fallback on → เลื่อน S → M ระหว่างรอ")
    if not ok:
        failures.append("test8")

    # ── Test 9: เหมือน T8 แต่ fallback off → ไม่เลื่อน รอจนครบ → None ──
    with patch.object(cd, "fetch_variants_via_http",
                      side_effect=stock_rows * 10), \
         patch.object(cd.asyncio, "sleep", new=AsyncMock()):
        got = await cd.poll_variant_stock(
            "u", 1, 101, "f", check_interval=0, max_checks=5,
            preferred_1=["1"], preferred_2=["1"],
            fallback_enabled=False)
    ok = got is None
    print(("PASS" if ok else f"FAIL (got={got})")
          + " — Test 9: fallback off → รอตัวเดิมจนครบ → None")
    if not ok:
        failures.append("test9")

    print()
    if failures:
        print(f"❌ FAILED: {failures}")
        return 1
    print("✅ ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
