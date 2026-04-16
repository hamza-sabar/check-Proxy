from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
import asyncio
import aiohttp
from aiohttp_socks import ProxyConnector
import time
import json
import os

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ================= CONFIG =================
TIMEOUT = 10
MAX_CONCURRENT = 300
RETRIES = 2
TEST_URL = "http://httpbin.org/ip"

semaphore = asyncio.Semaphore(MAX_CONCURRENT)
RESULTS = []
RUNNING = False
PAUSED = False

# ================= UTILS =================
def load_proxies_from_text(text):
    return list(set([line.strip() for line in text.splitlines() if line.strip()]))

# ================= PROXY CHECKER =================
async def check_one(proxy):
    global PAUSED
    async with semaphore:
        proxy_types = [
            ("http", f"http://{proxy}"),
            ("socks5", f"socks5://{proxy}")
        ]

        for proxy_type, proxy_url in proxy_types:
            for _ in range(RETRIES):
                try:
                    while PAUSED:  # ⚡ pause côté serveur
                        await asyncio.sleep(0.5)

                    start = time.time()
                    timeout = aiohttp.ClientTimeout(total=TIMEOUT)

                    if proxy_type == "socks5":
                        connector = ProxyConnector.from_url(proxy_url)
                        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                            async with session.get(TEST_URL) as response:
                                if response.status == 200:
                                    data = await response.text()
                                    ip = json.loads(data).get("origin", "Unknown")
                                    elapsed = round((time.time() - start) * 1000, 2)
                                    return True, proxy, f"{elapsed} ms | IP: {ip}"

                    else:
                        async with aiohttp.ClientSession(timeout=timeout) as session:
                            async with session.get(TEST_URL, proxy=proxy_url) as response:
                                if response.status == 200:
                                    data = await response.text()
                                    ip = json.loads(data).get("origin", "Unknown")
                                    elapsed = round((time.time() - start) * 1000, 2)
                                    return True, proxy, f"{elapsed} ms | IP: {ip}"

                except Exception:
                    continue

        return False, proxy, "Dead"

# ================= RUNNER =================
async def run_checker(proxies):
    global RESULTS, RUNNING
    RESULTS = []
    RUNNING = True

    tasks = [check_one(p) for p in proxies]

    for i, task in enumerate(asyncio.as_completed(tasks), 1):
        status, proxy, msg = await task
        RESULTS.append({
            "proxy": proxy,
            "status": "OK" if status else "DEAD",
            "info": msg,
            "progress": f"{i}/{len(proxies)}"
        })

    # Save working proxies
    working = [r["proxy"] for r in RESULTS if r["status"] == "OK"]
    with open("working_async.txt", "w") as f:
        f.write("\n".join(working))

    RUNNING = False

# ================= ROUTES =================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "results": RESULTS,
        "running": RUNNING
    })

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    content = await file.read()
    proxies = load_proxies_from_text(content.decode(errors="ignore"))
    asyncio.create_task(run_checker(proxies))
    return {"message": f"Scan démarré ({len(proxies)} proxies)"}

@app.get("/results")
async def get_results():
    return {"running": RUNNING, "results": RESULTS, "paused": PAUSED}

@app.post("/toggle_pause")
async def toggle_pause():
    global PAUSED
    PAUSED = not PAUSED
    return {"paused": PAUSED}

@app.get("/download")
async def download():
    if os.path.exists("working_async.txt"):
        return FileResponse("working_async.txt", filename="working_async.txt")
    return {"error": "File not found"}