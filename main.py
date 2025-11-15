import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Literal, Any, Dict
import re
import csv
import io
import json
import hashlib
import secrets

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import db
from bson.objectid import ObjectId

app = FastAPI(title="BizMate API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------ Utilities ------------------

def oid(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID")


def serialize(doc: Dict[str, Any]):
    if not doc:
        return doc
    d = {**doc}
    if "_id" in d:
        d["id"] = str(d.pop("_id"))
    # convert datetimes to iso
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.astimezone(timezone.utc).isoformat()
    return d


def now_utc():
    return datetime.now(timezone.utc)


# ------------------ Schemas ------------------
class CustomerIn(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    note: Optional[str] = None


class ProductIn(BaseModel):
    name: str
    unit_price: float = Field(..., ge=0)
    quantity: int = Field(0, ge=0)
    threshold: int = Field(0, ge=0)


class SaleItemIn(BaseModel):
    product_id: str
    quantity: int = Field(..., ge=1)
    price: Optional[float] = Field(None, ge=0)


class SaleIn(BaseModel):
    customer_id: Optional[str] = None
    items: List[SaleItemIn]
    payment_mode: Literal['Cash', 'UPI', 'Card', 'Other'] = 'Cash'


class ChatMessage(BaseModel):
    message: str


class PinPayload(BaseModel):
    pin: str


# ------------------ Health/Test ------------------
@app.get("/")
def read_root():
    return {"message": "BizMate Backend is running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set",
        "database_name": "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set",
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Connected & Working"
            response["connection_status"] = "Connected"
            response["collections"] = db.list_collection_names()
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:120]}"
    return response


# ------------------ Settings & Auth (PIN) ------------------
SETTINGS_COLL = "setting"


def get_setting(key: str) -> Optional[str]:
    doc = db[SETTINGS_COLL].find_one({"key": key})
    return doc.get("value") if doc else None


def set_setting(key: str, value: str):
    db[SETTINGS_COLL].update_one({"key": key}, {"$set": {"key": key, "value": value, "updated_at": now_utc()}}, upsert=True)


@app.get("/api/settings")
def list_settings():
    return [serialize(s) for s in db[SETTINGS_COLL].find({}).sort("key", 1)]


@app.post("/api/settings")
class SettingIn(BaseModel):
    key: str
    value: str


@app.post("/api/settings")
def save_setting(payload: SettingIn):
    set_setting(payload.key, payload.value)
    return {"status": "ok"}


@app.post("/api/auth/setup-pin")
def setup_pin(payload: PinPayload):
    if len(payload.pin) < 4 or len(payload.pin) > 12:
        raise HTTPException(400, "PIN length must be between 4 and 12")
    if get_setting("pin_hash"):
        raise HTTPException(400, "PIN already set. Use /api/auth/login to authenticate.")
    salt = secrets.token_hex(8)
    pin_hash = hashlib.sha256((salt + payload.pin).encode()).hexdigest()
    set_setting("pin_salt", salt)
    set_setting("pin_hash", pin_hash)
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(payload: PinPayload):
    salt = get_setting("pin_salt")
    pin_hash = get_setting("pin_hash")
    if not salt or not pin_hash:
        return {"authenticated": True, "note": "No PIN configured."}
    test_hash = hashlib.sha256((salt + payload.pin).encode()).hexdigest()
    if secrets.compare_digest(test_hash, pin_hash):
        return {"authenticated": True}
    raise HTTPException(401, "Invalid PIN")


# ------------------ Customers ------------------
@app.get("/api/customers")
def list_customers(q: Optional[str] = None):
    filt: Dict[str, Any] = {}
    if q:
        filt = {"$or": [
            {"name": {"$regex": q, "$options": "i"}},
            {"phone": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
        ]}
    cur = db["customer"].find(filt).sort("name", 1)
    return [serialize(doc) for doc in cur]


@app.post("/api/customers")
def create_customer(payload: CustomerIn):
    data = payload.model_dump()
    data.update({"created_at": now_utc(), "updated_at": now_utc(), "debt_balance": 0.0})
    res = db["customer"].insert_one(data)
    doc = db["customer"].find_one({"_id": res.inserted_id})
    return serialize(doc)


@app.get("/api/customers/{customer_id}")
def get_customer(customer_id: str):
    doc = db["customer"].find_one({"_id": oid(customer_id)})
    if not doc:
        raise HTTPException(404, "Customer not found")
    total_sales = db["sale"].count_documents({"customer_id": customer_id})
    doc["total_sales_count"] = total_sales
    return serialize(doc)


@app.delete("/api/customers/{customer_id}")
def delete_customer(customer_id: str):
    res = db["customer"].delete_one({"_id": oid(customer_id)})
    if res.deleted_count == 0:
        raise HTTPException(404, "Customer not found")
    return {"status": "ok"}


# ------------------ Products / Stock ------------------
@app.get("/api/products")
def list_products():
    cur = db["product"].find({}).sort("name", 1)
    items = []
    for d in cur:
        d = serialize(d)
        d["low_stock"] = d.get("quantity", 0) <= d.get("threshold", 0)
        items.append(d)
    return items


@app.post("/api/products")
def upsert_product(payload: ProductIn):
    existing = db["product"].find_one({"name": payload.name})
    if existing:
        update = {
            "$set": {
                "unit_price": payload.unit_price,
                "threshold": payload.threshold,
                "updated_at": now_utc(),
            },
            "$inc": {"quantity": payload.quantity}
        }
        db["product"].update_one({"_id": existing["_id"]}, update)
        doc = db["product"].find_one({"_id": existing["_id"]})
    else:
        data = payload.model_dump()
        data.update({"created_at": now_utc(), "updated_at": now_utc()})
        res = db["product"].insert_one(data)
        doc = db["product"].find_one({"_id": res.inserted_id})
    d = serialize(doc)
    d["low_stock"] = d.get("quantity", 0) <= d.get("threshold", 0)
    return d


@app.patch("/api/products/{product_id}")
def update_product(product_id: str, payload: ProductIn):
    update = {k: v for k, v in payload.model_dump().items() if v is not None}
    update["updated_at"] = now_utc()
    res = db["product"].update_one({"_id": oid(product_id)}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(404, "Product not found")
    doc = db["product"].find_one({"_id": oid(product_id)})
    return serialize(doc)


# ------------------ Sales ------------------
@app.get("/api/sales")
def list_sales(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    payment_mode: Optional[str] = None,
    customer_id: Optional[str] = None,
    product_id: Optional[str] = None,
):
    filt: Dict[str, Any] = {}
    if payment_mode:
        filt["payment_mode"] = payment_mode
    if customer_id:
        filt["customer_id"] = customer_id
    if date_from or date_to:
        rng: Dict[str, Any] = {}
        if date_from:
            rng["$gte"] = datetime.fromisoformat(date_from)
        if date_to:
            rng["$lte"] = datetime.fromisoformat(date_to)
        filt["created_at"] = rng
    cur = db["sale"].find(filt).sort("created_at", -1)
    sales = []
    for s in cur:
        s = serialize(s)
        if product_id:
            items = [i for i in s.get("items", []) if i.get("product_id") == product_id]
            if not items:
                continue
            s["items"] = items
        sales.append(s)
    return sales


@app.post("/api/sales")
def create_sale(payload: SaleIn):
    items_out = []
    total = 0.0
    for it in payload.items:
        prod = db["product"].find_one({"_id": oid(it.product_id)})
        if not prod:
            raise HTTPException(400, f"Product not found: {it.product_id}")
        price = float(it.price if it.price is not None else prod.get("unit_price", 0.0))
        qty = int(it.quantity)
        if prod.get("quantity", 0) < qty:
            raise HTTPException(400, f"Insufficient stock for {prod['name']}")
        total += price * qty
        items_out.append({
            "product_id": str(prod["_id"]),
            "product_name": prod["name"],
            "quantity": qty,
            "price": price,
        })
    sale_doc = {
        "customer_id": payload.customer_id,
        "items": items_out,
        "payment_mode": payload.payment_mode,
        "total_amount": round(total, 2),
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }
    if payload.customer_id:
        cust = db["customer"].find_one({"_id": oid(payload.customer_id)})
        if cust:
            sale_doc["customer_name"] = cust.get("name")
    res = db["sale"].insert_one(sale_doc)
    for it in items_out:
        db["product"].update_one({"_id": oid(it["product_id"])}, {"$inc": {"quantity": -it["quantity"]}, "$set": {"updated_at": now_utc()}})
    return serialize(db["sale"].find_one({"_id": res.inserted_id}))


@app.get("/api/sales/export")
def export_sales_csv():
    cur = db["sale"].find({}).sort("created_at", -1)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Customer", "Payment", "Product", "Qty", "Price", "Line Total", "Sale Total"])
    for s in cur:
        sale_total = float(s.get("total_amount", 0))
        created = s.get("created_at")
        when = created.astimezone(timezone.utc).isoformat() if isinstance(created, datetime) else str(created)
        customer = s.get("customer_name") or "General"
        pay = s.get("payment_mode")
        for item in s.get("items", []):
            lt = float(item.get("quantity", 0)) * float(item.get("price", 0))
            writer.writerow([when, customer, pay, item.get("product_name"), item.get("quantity"), item.get("price"), lt, sale_total])
    csv_data = output.getvalue()
    return Response(content=csv_data, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=sales.csv"})


# ------------------ Analytics ------------------
@app.get("/api/stats/summary")
def stats_summary():
    now = now_utc()
    start_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    start_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    start_year = datetime(now.year, 1, 1, tzinfo=timezone.utc)

    def total_since(start: datetime) -> float:
        pipeline = [
            {"$match": {"created_at": {"$gte": start}}},
            {"$group": {"_id": None, "sum": {"$sum": "$total_amount"}}}
        ]
        res = list(db["sale"].aggregate(pipeline))
        return float(res[0]["sum"]) if res else 0.0

    daily = total_since(start_day)
    monthly = total_since(start_month)
    yearly = total_since(start_year)

    pipeline_top_products = [
        {"$unwind": "$items"},
        {"$group": {"_id": "$items.product_name", "qty": {"$sum": "$items.quantity"}, "amount": {"$sum": {"$multiply": ["$items.quantity", "$items.price"]}}}},
        {"$sort": {"qty": -1}},
        {"$limit": 5}
    ]
    top_products = [{"name": d["_id"], "quantity": d["qty"], "amount": d.get("amount", 0)} for d in db["sale"].aggregate(pipeline_top_products)]

    pipeline_top_customers = [
        {"$match": {"customer_id": {"$ne": None}}},
        {"$group": {"_id": {"id": "$customer_id", "name": "$customer_name"}, "amount": {"$sum": "$total_amount"}, "count": {"$sum": 1}}},
        {"$sort": {"amount": -1}},
        {"$limit": 5}
    ]
    top_customers = [{"id": d["_id"]["id"], "name": d["_id"].get("name") or "Customer", "amount": d["amount"], "orders": d["count"]} for d in db["sale"].aggregate(pipeline_top_customers)]

    pipeline_payments = [
        {"$group": {"_id": "$payment_mode", "amount": {"$sum": "$total_amount"}}}
    ]
    payment_modes = [{"mode": d["_id"], "amount": d["amount"]} for d in db["sale"].aggregate(pipeline_payments)]

    return {
        "daily": round(daily, 2),
        "monthly": round(monthly, 2),
        "yearly": round(yearly, 2),
        "top_products": top_products,
        "top_customers": top_customers,
        "payment_modes": payment_modes,
        "low_stock": [serialize(p) for p in db["product"].find({"$expr": {"$lte": ["$quantity", "$threshold"]}}).limit(10)]
    }


# ------------------ Backup Export ------------------
@app.get("/api/backup/export")
def export_backup():
    data = {
        "customers": [serialize(x) for x in db["customer"].find({})],
        "products": [serialize(x) for x in db["product"].find({})],
        "sales": [serialize(x) for x in db["sale"].find({})],
        "settings": [serialize(x) for x in db[SETTINGS_COLL].find({})],
        "exported_at": now_utc().isoformat(),
    }
    return data


# ------------------ Chat Assistant (Rule-based MVP) ------------------
@app.post("/api/chat")
def chat_command(msg: ChatMessage):
    text = msg.message.strip()
    lower = text.lower()

    # Add stock: "add 100 bottles of water to stock" or similar
    m = re.search(r"add\s+(?P<qty>\d+)\s+(?P<name>[\w\s-]+)\s+(?:to\s+stock|stock)", lower)
    if m:
        qty = int(m.group("qty"))
        name = m.group("name").strip()
        existing = db["product"].find_one({"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}})
        unit_price = float(existing.get("unit_price", 0)) if existing else 0.0
        threshold = int(existing.get("threshold", 0)) if existing else 0
        if existing:
            db["product"].update_one({"_id": existing["_id"]}, {"$inc": {"quantity": qty}, "$set": {"updated_at": now_utc()}})
            prod = db["product"].find_one({"_id": existing["_id"]})
        else:
            res = db["product"].insert_one({"name": name.title(), "unit_price": unit_price, "quantity": qty, "threshold": threshold, "created_at": now_utc(), "updated_at": now_utc()})
            prod = db["product"].find_one({"_id": res.inserted_id})
        d = serialize(prod)
        return {"reply": f"✅ Added {qty} to stock for {d['name']}. Current qty: {d['quantity']}."}

    # Show sales this week
    if "show" in lower and "sales" in lower and ("week" in lower or "this week" in lower):
        start = now_utc() - timedelta(days=7)
        cur = db["sale"].find({"created_at": {"$gte": start}}).sort("created_at", -1)
        total = 0
        count = 0
        for s in cur:
            total += float(s.get("total_amount", 0))
            count += 1
        return {"reply": f"📊 In the last 7 days: {count} sales totaling ₹{round(total,2)}."}

    # Add sale: "add sale 2 shirts @ 500 via upi for ramesh"
    m = re.search(r"add\s+sale\s+(?P<qty>\d+)\s+(?P<product>[\w\s-]+?)\s*@\s*(?P<price>\d+(?:\.\d+)?)\s*(?:via\s*(?P<mode>cash|upi|card))?(?:\s*for\s*(?P<customer>[\w\s-]+))?", lower)
    if m:
        qty = int(m.group("qty"))
        product_name = m.group("product").strip()
        price = float(m.group("price"))
        mode = (m.group("mode") or "cash").title()
        cust_name = m.group("customer")
        prod = db["product"].find_one({"name": {"$regex": f"^{re.escape(product_name)}$", "$options": "i"}})
        if not prod:
            res = db["product"].insert_one({"name": product_name.title(), "unit_price": price, "quantity": 0, "threshold": 0, "created_at": now_utc(), "updated_at": now_utc()})
            prod = db["product"].find_one({"_id": res.inserted_id})
        prod_id = str(prod["_id"])
        cust_id = None
        cust_display = None
        if cust_name:
            cust = db["customer"].find_one({"name": {"$regex": f"^{re.escape(cust_name)}$", "$options": "i"}})
            if not cust:
                res = db["customer"].insert_one({"name": cust_name.title(), "created_at": now_utc(), "updated_at": now_utc(), "debt_balance": 0.0})
                cust = db["customer"].find_one({"_id": res.inserted_id})
            cust_id = str(cust["_id"])
            cust_display = cust.get("name")
        current = int(prod.get("quantity", 0))
        if current < qty:
            return {"reply": f"⚠️ Not enough stock for {prod['name']} (have {current}, need {qty})."}
        sale = {
            "customer_id": cust_id,
            "customer_name": cust_display,
            "items": [{"product_id": prod_id, "product_name": prod["name"], "quantity": qty, "price": price}],
            "payment_mode": mode,
            "total_amount": round(qty * price, 2),
            "created_at": now_utc(),
            "updated_at": now_utc(),
        }
        db["sale"].insert_one(sale)
        db["product"].update_one({"_id": ObjectId(prod_id)}, {"$inc": {"quantity": -qty}, "$set": {"updated_at": now_utc()}})
        who = f" for {cust_display}" if cust_display else ""
        return {"reply": f"💰 Added sale of {qty} x {prod['name']} at ₹{price} via {mode}{who}. Total ₹{sale['total_amount']} ✅"}

    return {"reply": "🤖 I didn't fully understand. Try: 'Add sale 2 shirts @ 500 via UPI for Ramesh' or 'Add 100 water bottles to stock'."}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
