"""
bg_capture.py — показывает overlay_tools.html через HTTP,
принимает состояние от браузера пользователя через POST /state,
делает скриншот с актуальным состоянием → bg.jpg
"""
import asyncio, threading, time, os, json
from http.server import HTTPServer, BaseHTTPRequestHandler, SimpleHTTPRequestHandler
from playwright.async_api import async_playwright

PORT     = 5010
HTML_DIR = "/teamspace/studios/this_studio"
BG_PATH  = "/teamspace/studios/this_studio/bg.jpg"
WIDTH, HEIGHT = 1920, 1080
INTERVAL = 1.5

_state = {}  # последнее состояние от пользователя
_state_lock = threading.Lock()
_state_updated = threading.Event()

# ── HTTP сервер: раздаёт HTML + принимает /state ───────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/overlay_tools.html' or path == '/':
            fpath = os.path.join(HTML_DIR, 'overlay_tools.html')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            with open(fpath, 'rb') as f:
                self.wfile.write(f.read())
        elif path == '/state':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            with _state_lock:
                self.wfile.write(json.dumps(_state).encode())
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == '/state':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                with _state_lock:
                    _state.update(data)
                _state_updated.set()
                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(b'ok')
            except Exception as e:
                self.send_response(400); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

def run_http():
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

threading.Thread(target=run_http, daemon=True).start()
print(f"[bg_capture] HTTP: http://localhost:{PORT}/overlay_tools.html", flush=True)

# ── Playwright скриншот-демон ──────────────────────────────────
async def capture_loop():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        await page.goto(f"http://localhost:{PORT}/overlay_tools.html", wait_until="networkidle")
        print(f"[bg_capture] страница загружена", flush=True)
        while True:
            t0 = time.time()
            # Инжектируем актуальное состояние в playwright-страницу
            with _state_lock:
                state_copy = dict(_state)
            if state_copy:
                await page.evaluate(f"window.__applyState && window.__applyState({json.dumps(state_copy)})")
            await page.screenshot(path=BG_PATH, type="jpeg", quality=90, full_page=False)
            elapsed = time.time() - t0
            print(f"[bg_capture] скриншот {elapsed*1000:.0f}ms", flush=True)
            await asyncio.sleep(max(0, INTERVAL - elapsed))

asyncio.run(capture_loop())
