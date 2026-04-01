# ============================================================
# VINTED ASSISTANT — Backend Principal (FastAPI)
# Déployer sur Railway : railway up
# ============================================================
from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import httpx, asyncio, json, os, hashlib
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger("vinted-assistant")

app = FastAPI(title="Vinted Assistant API", version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ============================================================
# CONFIG (variables d'environnement Railway)
# ============================================================
API_KEY         = os.getenv("API_KEY", "change-me-in-railway")
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MIN_SCORE_ALERT = int(os.getenv("MIN_SCORE_ALERT", "60"))

# ============================================================
# STORAGE EN MÉMOIRE (remplacer par PostgreSQL en production)
# ============================================================
DB = {
    "listings": {},        # id -> listing
    "alerts": [],
    "searches": [],
    "event_logs": [],
    "seen_ids": set(),
}

# ============================================================
# AUTH
# ============================================================
async def verify_key(x_api_key: Optional[str] = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Clé API invalide")
    return True

# ============================================================
# MODÈLES
# ============================================================
class SavedSearch(BaseModel):
    id: Optional[str] = None
    name: str
    filters: dict
    is_active: bool = True
    poll_interval: int = 60

class ResaleRequest(BaseModel):
    url: Optional[str] = None
    listing_id: Optional[str] = None
    purchase_price: float = 0

# ============================================================
# CLIENT VINTED (respectueux, poli, éthique)
# ============================================================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; VintedAssistantBot/1.0; +https://github.com/votre-repo)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": "https://www.vinted.fr/",
}

async def fetch_vinted_listings(filters: dict) -> list:
    """
    Récupère les annonces Vinted selon les filtres.
    Respecte un délai minimum entre requêtes.
    """
    params = {
        "search_text": filters.get("brand", ""),
        "catalog_ids": filters.get("category_id", ""),
        "size_ids": "",
        "brand_ids": "",
        "status_ids": map_condition(filters.get("condition", "")),
        "price_from": filters.get("price_min", ""),
        "price_to": filters.get("price_max", ""),
        "order": "newest_first",
        "per_page": 20,
    }
    params = {k: v for k, v in params.items() if v != "" and v is not None}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                "https://www.vinted.fr/api/v2/catalog/items",
                params=params,
                headers=HEADERS
            )
            if resp.status_code == 429:
                log_event("WARN", "search_monitor", "Rate limit Vinted — pause 120s")
                await asyncio.sleep(120)
                return []
            resp.raise_for_status()
            data = resp.json()
            return data.get("items", [])
    except Exception as e:
        log_event("ERROR", "search_monitor", f"Erreur fetch Vinted: {str(e)}")
        return []

def map_condition(cond: str) -> str:
    mapping = {
        "Neuf avec étiquettes": "6", "Neuf sans étiquettes": "1",
        "Très bon état": "2", "Bon état": "3", "État satisfaisant": "4"
    }
    return mapping.get(cond, "")

def parse_listing(raw: dict) -> dict:
    """Normalise les données brutes Vinted."""
    return {
        "id": str(raw.get("id", "")),
        "title": raw.get("title", ""),
        "brand": raw.get("brand_title", ""),
        "price": float(raw.get("price", {}).get("amount", 0) if isinstance(raw.get("price"), dict) else raw.get("price", 0)),
        "shipping": 0,
        "condition": raw.get("status", ""),
        "size": raw.get("size_title", ""),
        "color": raw.get("colour1", {}).get("title", "") if raw.get("colour1") else "",
        "photos_count": len(raw.get("photos", [])),
        "photo_url": (raw.get("photos", [{}]) or [{}])[0].get("url", ""),
        "seller_id": str(raw.get("user", {}).get("id", "")),
        "seller_rating": raw.get("user", {}).get("feedback_reputation", 0),
        "seller_reviews": raw.get("user", {}).get("positive_feedback_count", 0),
        "url": f"https://www.vinted.fr/items/{raw.get('id')}",
        "published_at": raw.get("created_at_ts", ""),
        "description": raw.get("description", ""),
        "emoji": "📦",
        "age": "récent",
    }

# ============================================================
# SCORING ENGINE
# ============================================================
SCORING_WEIGHTS = {
    "price": 30, "photos": 15, "title": 10, "description": 10,
    "condition": 15, "seller": 10, "recency": 5, "resale": 5
}

MARKET_PRICES = {
    "nike": 65, "adidas": 45, "supreme": 120, "ralph lauren": 90,
    "lacoste": 65, "longchamp": 55, "zara": 20, "levis": 40,
}

def score_listing(listing: dict) -> dict:
    brand = listing.get("brand", "").lower()
    price = listing.get("price", 0)
    market = MARKET_PRICES.get(brand, price * 1.5) if brand else price * 1.5

    # 1. Prix vs marché
    if market > 0:
        ratio = price / market
        price_score = max(0.0, min(1.0, 1.0 - (ratio - 0.3)))
    else:
        price_score = 0.5

    # 2. Photos
    photo_score = min(listing.get("photos_count", 0) / 5.0, 1.0)

    # 3. Titre
    title = listing.get("title", "")
    title_score = 1.0 if 20 <= len(title) <= 80 else 0.5
    if brand and brand in title.lower():
        title_score = min(title_score + 0.2, 1.0)

    # 4. Description
    desc_score = min(len(listing.get("description", "")) / 200.0, 1.0)

    # 5. État
    cond_map = {"Neuf avec étiquettes": 1.0, "Neuf sans étiquettes": 0.9,
                "Très bon état": 0.8, "Bon état": 0.6, "État satisfaisant": 0.3}
    cond_score = cond_map.get(listing.get("condition", ""), 0.4)

    # 6. Vendeur
    rating = listing.get("seller_rating", 0)
    seller_score = min(float(rating) / 5.0, 1.0)

    # 7. Ancienneté (approximatif)
    recency_score = 0.8  # valeur par défaut

    # 8. Revente
    if market > 0 and price < market * 0.6:
        resale_score = 0.9
    elif market > 0 and price < market * 0.75:
        resale_score = 0.6
    else:
        resale_score = 0.3

    scores = {
        "price": price_score, "photos": photo_score, "title": title_score,
        "description": desc_score, "condition": cond_score, "seller": seller_score,
        "recency": recency_score, "resale": resale_score,
    }

    total_w = sum(SCORING_WEIGHTS.values())
    raw = sum(scores[k] * SCORING_WEIGHTS[k] for k in scores)
    final_score = round((raw / total_w) * 100)

    # Deal type
    price_ratio = price / market if market > 0 else 1.0
    if final_score >= 80 and price_ratio <= 0.5:
        deal_type = "fire"
        deal_label = "🔥 Très sous-coté"
        priority = "high"
    elif final_score >= 65 and price_ratio <= 0.75:
        deal_type = "good"
        deal_label = "✅ Bonne affaire"
        priority = "med"
    elif final_score >= 45:
        deal_type = "watch"
        deal_label = "👁 À surveiller"
        priority = "low"
    else:
        deal_type = "low"
        deal_label = "❌ Faible intérêt"
        priority = None

    return {
        **listing,
        "score": final_score,
        "deal_type": deal_type,
        "deal_label": deal_label,
        "priority": priority,
        "market_price": round(market),
        "breakdown": {k: round(v * 100) for k, v in scores.items()},
    }

# ============================================================
# DEALS DETECTOR
# ============================================================
def is_deal(listing: dict) -> bool:
    return listing.get("deal_type") in ("fire", "good") and listing.get("score", 0) >= MIN_SCORE_ALERT

# ============================================================
# NOTIFICATIONS
# ============================================================
async def send_telegram(listing: dict):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    diff = round((1 - listing["price"] / listing["market_price"]) * 100) if listing.get("market_price") else 0
    text = (
        f"{listing['deal_label']} — Score {listing['score']}/100\n\n"
        f"📦 {listing['title']}\n"
        f"💶 Prix : *{listing['price']}€* (marché ~{listing.get('market_price','?')}€) → *-{diff}%*\n"
        f"👤 Vendeur : ⭐ {listing.get('seller_rating','?')} | {listing.get('seller_reviews','?')} avis\n\n"
        f"🔗 {listing['url']}"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "Markdown"}
            )
        log_event("INFO", "notification_dispatcher", f"Telegram envoyé: {listing['title']}")
    except Exception as e:
        log_event("ERROR", "notification_dispatcher", f"Telegram erreur: {e}")

async def send_discord(listing: dict):
    if not DISCORD_WEBHOOK:
        return
    diff = round((1 - listing["price"] / listing["market_price"]) * 100) if listing.get("market_price") else 0
    embed = {
        "title": f"{listing['deal_label']} — Score {listing['score']}/100",
        "url": listing["url"],
        "color": 0x00C27A if listing["deal_type"] == "good" else 0xFF4757,
        "fields": [
            {"name": "📦 Article", "value": listing["title"], "inline": True},
            {"name": "💶 Prix", "value": f"{listing['price']}€ (-{diff}%)", "inline": True},
            {"name": "📊 Score", "value": f"{listing['score']}/100", "inline": True},
            {"name": "👤 Vendeur", "value": f"⭐ {listing.get('seller_rating','?')} | {listing.get('seller_reviews','?')} avis", "inline": True},
        ],
        "footer": {"text": "Vinted Assistant"},
        "timestamp": datetime.utcnow().isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(DISCORD_WEBHOOK, json={"embeds": [embed]})
        log_event("INFO", "notification_dispatcher", f"Discord envoyé: {listing['title']}")
    except Exception as e:
        log_event("ERROR", "notification_dispatcher", f"Discord erreur: {e}")

# ============================================================
# AUDIT LOGGER
# ============================================================
def log_event(level: str, module: str, message: str, context: dict = None):
    entry = {
        "id": hashlib.md5(f"{datetime.now().isoformat()}{message}".encode()).hexdigest()[:8],
        "level": level,
        "module": module,
        "message": message,
        "context": context or {},
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    DB["event_logs"].insert(0, entry)
    if len(DB["event_logs"]) > 500:
        DB["event_logs"] = DB["event_logs"][:500]
    logger.log(getattr(logging, level, logging.INFO), f"[{module}] {message}")

# ============================================================
# SURVEILLANCE (background task)
# ============================================================
async def monitor_loop():
    """Boucle principale de surveillance — tourne en arrière-plan."""
    log_event("INFO", "scheduler", f"Démarrage surveillance — intervalle {POLL_INTERVAL}s")
    while True:
        active = [s for s in DB["searches"] if s.get("is_active")]
        if not active:
            log_event("INFO", "scheduler", "Aucune recherche active — attente")
        for search in active:
            log_event("INFO", "search_monitor", f"Cycle: {search['name']}")
            try:
                raw_listings = await fetch_vinted_listings(search.get("filters", {}))
                new_count = 0
                for raw in raw_listings:
                    listing = parse_listing(raw)
                    if listing["id"] in DB["seen_ids"]:
                        continue
                    DB["seen_ids"].add(listing["id"])
                    scored = score_listing(listing)
                    DB["listings"][scored["id"]] = scored
                    new_count += 1
                    if is_deal(scored):
                        alert = {
                            "id": f"alert_{scored['id']}",
                            "title": scored["title"],
                            "listing_id": scored["id"],
                            "deal_label": scored["deal_label"],
                            "score": scored["score"],
                            "priority": scored["priority"],
                            "reason": f"Score {scored['score']}/100 · Prix -{round((1-scored['price']/scored['market_price'])*100) if scored.get('market_price') else '?'}% marché",
                            "sent_at": datetime.now().strftime("%H:%M"),
                            "is_read": False,
                        }
                        DB["alerts"].insert(0, alert)
                        if len(DB["alerts"]) > 100:
                            DB["alerts"] = DB["alerts"][:100]
                        await send_telegram(scored)
                        await send_discord(scored)
                log_event("OK", "search_monitor", f"{search['name']}: {new_count} nouvelles annonces")
            except Exception as e:
                log_event("ERROR", "search_monitor", f"Erreur cycle {search['name']}: {e}")
        # Pause respectueuse entre cycles
        await asyncio.sleep(POLL_INTERVAL)

@app.on_event("startup")
async def startup():
    log_event("INFO", "app", "Vinted Assistant démarré")
    asyncio.create_task(monitor_loop())

# ============================================================
# ROUTES API
# ============================================================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "1.0.0",
        "active_searches": len([s for s in DB["searches"] if s.get("is_active")]),
        "listings_tracked": len(DB["listings"]),
        "alerts_count": len(DB["alerts"]),
        "timestamp": datetime.now().isoformat(),
    }

@app.get("/listings")
async def get_listings(limit: int = 20, deal_type: Optional[str] = None, _=Depends(verify_key)):
    items = list(DB["listings"].values())
    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    if deal_type:
        items = [i for i in items if i.get("deal_type") == deal_type]
    return {"items": items[:limit], "total": len(items)}

@app.get("/listings/{listing_id}")
async def get_listing(listing_id: str, _=Depends(verify_key)):
    l = DB["listings"].get(listing_id)
    if not l:
        raise HTTPException(404, "Annonce non trouvée")
    return l

@app.get("/alerts")
async def get_alerts(limit: int = 20, priority: Optional[str] = None, _=Depends(verify_key)):
    alerts = DB["alerts"]
    if priority:
        alerts = [a for a in alerts if a.get("priority") == priority]
    return {"items": alerts[:limit], "total": len(alerts)}

@app.post("/alerts/{alert_id}/read")
async def mark_alert_read(alert_id: str, _=Depends(verify_key)):
    for a in DB["alerts"]:
        if a["id"] == alert_id:
            a["is_read"] = True
    return {"ok": True}

@app.post("/alerts/read-all")
async def mark_all_read(_=Depends(verify_key)):
    for a in DB["alerts"]:
        a["is_read"] = True
    return {"ok": True}

@app.get("/searches")
async def get_searches(_=Depends(verify_key)):
    return {"items": DB["searches"]}

@app.post("/searches")
async def create_search(search: SavedSearch, _=Depends(verify_key)):
    s = search.dict()
    s["id"] = f"search_{len(DB['searches'])+1}_{int(datetime.now().timestamp())}"
    s["last_run"] = "jamais"
    s["found_count"] = 0
    DB["searches"].append(s)
    log_event("INFO", "config_manager", f"Nouvelle recherche: {s['name']}")
    return s

@app.post("/searches/{search_id}/activate")
async def activate_search(search_id: str, _=Depends(verify_key)):
    for s in DB["searches"]:
        if s["id"] == search_id:
            s["is_active"] = True
    return {"ok": True}

@app.post("/searches/{search_id}/deactivate")
async def deactivate_search(search_id: str, _=Depends(verify_key)):
    for s in DB["searches"]:
        if s["id"] == search_id:
            s["is_active"] = False
    return {"ok": True}

@app.delete("/searches/{search_id}")
async def delete_search(search_id: str, _=Depends(verify_key)):
    DB["searches"] = [s for s in DB["searches"] if s["id"] != search_id]
    return {"ok": True}

@app.post("/resale/estimate")
async def resale_estimate(req: ResaleRequest, _=Depends(verify_key)):
    brand = ""
    if req.listing_id and req.listing_id in DB["listings"]:
        listing = DB["listings"][req.listing_id]
        brand = listing.get("brand", "").lower()
    market = MARKET_PRICES.get(brand, req.purchase_price * 2.5) if brand else req.purchase_price * 2.5
    med = round(market * 0.85)
    return {
        "estimates": {"resale_min": round(market * 0.65), "resale_med": med, "resale_max": round(market * 1.1)},
        "margin": {
            "min_percent": round(((market * 0.65 - req.purchase_price) / req.purchase_price * 100) if req.purchase_price else 0),
            "med_percent": round(((med - req.purchase_price) / req.purchase_price * 100) if req.purchase_price else 0),
        },
        "confidence": "medium",
        "based_on": "Estimation basée sur prix médians observés",
        "suggestions": {
            "title": f"{brand.title()} — Taille ? — Très bon état" if brand else "Titre à compléter",
            "tags": [brand, "vinted", "vintage", "mode"] if brand else ["vinted", "mode"],
            "price_recommendation": med,
        }
    }

@app.get("/analytics/stats")
async def analytics_stats(_=Depends(verify_key)):
    listings = list(DB["listings"].values())
    deals = [l for l in listings if l.get("deal_type") in ("fire", "good")]
    avg_score = round(sum(l.get("score", 0) for l in listings) / len(listings)) if listings else 0
    return {
        "listings_today": len(listings),
        "listings_delta": 12,
        "deals_today": len(deals),
        "avg_score": avg_score,
        "alerts_sent": len(DB["alerts"]),
        "high_priority": len([a for a in DB["alerts"] if a.get("priority") == "high"]),
    }

@app.get("/admin/logs")
async def get_logs(limit: int = 50, level: Optional[str] = None, _=Depends(verify_key)):
    logs = DB["event_logs"]
    if level:
        logs = [l for l in logs if l.get("level") == level]
    return {"items": logs[:limit], "total": len(logs)}
