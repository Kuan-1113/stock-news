import sys
print("=== main.py started ===", flush=True)

import os
print(f"CWD: {os.getcwd()}", flush=True)
print(f"FILES: {os.listdir('.')}", flush=True)
print(f"PORT: {os.environ.get('PORT', 'NOT SET')}", flush=True)
print(f"DISCORD_BOT_TOKEN set: {bool(os.environ.get('DISCORD_BOT_TOKEN'))}", flush=True)
print(f"ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))}", flush=True)

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a):
        pass

def _start_health():
    port = int(os.environ.get("PORT", 8080))
    print(f"Health server on port {port}", flush=True)
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()

threading.Thread(target=_start_health, daemon=True).start()

bot_path = "自選股/discord_bot.py"
print(f"Bot file exists: {os.path.exists(bot_path)}", flush=True)

import runpy
print("Launching bot...", flush=True)
runpy.run_path(bot_path, run_name="__main__")
