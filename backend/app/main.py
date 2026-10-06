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
    # Dry-run paint shares one pinned generation with the alert bar; the real
    # status column is never rewritten, so the shelf stays factually on-shelf.
    snap = dry_overlay.load_generation(c)
    rows = dry_overlay.paint_fridge(rows, snap)
    c.close(); return rows

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    # Same pinned generation as /fridge: lots listed for removal leave the
    # alert bar together with their will_sweep paint on the shelf pages.
    snap = dry_overlay.load_generation(c)
    rows = dry_overlay.paint_alerts(rows, snap)
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
    """Dry-run generation: still on_shelf, with stock, expiry strictly before today."""
    return [dict(r) for r in c.execute(
        """SELECT lots.id, lots.item_id, lots.qty_remain, lots.expiry, items.name, items.layer
           FROM lots JOIN items ON items.id=lots.item_id
           WHERE lots.status='on_shelf' AND lots.qty_remain>0
             AND lots.expiry IS NOT NULL AND lots.expiry < ?""", (today,))]

def _preview_payload(snap: dict | None, today: str, lots: list[dict]) -> dict:
    return {
        "generation": snap["gen"] if snap else None,
        "as_of": snap["as_of"] if snap else today,
        "pinned": snap is not None,
        "lots": lots,
    }

@app.get("/api/expire-sweep/preview")
def expire_sweep_preview():
    """Dry run: read-only with respect to lots.

    Computes the list as of today and pins it as a new generation (ids + as_of
    + monotonic generation number) in one IMMEDIATE transaction. Shelf pages
    and the alert bar then read exactly this generation; no lot changes state.
    """
    today = date.today().isoformat()
    c = connect()
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")
        lots = _sweep_candidates(c, today)
        prev = c.execute(
            "SELECT value FROM settings WHERE key=?", (dry_overlay.GEN_KEY,)).fetchone()
        gen = dry_overlay.next_generation(prev["value"] if prev else None)
        dry_overlay.save_generation(c, gen, today, [l["id"] for l in lots])
        c.commit()
    except Exception:
        c.rollback(); c.close(); raise
    snap = dry_overlay.load_generation(c)
    c.close()
    return _preview_payload(snap, today, lots)

@app.delete("/api/expire-sweep/preview")
def expire_sweep_cancel():
    """Cancel the dry run: drop the pinned generation so views return to fact."""
    c = connect()
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")
        dry_overlay.clear_generation(c)
        c.commit()
    except Exception:
        c.rollback(); c.close(); raise
    c.close()
    return {"pinned": False, "generation": None}

class SweepIn(BaseModel):
    generation: int | None = None

@app.post("/api/expire-sweep")
def expire_sweep(body: SweepIn | None = None):
    """Commit the sweep against the PINNED dry-run list, not a fresh recompute.

    - 钉死干跑那一列：干跑后新写入的已过期批不属于本世代，本次不带走；
      未到期批永远不会进名单。期间被消费完的批由 on_shelf/qty>0 守卫跳过，
      不会误标。
    - generation 必须与当前钉死世代一致，否则 409（名单已被重新干跑或已提交）。
    - UPDATE 带 status='on_shelf' AND qty_remain>0 守卫，二次提交不会把
      已过期行再写一遍；清世代与写行在同一事务，任何失败整体回滚，
      总表 / 分层页 / 紧急条一起回到提交前。
    """
    today = date.today().isoformat()
    c = connect()
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")
        snap = dry_overlay.load_generation(c)
        if snap is None:
            c.rollback(); c.close()
            raise HTTPException(409, "no_pinned_preview")
        if body is not None and body.generation is not None and body.generation != snap["gen"]:
            c.rollback(); c.close()
            raise HTTPException(409, "generation_mismatch")

        pinned_ids = sorted(snap["ids"])
        skipped = []
        if pinned_ids:
            marks = ",".join("?" for _ in pinned_ids)
            # Snapshot the pinned lots' commit-time state so the response can
            # say which ones actually left; the UPDATE itself enforces the guards.
            pinned_rows = [dict(r) for r in c.execute(
                f"""SELECT id, status, qty_remain FROM lots
                    WHERE id IN ({marks})""", pinned_ids)]
            state = {r["id"]: r for r in pinned_rows}
            c.execute(
                f"UPDATE lots SET status='expired' "
                f"WHERE id IN ({marks}) AND status='on_shelf' AND qty_remain>0",
                pinned_ids)
            expired_ids = sorted(
                i for i in pinned_ids
                if state.get(i) and state[i]["status"] == "on_shelf" and state[i]["qty_remain"] > 0)
            skipped = sorted(i for i in pinned_ids if i not in set(expired_ids))
        else:
            expired_ids = []

        dry_overlay.clear_generation(c)
        c.commit()
    except HTTPException:
        raise
    except Exception:
        c.rollback(); c.close(); raise
    c.close()
    return {
        "as_of": snap["as_of"],
        "generation": snap["gen"],
        "committed_today": today,
        "expired_ids": expired_ids,
        "skipped_not_on_shelf": skipped,
    }

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
