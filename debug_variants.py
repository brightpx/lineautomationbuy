"""
Diagnostic script — dump all JSON API responses + raw __NUXT__ state
Run once to understand the data structure of LINE Shopping product page.
"""
import asyncio
import json
import logging
from pathlib import Path
from playwright.async_api import async_playwright, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("debug_variants")

CONFIG_FILE = "config.json"
DUMP_FILE   = "debug_variants_dump.json"
PRODUCT_ID  = 1008243591  # from config


async def main():
    config  = json.loads(Path(CONFIG_FILE).read_text(encoding="utf-8"))
    url     = config["product_url"]
    session = config.get("session_file", "line_session.json")

    api_calls: list[dict] = []

    async with async_playwright() as pw:
        # NO extra browser flags — let all network through
        browser = await pw.chromium.launch(headless=False)
        ctx     = await browser.new_context(
            storage_state=session if Path(session).exists() else None
        )
        page    = await ctx.new_page()

        # ── capture every JSON response ──
        async def on_resp(resp: Response):
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            if resp.status not in (200, 201):
                return
            if any(x in resp.url for x in (".js", ".css", ".png", ".ico")):
                return
            try:
                body = await resp.json()
                entry = {"url": resp.url, "status": resp.status, "body": body}
                api_calls.append(entry)
                log.info("📡 %s", resp.url[:120])
            except Exception:
                pass

        page.on("response", on_resp)

        log.info("เปิด %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)

        log.info("รอ networkidle...")
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            await page.wait_for_timeout(5_000)

        # ── dump window.__NUXT__ ──
        log.info("dump window.__NUXT__...")
        nuxt_raw = await page.evaluate("""
            () => {
                const n = window.__NUXT__ || window.__nuxt__;
                if (!n) return null;
                try { return JSON.stringify(n); } catch(e) { return String(e); }
            }
        """)

        # ── dump __NUXT_DATA__ script tag ──
        log.info("dump __NUXT_DATA__ script tag...")
        nuxt_data_raw = await page.evaluate("""
            () => {
                const el = document.getElementById('__NUXT_DATA__')
                    || document.querySelector('script[type="application/json"]');
                return el ? el.textContent : null;
            }
        """)

        # ── dump all script tags that mention product/variant ──
        log.info("dump inline scripts...")
        scripts = await page.evaluate(f"""
            () => Array.from(document.querySelectorAll('script:not([src])'))
                .map(s => ({{id: s.id, type: s.type, len: s.textContent.length,
                             hasVariant: s.textContent.includes('ariant'),
                             hasProduct: s.textContent.includes('{PRODUCT_ID}'),
                             preview: s.textContent.slice(0, 300) }}))
                .filter(s => s.hasVariant || s.hasProduct)
        """)

        # ── write dump ──
        dump = {
            "api_calls": api_calls,
            "nuxt_raw_length": len(nuxt_raw) if nuxt_raw else 0,
            "nuxt_data_raw_length": len(nuxt_data_raw) if nuxt_data_raw else 0,
            "nuxt_raw_preview": nuxt_raw[:2000] if nuxt_raw else None,
            "nuxt_data_raw_preview": nuxt_data_raw[:2000] if nuxt_data_raw else None,
            "nuxt_raw_full": json.loads(nuxt_raw) if nuxt_raw and nuxt_raw.startswith("{") else nuxt_raw,
            "nuxt_data_raw_full": json.loads(nuxt_data_raw) if nuxt_data_raw else None,
            "inline_scripts_meta": scripts,
        }
        Path(DUMP_FILE).write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")

        log.info("─" * 60)
        log.info("📦 API calls intercepted : %d", len(api_calls))
        log.info("📦 window.__NUXT__ size  : %d chars", len(nuxt_raw) if nuxt_raw else 0)
        log.info("📦 __NUXT_DATA__ size    : %d chars", len(nuxt_data_raw) if nuxt_data_raw else 0)
        log.info("📦 inline scripts matched: %d", len(scripts))
        log.info("✅ dump saved → %s", DUMP_FILE)
        log.info("─" * 60)

        if api_calls:
            log.info("API URLs:")
            for c in api_calls:
                log.info("  [%d] %s", c["status"], c["url"][:120])

        await browser.close()

asyncio.run(main())
