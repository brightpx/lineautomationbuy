# LINE Shopping Direct Checkout Script

สคริปต์สำหรับ checkout สินค้าจาก LINE Shopping แบบอัตโนมัติ รองรับ 2 โหมดการทำงาน

---

## 🎯 คุณสมบัติหลัก

### ✅ Product URL Mode (โหมดเดิม)
- รู้ product URL ล่วงหน้า
- เลือก variant ตามความต้องการ (สี/ไซส์)
- ตรวจสอบสต็อกอัตโนมัติ
- รองรับสินค้า 0/1/2 variant options

### 🆕 Shop Monitor Mode (โหมดใหม่)
- **ตรวจจับสินค้าใหม่** ที่โผล่ในร้านอัตโนมัติ
- ใช้กับร้านที่ **ซ่อนสินค้าก่อนเปิดขาย**
- Polling แบบ high-speed (500ms)
- **Prewarm browser** เพื่อลด latency
- Auto-select variant แรกที่มีสต็อก

---

## 📋 ความต้องการของระบบ

```bash
Python 3.10+
playwright
httpx
```

### ติดตั้ง Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## ⚙️ การตั้งค่า

### 1️⃣ สร้าง Session File

รันคำสั่งนี้เพื่อ login LINE ครั้งแรก:

```bash
python bot.py login
```

จะได้ไฟล์ `line_session.json` สำหรับใช้ต่อไป

### 2️⃣ แก้ไข `config.json`

---

## 🔵 Product URL Mode (โหมดเดิม)

### ใช้เมื่อ
- รู้ product URL ล่วงหน้า
- ต้องการเลือก variant เฉพาะ (สี/ไซส์)
- สินค้าเปิดขายแล้ว

### Config ตัวอย่าง

```json
{
    "product_url": "https://shop.line.me/@shop/product/1008199160",
    "session_file": "line_session.json",

    "preferred_1": ["Grey"],
    "preferred_2": ["S"],

    "quantity": 1,
    "checkout_encoding": "auto",

    "headless": false,
    "auto_confirm": false,

    "check_interval_seconds": 1,
    "max_stock_checks": 120
}
```

### การทำงาน

```
1. เปิด product page
2. ดึง variants จาก __NUXT_DATA__
3. หา variant ที่ตรง preferred_1 + preferred_2
4. ตรวจสอบสต็อก (รอถ้าหมด)
5. สร้าง checkout URL
6. Navigate → เลือก PromptPay → Place Order
```

### รัน

```bash
python checkout_direct.py
```

---

## 🆕 Shop Monitor Mode (โหมดใหม่)

### ใช้เมื่อ
- **ไม่รู้ product URL** (ร้านซ่อนสินค้า)
- รู้แค่ร้านและเวลาเปิดขาย
- ต้องการ checkout **ทันทีที่สินค้าปรากฏ**

### Config ตัวอย่าง

```json
{
    "mode": "shop_monitor",

    "shop_url": "https://shop.line.me/@mergeth",
    "sale_start_time": "17:59:30",
    "check_interval_ms": 500,

    "auto_pick_first_product": true,
    "auto_pick_first_variant": true,
    "prewarm_browser": true,

    "product_name_pattern": ".*",

    "session_file": "line_session.json",
    "quantity": 1,
    "checkout_encoding": "auto",
    "headless": false,
    "auto_confirm": true
}
```

### Parameters อธิบาย

| Parameter | คำอธิบาย | ค่าเริ่มต้น |
|-----------|---------|------------|
| `mode` | `"shop_monitor"` เพื่อเปิดโหมดนี้ | - |
| `shop_url` | URL ของร้าน เช่น `https://shop.line.me/@mergeth` | - |
| `sale_start_time` | เวลาเริ่มขาย รูปแบบ `HH:MM:SS` หรือ `HH:MM` | `"00:00:00"` |
| `check_interval_ms` | ระยะเวลา polling (มิลลิวินาที) | `500` |
| `auto_pick_first_product` | เลือกสินค้าใหม่ตัวแรกที่เจอ | `true` |
| `auto_pick_first_variant` | เลือก variant แรกที่มีสต็อกอัตโนมัติ | `true` |
| `prewarm_browser` | เปิด browser ล่วงหน้าก่อนเวลาขาย | `false` |
| `product_name_pattern` | Regex กรองชื่อสินค้า (optional) | `".*"` |
| `auto_confirm` | Place Order โดยไม่ต้องกด Enter | `false` |

### การทำงาน

```
1. โหลด baseline products จากร้าน (ก่อนเปิดขาย)
2. เก็บ product IDs ที่มีอยู่แล้ว
3. รอจนถึงเวลา sale_start_time
4. เริ่ม polling ทุก 500ms
5. เจอ product ใหม่ → ตรวจพบทันที
6. ดึง variants
7. เลือก variant แรกที่มีสต็อก
8. ใช้ prewarmed browser (ถ้าเปิด) หรือเปิดใหม่
9. Checkout → PromptPay → Place Order
```

### รัน

```bash
python checkout_direct.py
```

### ตัวอย่าง Log

```
📡 โหลด baseline products...
📡 Baseline products: 15 รายการ
   Product A, Product B, Product C...
⏰ รอจนถึงเวลาขาย 17:59:30 (อีก 45.2 วินาที)...
🔥 Prewarming browser...
✅ Browser prewarmed
🔍 เริ่ม polling shop...
🆕 ตรวจพบสินค้าใหม่ 1 รายการ
🆕 New product detected:
   id   = 1008243591
   name = New Product Name
   url  = https://shop.line.me/@mergeth/product/1008243591
🚀 Switching to checkout flow
📦 เลือก variant แรก: Grey / S (ID: 12345678)
🔗 Checkout URL: https://...
🌐 ใช้ prewarmed browser
✅ ถึงหน้า checkout
💳 เลือก PromptPay แล้ว
📦 พร้อม Place Order (SHOP MONITOR MODE)
============================================================
สินค้า : New Product Name
ราคา   : ฿599
ตัวเลือก: Grey / S
จำนวน  : 1
ร้าน    : @mergeth
ชำระเงิน: PromptPay
============================================================
🛒 กด Place Order...
✅ ไปหน้า payment/success/order แล้ว
```

---

## 🔧 Config Fields ทั้งหมด

### Common (ทุกโหมด)

| Field | Type | คำอธิบาย |
|-------|------|---------|
| `session_file` | string | ไฟล์ LINE session | 
| `quantity` | int | จำนวนที่ต้องการซื้อ |
| `checkout_encoding` | string | `auto` \| `full` \| `quote_only` \| `none` |
| `headless` | bool | เปิด browser แบบ headless |
| `auto_confirm` | bool | Place Order โดยไม่ถาม |

### Product URL Mode เท่านั้น

| Field | Type | คำอธิบาย |
|-------|------|---------|
| `product_url` | string | URL ของสินค้า |
| `preferred_1` | array | ตัวเลือกแรก (สี/ไซส์) |
| `preferred_2` | array | ตัวเลือกสอง (ไซส์/สี) |
| `check_interval_seconds` | int | ระยะเวลารอตรวจสต็อก |
| `max_stock_checks` | int | จำนวนครั้งตรวจสต็อกสูงสุด |

### Shop Monitor Mode เท่านั้น

| Field | Type | คำอธิบาย |
|-------|------|---------|
| `mode` | string | ต้องเป็น `"shop_monitor"` |
| `shop_url` | string | URL ของร้าน |
| `sale_start_time` | string | เวลาเริ่มขาย `HH:MM:SS` |
| `check_interval_ms` | int | Polling interval (ms) |
| `auto_pick_first_product` | bool | เลือกสินค้าแรกอัตโนมัติ |
| `auto_pick_first_variant` | bool | เลือก variant แรกอัตโนมัติ |
| `prewarm_browser` | bool | เปิด browser ล่วงหน้า |
| `product_name_pattern` | string | Regex filter ชื่อสินค้า |

---

## 🎛️ Checkout Encoding Modes

| Mode | คำอธิบาย | ใช้เมื่อ |
|------|---------|---------|
| `auto` | เลือก encoding ตาม variant<br>- ไม่มี variant → `full`<br>- มี variant → `quote_only` | แนะนำ (default) |
| `full` | Encode ทุกตัวอักษรพิเศษ<br>`%7B%22items%22%3A...` | สินค้าไม่มี variant |
| `quote_only` | Encode เฉพาะ `"`<br>`{%22items%22:[...]}` | สินค้ามี variant |
| `none` | Raw JSON ไม่ encode<br>`{"items":[...]}` | ทดสอบเท่านั้น |

---

## 🔀 Variant Matching Priority

สำหรับ Product URL Mode ที่มี `preferred_1` และ `preferred_2`:

```
1. ✅ option1 + option2 match ทั้งคู่
2. ✅ name match ทั้ง pref1 และ pref2
3. ✅ option1 หรือ option2 match pref1 only
4. ✅ option1 หรือ option2 match pref2 only
5. ✅ name match pref1 หรือ pref2 อย่างใดอย่างหนึ่ง
```

---

## 🚨 Error Handling

### Product URL Mode
- ไม่พบ variant ที่ตรง → แสดง variants ที่มีและหยุด
- สต็อกหมด → รอตรวจซ้ำทุก `check_interval_seconds`
- เกิน `max_stock_checks` → หยุด

### Shop Monitor Mode
- Poll error → log warning แล้วลองใหม่
- ไม่พบสินค้าใหม่ → polling ต่อ
- เจอหลายสินค้าใหม่ → เลือกตัวแรกหรือกรองด้วย `product_name_pattern`

---

## 📂 ไฟล์ที่สร้างขึ้น

| ไฟล์ | คำอธิบาย |
|------|---------|
| `line_session.json` | LINE login session |
| `debug_checkout_price.html` | HTML dump สำหรับตรวจสอบราคา |
| `debug_place_order_failed.png` | Screenshot เมื่อ Place Order ล้มเหลว |
| `debug_payment_page.png` | Screenshot หน้า payment |
| `debug_place_order_error.png` | Screenshot เมื่อเกิด error |

---

## 🔄 สลับโหมด

### Product URL → Shop Monitor

เปลี่ยนจาก:
```json
{
    "product_url": "..."
}
```

เป็น:
```json
{
    "mode": "shop_monitor",
    "shop_url": "...",
    "sale_start_time": "18:00:00"
}
```

### Shop Monitor → Product URL

ลบหรือ comment `"mode": "shop_monitor"` ออก:
```json
{
    // "mode": "shop_monitor",
    "product_url": "..."
}
```

---

## 💡 Tips & Best Practices

### Product URL Mode
- ใช้ `headless: false` เพื่อดูการทำงาน
- ตั้ง `max_stock_checks` ให้เหมาะสมกับเวลารอสูงสุด
- ระบุ `preferred_1` และ `preferred_2` ตรงกับชื่อใน LINE Shopping

### Shop Monitor Mode
- ใช้ `prewarm_browser: true` เพื่อลด latency
- ตั้ง `check_interval_ms` ต่ำ (300-500ms) สำหรับสินค้า high-demand
- ตั้ง `sale_start_time` ก่อนเวลาจริง 30-60 วินาที
- ใช้ `product_name_pattern` เพื่อกรองสินค้าที่ต้องการ
- ตั้ง `auto_confirm: true` เพื่อความเร็วสูงสุด

---

## ⚠️ ข้อควรระวัง

1. **Rate Limiting**: LINE Shopping อาจบล็อก IP ถ้า polling เร็วเกินไป
2. **Session Expiry**: ต้อง login ใหม่ด้วย `python bot.py login` เป็นระยะ
3. **Prewarm Browser**: ใช้ RAM มากขึ้น แต่เร็วกว่า
4. **Headless Mode**: ใช้กับ production เท่านั้น (ดูไม่เห็น browser)

---

## 🐛 Troubleshooting

### ❌ "ไม่พบ __NUXT_DATA__"
- ลอง refresh หน้า product
- ตรวจสอบว่า login session ยังใช้งานได้

### ❌ "เลือก PromptPay ไม่สำเร็จ"
- ตรวจสอบว่ามี PromptPay ในบัญชี LINE
- ลอง `headless: false` เพื่อดูหน้า checkout

### ❌ "URL ไม่เปลี่ยน หลัง Place Order"
- ตรวจสอบ screenshot `debug_place_order_failed.png`
- อาจมี popup หรือ error ที่ต้องปิด

### ❌ Shop Monitor ไม่เจอสินค้าใหม่
- ตรวจสอบ `shop_url` ถูกต้อง
- ลด `check_interval_ms` ลงเหลือ 300-500ms
- ตรวจสอบ baseline products มีอะไรบ้าง

---

## 📜 License

MIT License

---

## 🙏 Credits

Built with:
- [Playwright](https://playwright.dev/) - Browser automation
- [HTTPX](https://www.python-httpx.org/) - HTTP client

---

**⚡ Happy Shopping! 🛍️**
