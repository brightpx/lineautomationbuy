"""
LINE Shopping Bot — Webapp (front+back รวมกัน)
------------------------------------------------
รัน:  python webapp.py
เปิด: http://127.0.0.1:8000

- Backend: FastAPI (serve API + static frontend ใน process เดียว)
- Frontend: static/index.html (no build step)
- Live: WebSocket /ws ส่ง log + status, รูป preview ผ่าน /api/preview (auto-refresh)
- Bot engine: reuse checkout_direct.run() ทั้ง Product URL และ Shop Monitor mode
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import checkout_direct as engine

# Windows console (cp874) เขียน emoji ไม่ได้ → บังคับ UTF-8 + replace กันบอทตายกลางทาง
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_FILE = BASE_DIR / "config.json"
DEBUG_DIR = BASE_DIR / "debug"

log = logging.getLogger("webapp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

# ---------------- state ----------------
LOGS: deque[dict] = deque(maxlen=800)
_log_seq = itertools.count(1)  # id กัน log ซ้ำฝั่ง frontend
bot_task: Optional[asyncio.Task] = None
bot_state: dict[str, Any] = {
    "running": False,
    "mode": "idle",          # idle | product | shop_monitor
    "started_at": None,
    "detail": "",
    "finish_note": "",
}

# ---- รอยืนยัน Place Order บนเว็บ (กรณีไม่ติ๊ก auto_confirm) ----
pending_confirm: dict | None = None
_confirm_event: asyncio.Event | None = None
_confirm_result: bool = False


async def _web_confirm_hook(summary: dict) -> bool:
    """hook ให้ engine หยุดรอปุ่มบนเว็บแทน input() ที่ terminal"""
    global pending_confirm, _confirm_event, _confirm_result
    _confirm_event = asyncio.Event()
    _confirm_result = False
    pending_confirm = {**summary, "since": datetime.now().strftime("%H:%M:%S")}
    push_sys("⏳ รอยืนยัน Place Order — กดปุ่มบนเว็บ")
    try:
        await _confirm_event.wait()
    except asyncio.CancelledError:
        pending_confirm = None
        _confirm_event = None
        raise
    pending_confirm = None
    _confirm_event = None
    return _confirm_result


engine.WEB_CONFIRM_HOOK = _web_confirm_hook

# ---- LINE login session (เปิด browser ให้ login ด้วยมือผ่านหน้าเว็บ) ----
login_state: dict[str, Any] = {"active": False, "started_at": None, "session_file": "line_session.json"}
_login_pw = None
_login_browser = None
_login_context = None


async def _close_login_browser() -> None:
    global _login_pw, _login_browser, _login_context
    for obj, meth in ((_login_browser, "close"), (_login_pw, "stop")):
        if obj is not None:
            try:
                await getattr(obj, meth)()
            except Exception:
                pass
    _login_pw = _login_browser = _login_context = None
    login_state.update({"active": False, "started_at": None})


def session_info() -> dict[str, Any]:
    cfg = load_config_raw()
    sf = BASE_DIR / cfg.get("session_file", "line_session.json")
    if not sf.exists():
        return {"has_session": False, "session_file": sf.name, "mtime": None}
    return {"has_session": True, "session_file": sf.name,
            "mtime": time.strftime("%H:%M:%S %d/%m", time.localtime(sf.stat().st_mtime)),
            "size": sf.stat().st_size}


class WebLogHandler(logging.Handler):
    """ดัก log จาก engine + webapp เก็บลง deque ให้ WS/REST ดึง (seq เดียว ไม่ซ้ำ)"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOGS.append({
                "seq": next(_log_seq),
                "t": datetime.now().strftime("%H:%M:%S"),
                "level": record.levelname,
                "name": record.name,
                "msg": record.getMessage(),
            })
        except Exception:
            pass


# ผูก handler เข้ากับ logger ของ engine เพื่อให้เห็นความเคลื่อนไหวบอทสดๆ
_web_handler = WebLogHandler(level=logging.INFO)
for _lname in ("checkout_direct", "linebot", "webapp"):
    _lg = logging.getLogger(_lname)
    _lg.addHandler(_web_handler)
    _lg.setLevel(logging.INFO)
    _lg.propagate = False
_root = logging.getLogger()
_root.addHandler(_web_handler)


def push_sys(msg: str, level: str = "INFO") -> None:
    # อย่า append ตรงๆ — ปล่อยให้ handler แปะ seq ทีเดียว (กันเบิ้ล)
    (log.info if level == "INFO" else log.warning)(msg)


def load_config_raw() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def build_run_config(mode: str, overrides: dict | None = None) -> dict:
    """ประกอบ config ที่จะส่งให้ engine.run() — logic เดียวกับ checkout_direct.main()"""
    base = load_config_raw()
    overrides = overrides or {}
    if mode == "shop_monitor":
        mon = base.get("_shop_monitor_config") or {}
        merged = {**base, **mon}
        merged["mode"] = "shop_monitor"
        merged.pop("_shop_monitor_config", None)
    else:
        merged = dict(base)
        merged.pop("mode", None)
        merged.pop("_shop_monitor_config", None)
    # override จาก UI (headless/auto_confirm/quantity/urls/...)
    for k, v in overrides.items():
        if v is not None:
            merged[k] = v
    return merged


async def _run_bot_coro(cfg: dict, mode_label: str) -> None:
    bot_state.update({"running": True, "mode": mode_label,
                      "started_at": datetime.now().strftime("%H:%M:%S"),
                      "finish_note": "", "detail": cfg.get("product_url") or cfg.get("shop_url") or ""})
    push_sys(f"▶ เริ่มบอทโหมด {mode_label}: {bot_state['detail']}")
    try:
        await engine.run(cfg)
        bot_state["finish_note"] = "จบ flow ปกติ"
        push_sys("■ บอทจบ flow ปกติ")
    except asyncio.CancelledError:
        bot_state["finish_note"] = "ถูกสั่งหยุด"
        push_sys("■ บอทถูกสั่งหยุด", "WARNING")
        raise
    except Exception as e:  # noqa: BLE001
        bot_state["finish_note"] = f"error: {e}"
        push_sys(f"❌ บอท error: {e}", "WARNING")
    finally:
        bot_state.update({"running": False, "mode": "idle", "started_at": None})
        global bot_task
        bot_task = None


def latest_screenshot() -> Optional[Path]:
    """หารูป preview ล่าสุด: debug/*.png ก่อน แล้วค่อย *.png หน้า root"""
    cands: list[Path] = []
    if DEBUG_DIR.exists():
        cands += list(DEBUG_DIR.glob("*.png"))
    cands += [p for p in BASE_DIR.glob("debug_*.png")]
    cands = [p for p in cands if p.is_file()]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


# ---------------- app ----------------
app = FastAPI(title="LINE Buy Bot Webapp")


@app.get("/")
async def index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        return JSONResponse({"error": "ไม่พบ static/index.html"}, status_code=500)
    return FileResponse(str(idx))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/api/status")
async def api_status():
    return {
        **bot_state,
        "log_count": len(LOGS),
        "preview": (lambda p: {"file": p.name, "mtime": time.strftime(
            "%H:%M:%S", time.localtime(p.stat().st_mtime))} if p else None)(latest_screenshot()),
        **session_info(),
        "login": dict(login_state),
        "confirm": pending_confirm,
    }


@app.get("/api/login/status")
async def api_login_status():
    return {"login": dict(login_state), **session_info()}


@app.post("/api/login/start")
async def api_login_start(payload: dict | None = None):
    """เปิด browser จริง (headful) บนเครื่อง server ให้ user login LINE ด้วยมือ"""
    global _login_pw, _login_browser, _login_context
    if login_state["active"]:
        return JSONResponse({"ok": False, "error": "หน้าต่าง login เปิดอยู่แล้ว"}, status_code=409)
    session_file = (payload or {}).get("session_file") or load_config_raw().get("session_file", "line_session.json")
    try:
        from playwright.async_api import async_playwright
        _login_pw = await async_playwright().start()
        _login_browser = await _login_pw.chromium.launch(headless=False)
        _login_context = await _login_browser.new_context()
        page = await _login_context.new_page()
        await page.goto("https://shop.line.me/", wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:  # noqa: BLE001
        await _close_login_browser()
        push_sys(f"❌ เปิดหน้าต่าง login ไม่ได้: {e}", "WARNING")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    login_state.update({"active": True, "started_at": datetime.now().strftime("%H:%M:%S"),
                        "session_file": Path(session_file).name})
    push_sys("🔑 เปิดหน้าต่าง login แล้ว — login ใน browser ที่เด้งขึ้นมา เสร็จแล้วกด 'บันทึก session'")
    return {"ok": True, **login_state}


@app.post("/api/login/finish")
async def api_login_finish():
    """บันทึก storage_state จาก browser ที่เปิดไว้ลง session file แล้วปิด"""
    global _login_context
    if not login_state["active"] or _login_context is None:
        return JSONResponse({"ok": False, "error": "ยังไม่ได้เปิดหน้าต่าง login"}, status_code=404)
    try:
        storage = await _login_context.storage_state()
        sf = BASE_DIR / login_state.get("session_file", "line_session.json")
        sf.write_text(json.dumps(storage), encoding="utf-8")
        push_sys(f"✅ บันทึก session ลง {sf.name} แล้ว")
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        await _close_login_browser()
    return {"ok": True, **session_info()}


@app.post("/api/login/cancel")
async def api_login_cancel():
    if not login_state["active"]:
        return {"ok": True, "cancelled": False}
    await _close_login_browser()
    push_sys("ยกเลิกหน้าต่าง login", "WARNING")
    return {"ok": True, "cancelled": True}


@app.get("/api/confirm/status")
async def api_confirm_status():
    return {"pending": pending_confirm is not None, "detail": pending_confirm}


@app.post("/api/confirm")
async def api_confirm(payload: dict):
    """กดยืนยัน/ยกเลิก Place Order จากหน้าเว็บ"""
    global _confirm_result
    if pending_confirm is None or _confirm_event is None:
        return JSONResponse({"ok": False, "error": "ไม่มีรายการรอ confirm"}, status_code=404)
    _confirm_result = bool(payload.get("confirm", True))
    push_sys("✅ ยืนยัน Place Order บนเว็บ" if _confirm_result else "✕ ยกเลิก Place Order บนเว็บ",
             "INFO" if _confirm_result else "WARNING")
    _confirm_event.set()
    return {"ok": True, "confirm": _confirm_result}


@app.get("/api/config")
async def api_get_config():
    return load_config_raw()


@app.post("/api/config")
async def api_save_config(payload: dict):
    CONFIG_FILE.write_text(json.dumps(payload, indent=4, ensure_ascii=False), encoding="utf-8")
    push_sys("💾 บันทึก config.json แล้ว")
    return {"ok": True}


@app.post("/api/bot/start")
async def api_bot_start(payload: dict):
    global bot_task
    if bot_task and not bot_task.done():
        return JSONResponse({"ok": False, "error": "บอทกำลังรันอยู่"}, status_code=409)
    mode = str(payload.get("mode", "product")).lower()  # product | shop_monitor
    overrides = {k: payload.get(k) for k in (
        "product_url", "shop_url", "quantity", "headless",
        "auto_confirm", "preferred_1", "preferred_2",
        "check_interval_seconds", "sale_start_time", "check_interval_ms",
    ) if payload.get(k) is not None}
    # normalize preferred_* ให้เป็น list เสมอ (รับทั้ง "1" และ ["1"])
    for k in ("preferred_1", "preferred_2"):
        if isinstance(overrides.get(k), str):
            overrides[k] = [s.strip() for s in overrides[k].split(",") if s.strip()]
    cfg = build_run_config("shop_monitor" if mode == "shop_monitor" else "product", overrides)
    # webapp ไม่มีจอเสมอ → บังคับ headless ตามค่าที่ส่งมา (default True)
    if "headless" not in overrides:
        cfg["headless"] = True
    bot_task = asyncio.create_task(_run_bot_coro(cfg, "shop_monitor" if mode == "shop_monitor" else "product"))
    return {"ok": True, "mode": bot_state.get("mode")}


@app.post("/api/bot/stop")
async def api_bot_stop():
    global bot_task
    if not bot_task or bot_task.done():
        return {"ok": True, "stopped": False}
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass
    return {"ok": True, "stopped": True}


@app.get("/api/logs")
async def api_logs(limit: int = 200):
    return {"logs": list(LOGS)[-limit:]}


@app.get("/api/screenshots")
async def api_screenshots():
    files = []
    seen: set[str] = set()
    for d in (DEBUG_DIR, BASE_DIR):
        if not d.exists():
            continue
        pat = "*.png" if d == DEBUG_DIR else "debug_*.png"
        for p in d.glob(pat):
            if p.name in seen or not p.is_file():
                continue
            seen.add(p.name)
            st = p.stat()
            files.append({"file": p.name, "dir": "debug" if d == DEBUG_DIR else "root",
                          "size": st.st_size, "mtime": st.st_mtime,
                          "mtime_str": time.strftime("%H:%M:%S %d/%m", time.localtime(st.st_mtime))})
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return {"files": files[:40]}


@app.get("/api/preview")
async def api_preview(file: str | None = None):
    """รูป preview ล่าสุด (หรือระบุ ?file=ชื่อไฟล์เพื่อ pin) — frontend poll ทุก ~1.5s"""
    if file:
        for d in (DEBUG_DIR, BASE_DIR):
            p = d / Path(file).name
            if p.is_file():
                return FileResponse(str(p), media_type="image/png")
        return JSONResponse({"error": "ไม่พบไฟล์"}, status_code=404)
    p = latest_screenshot()
    if not p:
        return JSONResponse({"error": "ยังไม่มี screenshot — เริ่มบอทก่อน"}, status_code=404)
    return FileResponse(str(p), media_type="image/png")


@app.get("/api/preview-info")
async def api_preview_info():
    p = latest_screenshot()
    if not p:
        return {"file": None}
    st = p.stat()
    return {"file": p.name, "mtime": st.st_mtime,
            "mtime_str": time.strftime("%H:%M:%S", time.localtime(st.st_mtime)), "size": st.st_size}


@app.websocket("/ws")
async def ws_stream(ws: WebSocket):
    await ws.accept()
    push_sys("🔌 client เชื่อม WS")
    # snapshot แรกแค่ 200 แถวล่าสุด (กันย้อนทั้งกอง 800) แล้วส่งเฉพาะ seq ใหม่
    buf = list(LOGS)[-200:]
    last_seq = buf[-1]["seq"] if buf else 0
    try:
        if buf:
            await ws.send_json({"type": "logs", "logs": buf})
        while True:
            fresh = [l for l in LOGS if l["seq"] > last_seq]
            if fresh:
                await ws.send_json({"type": "logs", "logs": fresh})
                last_seq = fresh[-1]["seq"]
            # ส่ง status + preview info ทุก tick
            p = latest_screenshot()
            await ws.send_json({"type": "status", "status": {
                **bot_state,
                "preview": p.name if p else None,
                "preview_mtime": p.stat().st_mtime if p else None,
                "server_time": datetime.now().strftime("%H:%M:%S"),
                "login": dict(login_state),
                "confirm": pending_confirm,
                **session_info(),
            }})
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    STATIC_DIR.mkdir(exist_ok=True)
    DEBUG_DIR.mkdir(exist_ok=True)
    print("=" * 60)
    print("  LINE Buy Bot Webapp -> http://127.0.0.1:8000")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
