from fastapi import FastAPI, UploadFile, File, Request, Header
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import asyncio
import aiohttp
from aiohttp_socks import ProxyConnector
import time
import json
import io
from typing import Optional

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ================= CONFIG =================
TIMEOUT = 10
MAX_CONCURRENT = 100
RETRIES = 2
TEST_URL = "http://httpbin.org/ip"

# ================= SESSION STORE =================
SESSIONS: dict = {}

def get_session(session_id: str) -> dict:
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {
            "results": [],
            "running": False,
            "paused": False,
            "cancelled": False,
            "semaphore": asyncio.Semaphore(MAX_CONCURRENT),
            "active_tasks": [],
            "total": 0,
        }
    return SESSIONS[session_id]

# ================= UTILS =================
def load_proxies_from_text(text: str) -> list:
    return list(set([line.strip() for line in text.splitlines() if line.strip()]))

# ================= PROXY CHECKER =================
async def check_one(proxy: str, session: dict) -> tuple:
    async with session["semaphore"]:
        proxy_types = [
            ("http", f"http://{proxy}"),
            ("socks5", f"socks5://{proxy}"),
        ]
        for proxy_type, proxy_url in proxy_types:
            for _ in range(RETRIES):
                try:
                    if session["cancelled"]:
                        return False, proxy, "Cancelled"

                    while session["paused"]:
                        if session["cancelled"]:
                            return False, proxy, "Cancelled"
                        await asyncio.sleep(0.3)

                    start = time.time()
                    timeout = aiohttp.ClientTimeout(total=TIMEOUT)

                    if proxy_type == "socks5":
                        connector = ProxyConnector.from_url(proxy_url)
                        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
                            async with s.get(TEST_URL) as response:
                                if response.status == 200:
                                    data = await response.text()
                                    ip = json.loads(data).get("origin", "Unknown")
                                    elapsed = round((time.time() - start) * 1000, 2)
                                    return True, proxy, f"{elapsed} ms | IP: {ip}"
                    else:
                        async with aiohttp.ClientSession(timeout=timeout) as s:
                            async with s.get(TEST_URL, proxy=proxy_url) as response:
                                if response.status == 200:
                                    data = await response.text()
                                    ip = json.loads(data).get("origin", "Unknown")
                                    elapsed = round((time.time() - start) * 1000, 2)
                                    return True, proxy, f"{elapsed} ms | IP: {ip}"
                except asyncio.CancelledError:
                    return False, proxy, "Cancelled"
                except Exception:
                    continue

        return False, proxy, "Dead"

# ================= RUNNER =================
async def run_checker(proxies: list, session: dict):
    session["results"] = []
    session["running"] = True
    session["cancelled"] = False
    session["total"] = len(proxies)

    tasks = [asyncio.create_task(check_one(p, session)) for p in proxies]
    session["active_tasks"] = tasks

    for fut in asyncio.as_completed(tasks):
        if session["cancelled"]:
            for t in tasks:
                if not t.done():
                    t.cancel()
            break
        try:
            status, proxy, msg = await fut
            if session["cancelled"]:
                break
            if msg != "Cancelled":
                session["results"].append({
                    "proxy": proxy,
                    "status": "OK" if status else "DEAD",
                    "info": msg,
                })
        except asyncio.CancelledError:
            break
        except Exception:
            continue

    session["running"] = False
    session["active_tasks"] = []

# ================= ROUTES =================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {})

@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    x_session_id: Optional[str] = Header(None)
):
    if not x_session_id:
        return {"error": "No session ID provided"}
    session = get_session(x_session_id)
    if session["running"]:
        return {"error": "A scan is already running"}
    content = await file.read()
    proxies = load_proxies_from_text(content.decode(errors="ignore"))
    if not proxies:
        return {"error": "No proxies found in file"}
    asyncio.create_task(run_checker(proxies, session))
    return {"message": f"Scan started — {len(proxies)} proxies", "total": len(proxies)}

@app.get("/results")
async def get_results(x_session_id: Optional[str] = Header(None)):
    if not x_session_id or x_session_id not in SESSIONS:
        return {"running": False, "results": [], "paused": False, "total": 0, "cancelled": False}
    s = SESSIONS[x_session_id]
    return {
        "running": s["running"],
        "paused": s["paused"],
        "cancelled": s["cancelled"],
        "total": s["total"],
        "results": s["results"],
    }

@app.post("/toggle_pause")
async def toggle_pause(x_session_id: Optional[str] = Header(None)):
    if not x_session_id:
        return {"error": "No session ID"}
    session = get_session(x_session_id)
    if not session["running"]:
        return {"paused": False}
    session["paused"] = not session["paused"]
    return {"paused": session["paused"]}

@app.get("/download")
async def download(x_session_id: Optional[str] = Header(None)):
    if not x_session_id or x_session_id not in SESSIONS:
        return {"error": "Session not found"}
    s = SESSIONS[x_session_id]
    working = [r["proxy"] for r in s["results"] if r["status"] == "OK"]
    if not working:
        return {"error": "No working proxies found"}
    content = "\n".join(working)
    return StreamingResponse(
        io.BytesIO(content.encode()),
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=working_proxies.txt"}
    )

@app.post("/reset")
async def reset(x_session_id: Optional[str] = Header(None)):
    if not x_session_id:
        return {"error": "No session ID"}
    session = get_session(x_session_id)

    session["cancelled"] = True
    session["paused"] = False
    for t in session.get("active_tasks", []):
        if not t.done():
            t.cancel()

    await asyncio.sleep(0.2)

    session["results"] = []
    session["running"] = False
    session["paused"] = False
    session["cancelled"] = False
    session["total"] = 0
    session["active_tasks"] = []

    return {"ok": True}
