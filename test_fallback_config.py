"""
ทดสอบ Auto-Fallback ตามตำแหน่ง (mock ทั้งหมด ไม่ยิงเว็บจริง):
1. fallback_enabled=True, steps=0 → เลื่อนไม่จำกัด
2. fallback_enabled=False → ไม่เลื่อนเลย (ได้ตัวหมดสต็อกตัวแรก)
3. max_fallback_steps=1 → เลื่อนได้แค่ 1 ตำแหน่ง
4. max_fallback_steps=2 → เลื่อนได้ 2 ตำแหน่ง
5. fallback ปิด + มีสต็อกตำแหน่งที่ระบุ → ใช้ตำแหน่งนั้นปกติ
6. preferred เป็นชื่อ option (ไม่ใช่ตัวเลข) → fallback config ไม่กระทบ

Run: & .venv\\Scripts\\python.exe test_fallback_config.py
"""
import sys

from checkout_direct import find_matching_variant_with_fallback


def v(vid, name, opt1, opt2=None, avail=5):
    return {"id": vid, "name": name, "option1": opt1, "option2": opt2,
            "available": avail}


# สินค้า 4 ไซซ์: "S" หมด, "M" มี, "L" มี, "XL" มี
VARIANTS = [
    v(101, "Red-S", "Red", "S", avail=0),
    v(102, "Red-M", "Red", "M", avail=3),
    v(103, "Red-L", "Red", "L", avail=3),
    v(104, "Red-XL", "Red", "XL", avail=3),
]

failures = []


def check(label, got, want):
    ok = got == want
    print(("PASS" if ok else f"FAIL (got={got}, want={want})") + f" — {label}")
    if not ok:
        failures.append(label)


# ── Test 1: default (enabled, unlimited) — pref2="1"(S หมด) เลื่อนถึง M ──
r = find_matching_variant_with_fallback(VARIANTS, ["1"], ["1"])
check("T1 enabled+unlimited: S หมด → เลื่อนไป M", r["name"], "Red-M")

# ── Test 2: fallback_enabled=False — ไม่เลื่อน ได้ S (หมด) กลับไป ──
r = find_matching_variant_with_fallback(VARIANTS, ["1"], ["1"],
                                        fallback_enabled=False)
check("T2 disabled: คืน S (หมด) ให้ caller รอเติม", r["name"], "Red-S")
check("T2 disabled: available == 0", r["available"], 0)

# ── Test 3: max_fallback_steps=1 — เลื่อนได้แค่ [1]→[2] ──
r = find_matching_variant_with_fallback(VARIANTS, ["1"], ["1"],
                                        max_fallback_steps=1)
check("T3 steps=1: S หมด → เลื่อนได้แค่ M", r["name"], "Red-M")

# ── Test 4: max_fallback_steps=1 แต่ M ก็หมดด้วย → ไม่ไป L ได้ ──
V2 = [
    v(201, "Red-S", "Red", "S", avail=0),
    v(202, "Red-M", "Red", "M", avail=0),
    v(203, "Red-L", "Red", "L", avail=3),
]
r = find_matching_variant_with_fallback(V2, ["1"], ["1"], max_fallback_steps=1)
check("T4 steps=1: S,M หมดทั้งคู่ → คืน S (หมด) ไม่ข้ามไป L",
      r["name"], "Red-S")

# ── Test 5: steps=2 — S,M หมด → เลื่อนได้ถึง L ──
r = find_matching_variant_with_fallback(V2, ["1"], ["1"], max_fallback_steps=2)
check("T5 steps=2: S,M หมด → เลื่อนไป L", r["name"], "Red-L")

# ── Test 6: fallback ปิด + ตำแหน่งที่ระบุมีสต็อก → ใช้ปกติ ──
r = find_matching_variant_with_fallback(VARIANTS, ["1"], ["2"],
                                        fallback_enabled=False)
check("T6 disabled+มีสต็อก: ได้ M ตามตำแหน่ง 2", r["name"], "Red-M")

# ── Test 7: preferred เป็นชื่อ (ไม่ใช่ตัวเลข) — config ไม่กระทบ ──
r = find_matching_variant_with_fallback(VARIANTS, ["red"], ["m"],
                                        fallback_enabled=False,
                                        max_fallback_steps=1)
check("T7 ชื่อ option: match 'red'+'m' ได้ M", r["name"], "Red-M")

# ── Test 8: steps เกินจำนวน option ที่มี → เลื่อนถึงตัวท้ายที่มีสต็อก ──
V3 = [
    v(301, "Red-S", "Red", "S", avail=0),
    v(302, "Red-M", "Red", "M", avail=0),
    v(303, "Red-L", "Red", "L", avail=0),
    v(304, "Red-XL", "Red", "XL", avail=3),
]
r = find_matching_variant_with_fallback(V3, ["1"], ["1"], max_fallback_steps=99)
check("T8 steps=99: S,M,L หมด → เลื่อนถึง XL", r["name"], "Red-XL")

print()
if failures:
    print(f"❌ FAILED: {failures}")
    sys.exit(1)
print("✅ ALL TESTS PASSED")
