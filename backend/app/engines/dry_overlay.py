KEY = "sweep_preview_ids"

def save_preview(c, ids: list) -> None:
    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
              (KEY, ",".join(str(i) for i in ids)))
    c.commit()

def load_preview_ids(c) -> set:
    row = c.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    if not row or not row["value"]:
        return set()
    return {int(x) for x in str(row["value"]).split(",") if x}

def paint_fridge(rows: list, preview_ids: set) -> list:
    out = []
    for r in rows:
        d = dict(r)
        if int(d.get("id") or 0) in preview_ids:
            d["status"] = "expired"
            d["overlay_expired"] = True
        out.append(d)
    return out

def paint_alerts(rows: list, preview_ids: set) -> list:
    return [r for r in rows if int(r.get("id") or 0) not in preview_ids]


def _copy_lot(lot: dict) -> dict:
    return dict(lot)

def _qty(lot: dict) -> float:
    return float(lot.get("qty_remain") or 0)

def _lot_id(lot: dict) -> int:
    return int(lot.get("id") or 0)

def _on_shelf(lot: dict) -> bool:
    return str(lot.get("status") or "") == "on_shelf"

def _is_clean(lot: dict) -> bool:
    return str(lot.get("data_quality") or "clean") == "clean"

def _filter_shelf(rows: list) -> list:
    return [r for r in rows if _on_shelf(r)]

def _sum_remain(rows: list) -> float:
    return sum(_qty(r) for r in rows)

def _index_by_id(rows: list) -> dict:
    return {_lot_id(r): r for r in rows if r.get("id") is not None}
