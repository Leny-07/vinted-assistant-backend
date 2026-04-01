from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx, asyncio, os, hashlib
from datetime import datetime

app = FastAPI(title="Vinted Assistant API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY = os.getenv("API_KEY", "change-me")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MIN_SCORE_ALERT = int(os.getenv("MIN_SCORE_ALERT", "60"))

DB = {"listings": {}, "alerts": [], "searches": [], "event_logs": [], "seen_ids": set()}

async def verify_key(x_api_key: Optional[str] = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Clé API invalide")
    return True

def log_event(level, module, message):
    entry = {"id": hashlib.md5(f"{datetime.now()}{message}".encode()).hexdigest()[:8], "level": level, "module": module, "message": message, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    DB["event_logs"].insert(0, entry)
    if len(DB["event_logs"]) > 500:
        DB["event_logs"] = DB["event_logs"][:500]

MARKET_PRICES = {"nike": 65, "adidas": 45, "supreme": 120, "ralph lauren": 90, "lacoste": 65, "longchamp": 55, "zara": 20, "levis": 40}

def score_listing(listing):
    brand = listing.get("brand", "").lower()
    price = listing.get("price", 0)
    market = MARKET_PRICES.get(brand, price * 1.5)
    price_score = max(0.0, min(1.0, 1.0 - (price / market - 0.3))) if market > 0 else 0.5
    photo_score = min(listing.get("photos_count", 0) / 5.0, 1.0)
    cond_map = {"Neuf avec étiquettes": 1.0, "Très bon état": 0.8, "Bon état": 0.6, "État satisfaisant": 0.3}
    cond_score = cond_map.get(listing.get("condition", ""), 0.4)
    seller_score = min(float(listing.get("seller_rating", 0)) / 5.0, 1.0)
    scores = {"price": (price_score, 30), "photos": (photo_score, 15), "condition": (cond_score, 15), "seller": (seller_score, 10)}
    total_w = sum(w for _, w in scores.values())
    raw = sum(s * w for s, w in scores.values())
    final_score = round((raw / total_w) * 100)
    price_ratio = price / market if market > 0 else 1.0
    if final_score >= 80 and price_ratio <= 0.5:
        deal_type, deal_label, priority = "fire", "🔥 Très sous-coté", "high"
    elif final_score >= 65 and price_ratio <= 0.75:
        deal_type, deal_label, priority = "good", "✅ Bonne affaire", "med"
    elif final_score >= 45:
        deal_type, deal_label, priority = "watch", "👁 À surveiller", "low"
    else:
        deal_type, deal_label, priority = "low", "❌ Faible intérêt", None
    return {**listing, "score": final_score, "deal_type": deal_type, "deal_label": deal_label, "priority": priority, "market_price": round(market)}

async def send_telegram(listing):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    diff = round((1 - listing["price"] / listing["market_price"]) * 100) if listing.get("market_price") else 0
    text = f"{listing['deal_label']} — Score {listing['score']}/100\n\n📦 {listing['title']}\n💶 Prix : *{listing['price']}€* (marché ~{listing.get('market_price','?')}€) → *-{diff}%*\n\n🔗 {listing.get('url','')}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "Markdown"})
    except Exception as e:
        log_event("ERROR", "notification", str(e))

async def monitor_loop():
    log_event("INFO", "scheduler", f"Démarrage surveillance")
    while True:
        for search in [s for s in DB["searches"] if s.get("is_active")]:
            try:
                params = {"search_text": search.get("filters", {}).get("brand", ""), "order": "newest_first", "per_page": 20}
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get("https://www.vinted.fr/api/v2/catalog/items", params=params, headers={"User-Agent": "Mozilla/5.0"})
                    if resp.status_code == 429:
                        await asyncio.sleep(120)
                        continue
                    data = resp.json()
                    for raw in data.get("items", []):
                        lid = str(raw.get("id", ""))
                        if lid in DB["seen_ids"]:
                            continue
                        DB["seen_ids"].add(lid)
                        listing = {"id": lid, "title": raw.get("title", ""), "brand": raw.get("brand_title", ""), "price": float(raw.get("price", {}).get("amount", 0) if isinstance(raw.get("price"), dict) else raw.get("price", 0)), "photos_count": len(raw.get("photos", [])), "condition": raw.get("status", ""), "seller_rating": raw.get("user", {}).get("feedback_reputation", 0), "url": f"https://www.vinted.fr/items/{lid}", "emoji": "📦"}
                        scored = score_listing(listing)
                        DB["listings"][lid] = scored
                        if scored.get("priority") in ("high", "med") and scored.get("score", 0) >= MIN_SCORE_ALERT:
                            alert = {"id": f"alert_{lid}", "title": scored["title"], "deal_label": scored["deal_label"], "score": scored["score"], "priority": scored["priority"], "reason": f"Score {scored['score']}/100", "sent_at": datetime.now().strftime("%H:%M"), "is_read": False}
                            DB["alerts"].insert(0, alert)
                            await send_telegram(scored)
                log_event("INFO", "search_monitor", f"Cycle OK: {search['name']}")
            except Exception as e:
                log_event("ERROR", "search_monitor", str(e))
        await asyncio.sleep(POLL_INTERVAL)

@app.on_event("startup")
async def startup():
    log_event("INFO", "app", "Vinted Assistant démarré")
    asyncio.create_task(monitor_loop())

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0", "active_searches": len([s for s in DB["searches"] if s.get("is_active")]), "listings_tracked": len(DB["listings"]), "alerts_count": len(DB["alerts"])}

@app.get("/listings")
async def get_listings(limit: int = 20, _=Depends(verify_key)):
    items = sorted(DB["listings"].values(), key=lambda x: x.get("score", 0), reverse=True)
    return {"items": items[:limit], "total": len(items)}

@app.get("/alerts")
async def get_alerts(limit: int = 20, _=Depends(verify_key)):
    return {"items": DB["alerts"][:limit], "total": len(DB["alerts"])}

@app.post("/alerts/{alert_id}/read")
async def mark_read(alert_id: str, _=Depends(verify_key)):
    for a in DB["alerts"]:
        if a["id"] == alert_id: a["is_read"] = True
    return {"ok": True}

@app.post("/alerts/read-all")
async def mark_all_read(_=Depends(verify_key)):
    for a in DB["alerts"]: a["is_read"] = True
    return {"ok": True}

@app.get("/searches")
async def get_searches(_=Depends(verify_key)):
    return {"items": DB["searches"]}

@app.post("/searches")
async def create_search(data: dict, _=Depends(verify_key)):
    s = {**data, "id": f"s_{len(DB['searches'])+1}", "is_active": True, "last_run": "jamais", "found_count": 0}
    DB["searches"].append(s)
    return s

@app.post("/searches/{sid}/activate")
async def activate(sid: str, _=Depends(verify_key)):
    for s in DB["searches"]:
        if s["id"] == sid: s["is_active"] = True
    return {"ok": True}

@app.post("/searches/{sid}/deactivate")
async def deactivate(sid: str, _=Depends(verify_key)):
    for s in DB["searches"]:
        if s["id"] == sid: s["is_active"] = False
    return {"ok": True}

@app.get("/analytics/stats")
async def stats(_=Depends(verify_key)):
    listings = list(DB["listings"].values())
    avg = round(sum(l.get("score", 0) for l in listings) / len(listings)) if listings else 0
    return {"listings_today": len(listings), "listings_delta": 0, "deals_today": len([l for l in listings if l.get("deal_type") in ("fire","good")]), "avg_score": avg, "alerts_sent": len(DB["alerts"]), "high_priority": len([a for a in DB["alerts"] if a.get("priority") == "high"])}

@app.post("/resale/estimate")
async def resale(data: dict, _=Depends(verify_key)):
    buy = data.get("purchase_price", 0)
    market = buy * 2.5
    return {"estimates": {"resale_min": round(market*0.65), "resale_med": round(market*0.85), "resale_max": round(market*1.1)}, "margin": {"med_percent": round(((market*0.85-buy)/buy*100) if buy else 0)}, "confidence": "medium", "based_on": "Estimation basée sur prix médians", "suggestions": {"title": "Article — Très bon état", "tags": ["vinted","mode"], "price_recommendation": round(market*0.85)}}

@app.get("/admin/logs")
async def get_logs(limit: int = 50, _=Depends(verify_key)):
    return {"items": DB["event_logs"][:limit], "total": len(DB["event_logs"])}
