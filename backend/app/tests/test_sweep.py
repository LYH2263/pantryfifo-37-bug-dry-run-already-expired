import os
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # reload in dependency order so every module binds the temp-DATA_DIR connect()
    import importlib
    import app.db as db
    import app.seed as seed
    import app.engines.dry_overlay as dry_overlay  # noqa: F401
    import app.main as main
    importlib.reload(db)
    importlib.reload(seed)
    importlib.reload(main)
    with TestClient(main.app) as cl:
        # drop seed lots/items (seed ships already-expired lots); tests insert their own
        c = db.connect()
        c.execute("DELETE FROM lots"); c.execute("DELETE FROM items"); c.commit()
        c.close()
        yield cl


def _today(): return date.today().isoformat()
def _past(days=2): return (date.today() - timedelta(days=days)).isoformat()
def _future(days=2): return (date.today() + timedelta(days=days)).isoformat()


def _add_item(client, name="测试品", layer="upper", unit="个"):
    c = _conn(client)
    cur = c.execute("INSERT INTO items(name,layer,unit) VALUES (?,?,?)", (name, layer, unit))
    c.commit(); iid = cur.lastrowid; c.close(); return iid


def _add_lot(client, item_id, qty, expiry, status="on_shelf"):
    c = _conn(client)
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (item_id, qty, qty, expiry, status, "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return lid


def _conn(client):
    import app.db as db
    return db.connect()


def _status(client, lot_id):
    c = _conn(client)
    r = c.execute("SELECT status,qty_remain FROM lots WHERE id=?", (lot_id,)).fetchone()
    c.close(); return r["status"], r["qty_remain"]


def _pinned_ids(client):
    c = _conn(client)
    row = c.execute("SELECT value FROM settings WHERE key='sweep_preview_ids'").fetchone()
    c.close()
    return None if row is None or not row["value"] else {int(x) for x in row["value"].split(",") if x}


# ---- 干跑：只列名单，不改集合；三视图同一世代 ----

def test_preview_lists_without_touching_status(client):
    iid = _add_item(client)
    old = _add_lot(client, iid, 3, _past())
    fresh = _add_lot(client, iid, 2, _future(10))

    r = client.get("/api/expire-sweep/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["pinned"] is True and body["generation"] == 1
    assert [l["id"] for l in body["lots"]] == [old]

    # 真实集合未动：仍是在架
    assert _status(client, old) == ("on_shelf", 3.0)
    assert _status(client, fresh) == ("on_shelf", 2.0)

    # 总表：真实 status 仍在架，只多 will_sweep 画皮
    fridge = {x["id"]: x for x in client.get("/api/fridge").json()}
    assert fridge[old]["status"] == "on_shelf" and fridge[old]["will_sweep"] is True
    assert fridge[fresh]["will_sweep"] is False


def test_alert_bar_shares_generation_with_shelf(client):
    iid = _add_item(client)
    old = _add_lot(client, iid, 3, _past())
    soon = _add_lot(client, iid, 2, _future(2))  # warn_days=3 → 紧急条

    alerts_before = {a["id"] for a in client.get("/api/alerts").json()}
    assert {old, soon} <= alerts_before

    client.get("/api/expire-sweep/preview")
    alerts_during = {a["id"] for a in client.get("/api/alerts").json()}
    # 钉死批离开紧急条，未到期/临期批不受影响
    assert old not in alerts_during and soon in alerts_during

    # 取消预览后紧急条回到事实
    assert client.delete("/api/expire-sweep/preview").status_code == 200
    alerts_after = {a["id"] for a in client.get("/api/alerts").json()}
    assert old in alerts_after


def test_unexpired_lot_never_pinned(client):
    iid = _add_item(client)
    today_lot = _add_lot(client, iid, 1, _today())       # 到期=今天，不算过期
    future_lot = _add_lot(client, iid, 1, _future(1))
    body = client.get("/api/expire-sweep/preview").json()
    pinned = {l["id"] for l in body["lots"]}
    assert today_lot not in pinned and future_lot not in pinned


# ---- 提交：钉死干跑那一列 ----

def test_commit_uses_pinned_list_not_recompute(client):
    iid = _add_item(client)
    old = _add_lot(client, iid, 3, _past())
    pv = client.get("/api/expire-sweep/preview").json()
    gen = pv["generation"]

    # 干跑后才入库、且已过期：不属于本世代，本次不得带走
    late = _add_lot(client, iid, 1, _past(5))

    r = client.post("/api/expire-sweep", json={"generation": gen})
    assert r.status_code == 200
    out = r.json()
    assert out["expired_ids"] == [old]
    assert _status(client, old) == ("expired", 3.0)
    assert _status(client, late) == ("on_shelf", 1.0)
    # 世代已消费：名单两键清掉（世代计数器只增不清）
    assert _pinned_ids(client) is None

    # 总表（on_shelf 过滤）不再有 old；late 还在且没有画皮
    fridge = {x["id"]: x for x in client.get("/api/fridge").json()}
    assert old not in fridge and fridge[late]["will_sweep"] is False


def test_pinned_lot_consumed_meanwhile_is_skipped_not_expired(client):
    iid = _add_item(client)
    old = _add_lot(client, iid, 1, _past())
    _add_lot(client, iid, 2, _future(5))
    gen = client.get("/api/expire-sweep/preview").json()["generation"]

    # 干跑与提交之间，该批被 FEFO 扣减打完
    cr = client.post("/api/consume", json={"item_id": iid, "qty": 1})
    assert cr.status_code == 200
    assert _status(client, old) == ("consumed", 0.0)

    out = client.post("/api/expire-sweep", json={"generation": gen}).json()
    assert out["expired_ids"] == []
    assert out["skipped_not_on_shelf"] == [old]
    # 被扣减打完的批不得被打成过期
    assert _status(client, old) == ("consumed", 0.0)


def test_second_commit_is_idempotent_and_rejected(client):
    iid = _add_item(client)
    old = _add_lot(client, iid, 2, _past())
    gen = client.get("/api/expire-sweep/preview").json()["generation"]
    assert client.post("/api/expire-sweep", json={"generation": gen}).status_code == 200

    # 已无钉死名单：二次提交被拒，不会把已过期行再写一遍
    r = client.post("/api/expire-sweep", json={"generation": gen})
    assert r.status_code == 409 and r.json()["detail"] == "no_pinned_preview"
    assert _status(client, old) == ("expired", 2.0)


def test_stale_generation_rejected_and_nothing_committed(client):
    iid = _add_item(client)
    old = _add_lot(client, iid, 2, _past())
    g1 = client.get("/api/expire-sweep/preview").json()["generation"]
    g2 = client.get("/api/expire-sweep/preview").json()["generation"]
    assert g2 == g1 + 1

    r = client.post("/api/expire-sweep", json={"generation": g1})
    assert r.status_code == 409 and r.json()["detail"] == "generation_mismatch"
    # 旧世代提交无效：批仍在架，当前钉死名单仍在
    assert _status(client, old) == ("on_shelf", 2.0)
    assert _pinned_ids(client) == {old}

    assert client.post("/api/expire-sweep", json={"generation": g2}).status_code == 200
    assert _status(client, old) == ("expired", 2.0)


def test_commit_failure_rolls_everything_back(client, monkeypatch):
    import app.main as main
    iid = _add_item(client)
    old = _add_lot(client, iid, 2, _past())
    gen = client.get("/api/expire-sweep/preview").json()["generation"]

    # 让清世代那一步在事务内炸掉：UPDATE 与清世代必须一起回滚
    def boom(c):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(main.dry_overlay, "clear_generation", boom)

    with pytest.raises(Exception):
        client.post("/api/expire-sweep", json={"generation": gen})

    # 无半份过期：批回到在架，名单仍钉在本世代，三视图仍是提交前那一世代
    assert _status(client, old) == ("on_shelf", 2.0)
    assert _pinned_ids(client) == {old}
    fridge = {x["id"]: x for x in client.get("/api/fridge").json()}
    assert fridge[old]["status"] == "on_shelf" and fridge[old]["will_sweep"] is True
    alerts = {a["id"] for a in client.get("/api/alerts").json()}
    assert old not in alerts


def test_generation_monotonic_and_cancel_clears_paint(client):
    iid = _add_item(client)
    old = _add_lot(client, iid, 1, _past())
    g1 = client.get("/api/expire-sweep/preview").json()["generation"]
    client.delete("/api/expire-sweep/preview")
    g2 = client.get("/api/expire-sweep/preview").json()["generation"]
    assert g2 == g1 + 1
    client.delete("/api/expire-sweep/preview")
    fridge = {x["id"]: x for x in client.get("/api/fridge").json()}
    assert fridge[old]["will_sweep"] is False
