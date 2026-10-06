"""Pinned dry-run generation for the expire sweep.

干跑只「列出将收走的批号」，不碰 lots 集合：

- 世代存在 settings 三个键里（sweep_gen / sweep_as_of / sweep_preview_ids）。
- save/clear 都不自行 commit，必须由调用方包进事务——提交失败时，
  名单世代与 lots 的真实写回一起回滚，不会留下半份过期。
- 画皮只打 will_sweep 标记，绝不改 status：干跑页看到的总表真实状态
  仍是在架；紧急条（alerts）按同一套名单过滤，三处同属一个世代。
"""

GEN_KEY = "sweep_gen"
AS_OF_KEY = "sweep_as_of"
IDS_KEY = "sweep_preview_ids"


def save_generation(c, gen: int, as_of: str, ids: list) -> None:
    """Pin the dry-run list. Caller owns the transaction (no commit here)."""
    c.executemany(
        "INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
        [
            (GEN_KEY, str(gen)),
            (AS_OF_KEY, as_of),
            (IDS_KEY, ",".join(str(i) for i in ids)),
        ],
    )


def clear_generation(c) -> None:
    """Drop the pinned list. Caller owns the transaction (no commit).

    The monotonic generation counter is deliberately kept: a stale client
    holding an old generation number must not match a freshly pinned list.
    """
    c.execute("DELETE FROM settings WHERE key IN (?,?)", (AS_OF_KEY, IDS_KEY))


def load_generation(c) -> dict | None:
    """Return {gen, as_of, ids} of the pinned generation, or None."""
    rows = {r["key"]: r["value"] for r in c.execute(
        "SELECT key,value FROM settings WHERE key IN (?,?,?)", (GEN_KEY, AS_OF_KEY, IDS_KEY))}
    raw_ids = rows.get(IDS_KEY)
    if GEN_KEY not in rows or raw_ids is None:
        return None
    try:
        gen = int(rows[GEN_KEY])
    except (TypeError, ValueError):
        return None
    ids = {int(x) for x in str(raw_ids).split(",") if x}
    return {"gen": gen, "as_of": rows.get(AS_OF_KEY) or "", "ids": ids}


def next_generation(prev) -> int:
    """Monotonic generation number; 1 when no previous sweep existed."""
    try:
        return int(prev) + 1
    except (TypeError, ValueError):
        return 1


def paint_fridge(rows: list, snap: dict | None) -> list:
    """Mark pinned lots with will_sweep. Status stays the real on-shelf state."""
    ids = snap["ids"] if snap else set()
    out = []
    for r in rows:
        d = dict(r)
        d["will_sweep"] = int(d.get("id") or 0) in ids
        out.append(d)
    return out


def paint_alerts(rows: list, snap: dict | None) -> list:
    """Drop pinned lots from the alert bar: they are leaving in this generation."""
    ids = snap["ids"] if snap else set()
    if not ids:
        return [dict(r) for r in rows]
    return [dict(r) for r in rows if int(r.get("id") or 0) not in ids]
