"""Sweep-preview generation overlay.

One open dry-run generation lives in settings as JSON {"as_of", "ids"}.
The master table, layer pages and the alert bar all read THIS list, so every
view renders the same generation. A lot in the generation is "待下架"
(sweep_pending) — still on_shelf in truth, never painted as already expired.
The generation is consumed (cleared) inside the sweep commit's transaction,
so a failed commit rolls the overlay back together with the lots.
"""
import json

KEY = "sweep_preview"


def save_preview(c, as_of: str, ids: list) -> None:
    """Open a new generation (replaces any previous one). Owns its commit."""
    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
              (KEY, json.dumps({"as_of": as_of, "ids": [int(i) for i in ids]})))
    c.commit()


def load_preview(c) -> dict | None:
    """The open generation, or None if no dry run is open (or after commit)."""
    row = c.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    if not row or not row["value"]:
        return None
    try:
        data = json.loads(row["value"])
        return {"as_of": data.get("as_of"),
                "ids": {int(i) for i in data.get("ids", [])}}
    except (ValueError, AttributeError, TypeError):
        return None


def clear_preview(c) -> None:
    """Consume the generation. NO commit here — callers fold this into their
    own transaction so a rollback restores the overlay with everything else."""
    c.execute("DELETE FROM settings WHERE key=?", (KEY,))


def _pending_ids(preview: dict | None) -> set:
    return preview["ids"] if preview else set()


def paint_fridge(rows: list, preview: dict | None) -> list:
    """Mark lots slated for removal as sweep_pending; status stays truthful."""
    ids = _pending_ids(preview)
    out = []
    for r in rows:
        d = dict(r)
        if int(d.get("id") or 0) in ids:
            d["sweep_pending"] = True
        out.append(d)
    return out


def paint_alerts(rows: list, preview: dict | None) -> list:
    """Lots owned by the open sweep generation leave the alert bar — they are
    accounted for by the dry-run card, so bar and table tell one story."""
    ids = _pending_ids(preview)
    return [r for r in rows if int(r.get("id") or 0) not in ids]
