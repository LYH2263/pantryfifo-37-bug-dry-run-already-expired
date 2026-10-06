import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo
from app.engines import dry_overlay

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]
    rows = dry_overlay.paint_fridge(rows, dry_overlay.load_preview(c))
    c.close(); return rows

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    # the alert bar reads the same open generation as the master table
    rows = dry_overlay.paint_alerts(rows, dry_overlay.load_preview(c))
    c.close()
    out = []
    for r in rows:
        if r["expiry"] <= today:
            r["level"] = "expired"
            out.append(r)
        else:
            # simple day diff via fromisoformat
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")
        lots = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (body.item_id,))]
        result = consume_fefo(lots, body.qty)
        if not result["ok"] and result["reason"] == "qty_non_positive":
            c.rollback(); c.close(); raise HTTPException(400, result["reason"])
        if not result["ok"]:
            c.rollback(); c.close(); raise HTTPException(409, result)
        for d in result["deductions"]:
            c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 0:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        c.commit()
    except HTTPException:
        raise
    except Exception:
        c.rollback()
        c.close()
        raise
    c.close(); return result

def _sweep_candidates(c, today: str) -> list[dict]:
    """Truth at read time: still on_shelf, with stock, expiry strictly before today."""
    return [dict(r) for r in c.execute(
        """SELECT lots.id, lots.item_id, lots.qty_remain, lots.expiry, items.name, items.layer
           FROM lots JOIN items ON items.id=lots.item_id
           WHERE lots.status='on_shelf' AND lots.qty_remain>0
             AND lots.expiry IS NOT NULL AND lots.expiry < ?""", (today,))]

@app.get("/api/expire-sweep/preview")
def expire_sweep_preview():
    """Dry run: read-only on lots. Lists lots expired before today that are
    still on the shelf, and opens a generation (the pinned id list) that the
    master table / layer pages / alert bar all render until commit or cancel.
    Listed lots are 待下架, NOT yet expired."""
    today = date.today().isoformat()
    c = connect()
    lots = _sweep_candidates(c, today)
    dry_overlay.save_preview(c, today, [l["id"] for l in lots])
    c.close()
    for l in lots:
        l["sweep_pending"] = True
    return {"as_of": today, "lots": lots}

@app.delete("/api/expire-sweep/preview")
def expire_sweep_preview_cancel():
    """Abandon the open generation: overlay clears, all views return to the
    base generation together."""
    c = connect()
    dry_overlay.clear_preview(c)
    c.commit(); c.close()
    return {"ok": True}

class SweepIn(BaseModel):
    ids: list[int] | None = None

@app.post("/api/expire-sweep")
def expire_sweep(body: SweepIn | None = None):
    """Commit exactly the pinned dry-run generation — never a recompute.

    The list swept is the one the dry run listed and the master table showed
    as 待下架; a lot inbound'ed (already expired) after the dry run is NOT
    taken — it waits for the next generation. Pinned ids are revalidated
    inside one IMMEDIATE transaction (still on_shelf, qty_remain>0, expiry
    strictly before today), so a lot consumed meanwhile is left alone and an
    unexpired lot can never be taken. The generation is cleared in the SAME
    transaction, so any failure rolls lots + overlay back together and every
    view returns to the pre-commit generation as one. With the generation
    consumed, a second submit gets 409 — an already expired lot is never
    rewritten (the UPDATE's status guard is the backstop).
    """
    today = date.today().isoformat()
    c = connect()
    c.isolation_level = None  # explicit transaction control
    try:
        c.execute("BEGIN IMMEDIATE")
        gen = dry_overlay.load_preview(c)
        if gen is None:
            c.rollback()
            raise HTTPException(409, "no_open_preview")
        pinned = gen["ids"]
        if body and body.ids is not None and set(body.ids) != pinned:
            c.rollback()
            raise HTTPException(409, "generation_mismatch")
        sweepable = {l["id"] for l in _sweep_candidates(c, today)}
        take = sorted(pinned & sweepable)
        gone = sorted(pinned - sweepable)  # left the shelf meanwhile — untouched
        if take:
            marks = ",".join("?" for _ in take)
            c.execute(
                f"UPDATE lots SET status='expired' "
                f"WHERE id IN ({marks}) AND status='on_shelf' AND qty_remain>0",
                take)
        left_unswept = sorted(sweepable - pinned)  # reported, never taken
        dry_overlay.clear_preview(c)  # same tx: rollback restores the overlay too
        c.commit()
    except HTTPException:
        c.close(); raise
    except Exception:
        c.rollback(); c.close(); raise
    c.close()
    return {
        "as_of": today,
        "expired_ids": take,
        "gone_after_preview": gone,
        "left_unswept": left_unswept,
    }

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
