import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright
import httpx
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ABA-Auto")

app = FastAPI(title="ABA PayWay Auto-Intercept Microservice")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory session tracking
sessions: Dict[str, dict] = {}

class InitRequest(BaseModel):
    payway_url: str

class StatusResponse(BaseModel):
    status: str
    message: Optional[str] = None

# ============ Playwright Auto-Interceptor ============
async def intercept_aba_session(target_url: str) -> dict:
    captured = {}
    done_event = asyncio.Event()

    async with async_playwright() as p:
        # បើក Headless Chromium លើ Server
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
            viewport={"width": 412, "height": 915}
        )
        page = await context.new_page()

        # ស្ទាក់ចាប់ Network Request
        async def handle_request(request):
            # ស្ទាក់ចាប់ Token និង Hash ចេញពី check-payment-status
            if "check-payment-status" in request.url:
                try:
                    headers = request.headers
                    post_data = request.post_data_json
                    if headers.get("token"):
                        captured["token"] = headers.get("token")
                    if post_data:
                        captured["device_id"] = post_data.get("device_id")
                        captured["request_time"] = post_data.get("request_time")
                        captured["client_id"] = post_data.get("client_id")
                        captured["hash"] = post_data.get("hash")
                    if "token" in captured and "hash" in captured:
                        done_event.set()
                except Exception as e:
                    logger.warning(f"Error reading intercepted request: {e}")

        page.on("request", handle_request)

        try:
            # ចូលទៅ PayWay Checkout Link
            await page.goto(target_url, wait_until="networkidle", timeout=30000)
            
            # ស្វែងរក Element KHQR String ឬ Fallback យក Link
            qr_element = await page.query_selector("[data-qr], img[src*='qr'], canvas")
            captured["qr_string"] = target_url

            # រង់ចាំចាប់ network payload យ៉ាងយូរ 10 វិនាទី
            await asyncio.wait_for(done_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Capture wait timed out. Checking collected state.")
        finally:
            await browser.close()

    if not captured.get("token") or not captured.get("hash"):
        raise HTTPException(
            status_code=502, 
            detail="Could not intercept ABA session token/hash. Cloudflare might have challenged the request."
        )

    return captured

# ============ API Endpoints ============
@app.post("/api/payway/init")
async def init_payway(req: InitRequest):
    if not req.payway_url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid PayWay URL")

    try:
        data = await intercept_aba_session(req.payway_url)
        session_id = secrets.token_urlsafe(16)
        
        sessions[session_id] = {
            "token": data["token"],
            "hash": data["hash"],
            "client_id": data["client_id"],
            "request_time": data["request_time"],
            "device_id": data.get("device_id", "ahFkzEzsCj"),
            "status": "Pending",
            "created_at": datetime.utcnow()
        }

        return {
            "session_id": session_id,
            "qr_string": data["qr_string"],
            "deep_link": f"aba://payway?token={data['token']}"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Init failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/payway/status", response_model=StatusResponse)
async def check_payway_status(session_id: str):
    sess = sessions.get(session_id)
    if not sess:
        return StatusResponse(status="Pending", message="Syncing session...")

    if datetime.utcnow() - sess["created_at"] > timedelta(minutes=5):
        sess["status"] = "Expired"
        return StatusResponse(status="Expired", message="Session expired")

    # បាញ់ទៅ Endpoint ផ្លូវការរបស់ ABA ជាមួយ Token + Hash ដែលចាប់បាន
    url = "https://pwapp.ababank.com/api/pw-app/v1/payment-link/check-payment-status"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "token": sess["token"],
        "language": "en",
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36"
    }
    payload = {
        "device_id": sess["device_id"],
        "request_time": sess["request_time"],
        "client_id": sess["client_id"],
        "hash": sess["hash"]
    }

    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                res_json = resp.json()
                action = res_json.get("data", {}).get("action")
                
                if action == "request_qr":
                    sess["status"] = "Pending"
                    return StatusResponse(status="Pending", message="Waiting for scan...")
                elif action in ["payment_success", "completed", "approved"]:
                    sess["status"] = "Approved"
                    return StatusResponse(status="Approved", message="Payment confirmed!")
        except Exception as e:
            logger.warning(f"Error querying ABA status: {e}")

    return StatusResponse(status=sess["status"], message="Processing")

# ============ Integrated Frontend ============
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content="""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ABA PayWay Live Interceptor</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
</head>
<body class="bg-slate-100 min-h-screen flex items-center justify-center p-4">
    <div class="max-w-md w-full bg-white rounded-2xl shadow-xl p-6 border border-slate-200">
        <h1 class="text-xl font-bold text-center text-slate-800 mb-1">ABA PayWay Checkout</h1>
        <p class="text-xs text-center text-slate-400 mb-6">Playwright Auto-Intercept Mode</p>

        <!-- Input Setup -->
        <div id="setup-view">
            <div class="mb-4">
                <label class="block text-xs font-semibold text-slate-600 mb-1">Paste PayWay Link:</label>
                <input id="paywayUrl" type="url" placeholder="https://link.payway.com.kh/..." 
                       class="w-full text-xs p-3 border rounded-xl focus:ring-2 focus:ring-blue-500 outline-none bg-slate-50"/>
            </div>
            <button id="submitBtn" onclick="initiatePayment()" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold py-3 rounded-xl transition shadow">
                Start Auto Capture & Pay
            </button>
        </div>

        <!-- Processing Screen -->
        <div id="qr-view" class="hidden text-center">
            <div class="flex justify-center mb-4">
                <div id="qrcode" class="p-3 bg-white border rounded-xl shadow-sm"></div>
            </div>
            <p id="statusTxt" class="text-sm font-semibold text-amber-600 mb-4 animate-pulse">Waiting for payment scan...</p>
            <a id="deepLinkBtn" href="#" class="block w-full bg-cyan-600 hover:bg-cyan-700 text-white font-medium py-2.5 rounded-xl text-sm mb-3">
                Open in ABA Mobile
            </a>
            <button onclick="location.reload()" class="text-xs text-slate-400 hover:text-slate-600">Cancel</button>
        </div>

        <!-- Approved Screen -->
        <div id="success-view" class="hidden text-center">
            <div class="w-16 h-16 bg-emerald-100 text-emerald-600 rounded-full flex items-center justify-center mx-auto mb-3 text-2xl font-bold">✓</div>
            <h2 class="text-xl font-bold text-slate-800 mb-1">Payment Approved!</h2>
            <p class="text-xs text-slate-400 mb-6">Transaction completed successfully.</p>
            <button onclick="location.reload()" class="w-full bg-slate-100 hover:bg-slate-200 text-slate-700 font-medium py-2.5 rounded-xl text-sm">
                Make Another Payment
            </button>
        </div>
    </div>

    <script>
        let pollTimer = null;
        let activeSessionId = null;

        async function initiatePayment() {
            const url = document.getElementById('paywayUrl').value.trim();
            const btn = document.getElementById('submitBtn');

            if (!url) {
                alert('Please enter a PayWay link');
                return;
            }

            btn.disabled = true;
            btn.innerText = 'Capturing ABA Session (5-10s)...';

            try {
                const res = await fetch('/api/payway/init', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ payway_url: url })
                });
                
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || 'Initialization failed');

                activeSessionId = data.session_id;
                document.getElementById('setup-view').classList.add('hidden');
                document.getElementById('qr-view').classList.remove('hidden');

                new QRCode(document.getElementById('qrcode'), {
                    text: data.qr_string,
                    width: 180,
                    height: 180
                });

                if (data.deep_link) {
                    document.getElementById('deepLinkBtn').href = data.deep_link;
                }

                // Poll status every 3s
                pollTimer = setInterval(pollStatus, 3000);
            } catch (err) {
                alert(err.message);
                btn.disabled = false;
                btn.innerText = 'Start Auto Capture & Pay';
            }
        }

        async function pollStatus() {
            if (!activeSessionId) return;
            try {
                const res = await fetch('/api/payway/status?session_id=' + activeSessionId, { method: 'POST' });
                const data = await res.json();
                if (data.status === 'Approved') {
                    clearInterval(pollTimer);
                    document.getElementById('qr-view').classList.add('hidden');
                    document.getElementById('success-view').classList.remove('hidden');
                }
            } catch (e) {
                console.error(e);
            }
        }
    </script>
</body>
</html>
    """)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
