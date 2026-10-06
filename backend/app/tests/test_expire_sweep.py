"""Expire-sweep generation consistency.

The dry run pins a generation (id list); master table, layer pages and the
alert bar render that same generation as 待下架 without touching the truth
(still on_shelf). Commit sweeps exactly the pinned list — revalidated, in one
transaction, overlay cleared in the same tx — and a second submit is refused.
"""
import os
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import connect
from app.engines import dry_overlay

YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
LONG_AGO = (date.today() - timedelta(days=30)).isoformat()
FUTURE = (date.today() + timedelta(days=30)).isoformat()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app, raise_server_exceptions=False) as cl:
        c = connect()
        c.execute("DELETE FROM lots")
        c.execute("DELETE FROM settings WHERE key=?", (dry_overlay.KEY,))
        c.commit(); c.close()
        yield cl


def add_lot(client, item_id=1, qty=2, expiry=YESTERDAY):
    r = client.post("/api/lots", json={"item_id": item_id, "qty": qty, "expiry": expiry})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def lot_row(lot_id):
    c = connect()
    r = c.execute("SELECT status, qty_remain FROM lots WHERE id=?", (lot_id,)).fetchone()
    c.close()
    return dict(r) if r else None


def fridge_row(client, lot_id):
    for r in client.get("/api/fridge").json():
        if r["id"] == lot_id:
            return r
    return None


def test_preview_lists_without_mutating(client):
    a = add_lot(client, expiry=YESTERDAY)
    b = add_lot(client, expiry=FUTURE)
    r = client.get("/api/expire-sweep/preview")
    assert r.status_code == 200
    ids = [l["id"] for l in r.json()["lots"]]
    assert ids == [a]  # unexpired lot is never listed
    # dry run must not fake-expire: truth is still on_shelf, marked 待下架 only
    row = fridge_row(client, a)
    assert row["status"] == "on_shelf" and row["sweep_pending"] is True
    assert fridge_row(client, b).get("sweep_pending") is None
    assert lot_row(a)["status"] == "on_shelf"


def test_alert_bar_shares_the_generation(client):
    a = add_lot(client, expiry=YESTERDAY)
    assert any(x["id"] == a for x in client.get("/api/alerts").json())
    client.get("/api/expire-sweep/preview")
    # preview owns A now: the bar stops shouting about it (same generation)
    assert not any(x["id"] == a for x in client.get("/api/alerts").json())
    # a lot inbound'ed after the dry run belongs to no generation: bar shows it
    c_lot = add_lot(client, expiry=LONG_AGO)
    assert any(x["id"] == c_lot for x in client.get("/api/alerts").json())


def test_commit_pins_to_preview_list(client):
    a = add_lot(client, expiry=YESTERDAY)
    client.get("/api/expire-sweep/preview")
    c_lot = add_lot(client, expiry=LONG_AGO)  # inbound after the dry run
    r = client.post("/api/expire-sweep", json={"ids": [a]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["expired_ids"] == [a]
    assert body["left_unswept"] == [c_lot]  # reported, never taken
    assert lot_row(a)["status"] == "expired"
    assert lot_row(c_lot)["status"] == "on_shelf"
    # generation consumed: overlay gone from the master table
    assert fridge_row(client, c_lot).get("sweep_pending") is None


def test_commit_never_takes_unexpired(client):
    a = add_lot(client, expiry=YESTERDAY)
    b = add_lot(client, expiry=FUTURE)
    client.get("/api/expire-sweep/preview")
    # forged list containing the unexpired lot: whole commit refused
    r = client.post("/api/expire-sweep", json={"ids": [a, b]})
    assert r.status_code == 409
    assert lot_row(a)["status"] == "on_shelf"
    assert lot_row(b)["status"] == "on_shelf"
    # honest commit of the pinned list leaves the unexpired lot alone
    r = client.post("/api/expire-sweep", json={"ids": [a]})
    assert r.status_code == 200
    assert lot_row(b)["status"] == "on_shelf"
    assert lot_row(b)["qty_remain"] == 2


def test_second_commit_rejected_nothing_rewritten(client):
    a = add_lot(client, expiry=YESTERDAY)
    client.get("/api/expire-sweep/preview")
    assert client.post("/api/expire-sweep", json={"ids": [a]}).status_code == 200
    # generation consumed: replay gets 409 and the expired row is not rewritten
    r = client.post("/api/expire-sweep", json={"ids": [a]})
    assert r.status_code == 409 and r.json()["detail"] == "no_open_preview"
    assert lot_row(a)["status"] == "expired"
    c = connect()
    n = c.execute("SELECT COUNT(*) n FROM lots WHERE status='expired'").fetchone()["n"]
    c.close()
    assert n == 1


def test_commit_without_preview_rejected(client):
    a = add_lot(client, expiry=YESTERDAY)
    r = client.post("/api/expire-sweep", json={"ids": [a]})
    assert r.status_code == 409
    assert lot_row(a)["status"] == "on_shelf"


def test_consumed_between_preview_and_commit(client):
    a = add_lot(client, qty=2, expiry=YESTERDAY)
    client.get("/api/expire-sweep/preview")
    r = client.post("/api/consume", json={"item_id": 1, "qty": 2})
    assert r.status_code == 200
    r = client.post("/api/expire-sweep", json={"ids": [a]})
    assert r.status_code == 200
    assert r.json()["expired_ids"] == []
    assert r.json()["gone_after_preview"] == [a]
    assert lot_row(a)["status"] == "consumed"  # not marked expired


def test_failure_rolls_everything_back(client, monkeypatch):
    a = add_lot(client, expiry=YESTERDAY)
    client.get("/api/expire-sweep/preview")

    def boom(c):
        raise RuntimeError("mid-commit failure")

    monkeypatch.setattr(dry_overlay, "clear_preview", boom)
    r = client.post("/api/expire-sweep", json={"ids": [a]})
    assert r.status_code == 500
    # lots and overlay return to the pre-commit generation together
    assert lot_row(a)["status"] == "on_shelf"
    row = fridge_row(client, a)
    assert row["status"] == "on_shelf" and row["sweep_pending"] is True
    assert not any(x["id"] == a for x in client.get("/api/alerts").json())


def test_cancel_preview_clears_generation(client):
    a = add_lot(client, expiry=YESTERDAY)
    client.get("/api/expire-sweep/preview")
    assert fridge_row(client, a)["sweep_pending"] is True
    client.delete("/api/expire-sweep/preview")
    row = fridge_row(client, a)
    assert row["status"] == "on_shelf" and row.get("sweep_pending") is None
    assert any(x["id"] == a for x in client.get("/api/alerts").json())
    assert client.post("/api/expire-sweep", json={"ids": [a]}).status_code == 409


def test_empty_preview_commits_as_noop(client):
    add_lot(client, expiry=FUTURE)
    client.get("/api/expire-sweep/preview")
    r = client.post("/api/expire-sweep", json={"ids": []})
    assert r.status_code == 200
    assert r.json()["expired_ids"] == []
