"""户数台账与配电班复盘清单测试。"""
from __future__ import annotations

import random
from datetime import datetime

import pytest

from app import core
from app.db import read_conn, read_snapshot, write_txn

DAY = "2026-11-23"


def E(iso: str) -> float:
    return core.to_epoch(datetime.fromisoformat(iso.replace("Z", "+00:00")))


def reg(area, ts, n):
    with write_txn() as conn:
        return core.upsert_households(conn, area, E(ts), n)


def create(no, sw, ts, st):
    with write_txn() as conn:
        return core.create_order(conn, no, sw, E(ts), st)


def update(no, ts, st):
    with write_txn() as conn:
        return core.update_order(conn, no, E(ts), st)


def delete(no):
    with write_txn() as conn:
        return core.delete_order(conn, no)


def review(area, rs, re_):
    with read_snapshot() as conn:
        return core.review_loss_periods(conn, area, E(rs), E(re_))


def records(area):
    with read_conn() as conn:
        return core.list_household_records(conn, area)


def outage_10_11():
    """T1/T2 在 [10:00, 11:00) 失电：10:00 拉开 K1，11:00 合上联络 K3 转供。"""
    create("D1", "K1", f"{DAY}T10:00:00Z", "断")
    create("D2", "K3", f"{DAY}T11:00:00Z", "通")


# ------------------------------------------------------------ 登记与同刻更正

def test_register_and_correct_same_timestamp(db):
    created, valid_until = reg("T1", f"{DAY}T08:00:00Z", 100)
    assert created is True and valid_until is None
    # 同刻更正：不是新登记
    created, valid_until = reg("T1", f"{DAY}T08:00:00Z", 150)
    assert created is False and valid_until is None
    # 同值幂等 no-op
    created, _ = reg("T1", f"{DAY}T08:00:00Z", 150)
    assert created is False
    assert records("T1") == [(E(f"{DAY}T08:00:00Z"), 150)]
    # 再补一笔，前一笔沿用到该时刻
    created, valid_until = reg("T1", f"{DAY}T09:00:00Z", 80)
    assert created is True
    assert records("T1") == [
        (E(f"{DAY}T08:00:00Z"), 150),
        (E(f"{DAY}T09:00:00Z"), 80),
    ]


def test_register_rejects_bad_input(db):
    with pytest.raises(core.ValidationError):
        reg("T9", f"{DAY}T08:00:00Z", 100)  # 台区不存在
    with pytest.raises(core.ValidationError):
        reg("T1", f"{DAY}T08:00:00Z", -1)  # 户数必须非负
    with pytest.raises(core.ValidationError):
        reg("T1", f"{DAY}T08:00:00Z", 1.5)  # 必须是整数


def test_correction_revalues_only_its_validity(db):
    reg("T1", f"{DAY}T09:00:00Z", 100)
    reg("T1", f"{DAY}T10:00:00Z", 200)
    reg("T1", f"{DAY}T11:00:00Z", 300)
    # 更正 10:00 那笔为 50：只影响 [10:00, 11:00) 有效期
    reg("T1", f"{DAY}T10:00:00Z", 50)
    with read_conn() as conn:
        f = lambda t: core.household_cum_at(conn, "T1", E(t))
        assert f(f"{DAY}T10:00:00Z") == 100 * 3600
        assert f(f"{DAY}T11:00:00Z") == 100 * 3600 + 50 * 3600
        assert f(f"{DAY}T12:00:00Z") == 100 * 3600 + 50 * 3600 + 300 * 3600
        assert f(f"{DAY}T08:00:00Z") == 0  # 首笔之前


def test_insert_before_first_record_shifts_cum(db):
    reg("T1", f"{DAY}T10:00:00Z", 100)
    reg("T1", f"{DAY}T09:00:00Z", 40)  # 插到首笔之前，成为新的首笔
    with read_conn() as conn:
        f = lambda t: core.household_cum_at(conn, "T1", E(t))
        assert f(f"{DAY}T09:00:00Z") == 0
        assert f(f"{DAY}T10:00:00Z") == 40 * 3600
        assert f(f"{DAY}T11:00:00Z") == 40 * 3600 + 100 * 3600


# ------------------------------------------------------------ 中断户秒与缺档秒数

def test_household_seconds_accumulate(db):
    outage_10_11()
    reg("T1", f"{DAY}T09:00:00Z", 100)  # 失电前登记，沿用到下一笔
    reg("T1", f"{DAY}T10:30:00Z", 50)
    # [10:00, 11:00)：100×1800 + 50×1800
    assert review("T1", f"{DAY}T00:00:00Z", "2026-11-24T00:00:00Z") == [
        (E(f"{DAY}T10:00:00Z"), E(f"{DAY}T11:00:00Z"), 270000.0, 0.0)
    ]


def test_missing_before_first_record(db):
    outage_10_11()
    # 没有任何台账：整段缺档
    assert review("T1", f"{DAY}T00:00:00Z", "2026-11-24T00:00:00Z") == [
        (E(f"{DAY}T10:00:00Z"), E(f"{DAY}T11:00:00Z"), 0.0, 3600.0)
    ]
    # 首笔在 10:10 生效：缺档 600 秒，其余按 100 户计
    reg("T1", f"{DAY}T10:10:00Z", 100)
    assert review("T1", f"{DAY}T00:00:00Z", "2026-11-24T00:00:00Z") == [
        (E(f"{DAY}T10:00:00Z"), E(f"{DAY}T11:00:00Z"), 300000.0, 600.0)
    ]


def test_zero_registration_counts_as_zero_not_missing(db):
    outage_10_11()
    reg("T1", f"{DAY}T09:00:00Z", 0)  # 登记为零就按零户，不算缺档
    assert review("T1", f"{DAY}T00:00:00Z", "2026-11-24T00:00:00Z") == [
        (E(f"{DAY}T10:00:00Z"), E(f"{DAY}T11:00:00Z"), 0.0, 0.0)
    ]


# ------------------------------------------------------------ 复电时间范围过滤

def two_outages():
    """T1 两段失电：[10:00, 11:00) 与 [12:00, 13:00)。"""
    outage_10_11()
    create("D3", "K3", f"{DAY}T12:00:00Z", "断")  # 拉开联络，T1 再失电
    create("D4", "K1", f"{DAY}T13:00:00Z", "通")  # 复电


def test_review_filters_on_restore_time(db):
    two_outages()
    reg("T1", f"{DAY}T08:00:00Z", 10)
    p1 = (E(f"{DAY}T10:00:00Z"), E(f"{DAY}T11:00:00Z"), 36000.0, 0.0)
    p2 = (E(f"{DAY}T12:00:00Z"), E(f"{DAY}T13:00:00Z"), 36000.0, 0.0)
    # 复电时刻起点计入、终点不计
    assert review("T1", f"{DAY}T11:00:00Z", f"{DAY}T13:00:00Z") == [p1]
    assert review("T1", f"{DAY}T11:00:00Z", f"{DAY}T13:00:01Z") == [p1, p2]
    assert review("T1", f"{DAY}T10:30:00Z", f"{DAY}T11:00:00Z") == []
    # 整段起止：范围从 10:30 起，段起点仍是 10:00，不裁剪
    assert review("T1", f"{DAY}T10:30:00Z", f"{DAY}T11:30:00Z") == [p1]


def test_ongoing_period_excluded(db):
    create("D1", "K1", f"{DAY}T10:00:00Z", "断")  # 未复电
    assert review("T1", f"{DAY}T00:00:00Z", "2026-11-24T00:00:00Z") == []


def test_review_rejects_bad_input(db):
    with pytest.raises(core.ValidationError):
        review("T9", f"{DAY}T00:00:00Z", "2026-11-24T00:00:00Z")  # 台区不存在
    with pytest.raises(core.ValidationError):
        review("T1", "2026-11-24T00:00:00Z", f"{DAY}T00:00:00Z")  # 起终点颠倒


# ------------------------------------------------------------ 变位单补改 / 撤销后清单刷新

def test_order_changes_refresh_review(db):
    two_outages()
    reg("T1", f"{DAY}T08:00:00Z", 10)
    full = (f"{DAY}T00:00:00Z", "2026-11-24T00:00:00Z")
    assert [p[:2] for p in review("T1", *full)] == [
        (E(f"{DAY}T10:00:00Z"), E(f"{DAY}T11:00:00Z")),
        (E(f"{DAY}T12:00:00Z"), E(f"{DAY}T13:00:00Z")),
    ]
    # 撤销 11:00 的转供：两段合并为 [10:00, 13:00)，旧段撤下
    delete("D2")
    assert [p[:2] for p in review("T1", *full)] == [
        (E(f"{DAY}T10:00:00Z"), E(f"{DAY}T13:00:00Z"))
    ]
    # 重新补录 11:00 转供：合并段重新拆分
    create("D5", "K3", f"{DAY}T11:00:00Z", "通")
    assert [p[:2] for p in review("T1", *full)] == [
        (E(f"{DAY}T10:00:00Z"), E(f"{DAY}T11:00:00Z")),
        (E(f"{DAY}T12:00:00Z"), E(f"{DAY}T13:00:00Z")),
    ]
    # 补改复电时刻 13:00 -> 13:30：整段终点随之移动
    update("D4", f"{DAY}T13:30:00Z", "通")
    assert [p[:2] for p in review("T1", *full)] == [
        (E(f"{DAY}T10:00:00Z"), E(f"{DAY}T11:00:00Z")),
        (E(f"{DAY}T12:00:00Z"), E(f"{DAY}T13:30:00Z")),
    ]
    # 撤销复电单：重新变为未复电，旧段从清单撤下
    delete("D4")
    assert [p[:2] for p in review("T1", *full)] == [
        (E(f"{DAY}T10:00:00Z"), E(f"{DAY}T11:00:00Z"))
    ]


# ------------------------------------------------------------ 事务一致性

def test_failed_household_write_keeps_committed(db):
    outage_10_11()
    reg("T1", f"{DAY}T09:00:00Z", 100)
    before = review("T1", f"{DAY}T00:00:00Z", "2026-11-24T00:00:00Z")
    with pytest.raises(RuntimeError):
        with write_txn() as conn:
            core.upsert_households(conn, "T1", E(f"{DAY}T09:30:00Z"), 999)
            raise RuntimeError("模拟写入失败")
    # 写入失败回滚，继续按已提交的数据查询
    assert review("T1", f"{DAY}T00:00:00Z", "2026-11-24T00:00:00Z") == before
    assert records("T1") == [(E(f"{DAY}T09:00:00Z"), 100)]


def test_review_uses_single_committed_snapshot(db):
    outage_10_11()
    reg("T1", f"{DAY}T09:00:00Z", 100)
    rs, re_ = E(f"{DAY}T00:00:00Z"), E("2026-11-24T00:00:00Z")
    with read_snapshot() as conn:
        before = core.review_loss_periods(conn, "T1", rs, re_)
        # 另一连接提交新数据：新户数 + 新失电
        with write_txn() as w:
            core.upsert_households(w, "T1", E(f"{DAY}T09:30:00Z"), 5000)
            core.create_order(w, "D9", "K5", E(f"{DAY}T12:00:00Z"), "断")
            core.create_order(w, "D10", "K5", E(f"{DAY}T13:00:00Z"), "通")
        after = core.review_loss_periods(conn, "T1", rs, re_)
    assert before == after  # 一次查询固定在同一个已提交状态
    with read_snapshot() as conn:  # 新查询可见新提交
        assert core.review_loss_periods(conn, "T1", rs, re_) != before


# ------------------------------------------------------------ 累计积分 vs 逐条扫描暴力对照

def brute_review(area, rs, re_):
    """参考实现：逐条扫描失电期间的户数记录。"""
    with read_conn() as conn:
        periods = conn.execute(
            "SELECT start_epoch, end_epoch FROM loss_periods "
            "WHERE area_id = ? AND end_epoch IS NOT NULL "
            "AND end_epoch >= ? AND end_epoch < ? ORDER BY start_epoch",
            (area, rs, re_),
        ).fetchall()
        recs = [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT start_epoch, households FROM household_records "
                "WHERE area_id = ? ORDER BY start_epoch",
                (area,),
            )
        ]
    out = []
    for s, e in periods:
        gap = max(0.0, min(e, recs[0][0]) - s) if recs else e - s
        hs = 0.0
        for i, (t, c) in enumerate(recs):
            t_next = recs[i + 1][0] if i + 1 < len(recs) else None
            lo, hi = max(s, t), (e if t_next is None else min(e, t_next))
            if lo < hi:
                hs += c * (hi - lo)
        out.append((s, e, hs, gap))
    return out


def test_review_matches_bruteforce(db):
    rng = random.Random(20261123)
    base = E("2026-11-20T00:00:00Z")
    areas = ["T1", "T2", "T3", "T4"]
    switches = ["K1", "K2", "K3", "K4", "K5"]
    for seq in range(60):  # 随机变位单
        try:
            with write_txn() as conn:
                core.create_order(
                    conn,
                    f"O{seq:03d}",
                    rng.choice(switches),
                    base + rng.randrange(0, 3 * 24 * 60) * 60.0,
                    rng.choice(["通", "断"]),
                )
        except core.ConflictError:
            continue
    for area in areas:  # 随机台账（含同刻更正与零户）
        for _ in range(rng.randrange(0, 12)):
            reg_t = base + rng.randrange(0, 3 * 24 * 60) * 60.0
            with write_txn() as conn:
                core.upsert_households(conn, area, reg_t, rng.randrange(0, 200))
    rs, re_ = base - 3600.0, base + 4 * 86400.0
    with read_snapshot() as conn:
        for area in areas:
            actual = core.review_loss_periods(conn, area, rs, re_)
            expected = brute_review(area, rs, re_)
            assert len(actual) == len(expected)
            for got, want in zip(actual, expected):
                assert got[0] == want[0] and got[1] == want[1]
                assert got[2] == pytest.approx(want[2])
                assert got[3] == pytest.approx(want[3])


# ------------------------------------------------------------ API 端到端

def test_api_households_and_review(client):
    # 登记
    r = client.post("/households", json={
        "area_id": "T1",
        "effective_time": "2026-11-23T09:00:00Z",
        "households": 100,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True and body["valid_until"] is None
    assert body["effective_time"] == "2026-11-23T09:00:00.000000Z"
    # 同刻更正
    r = client.post("/households", json={
        "area_id": "T1",
        "effective_time": "2026-11-23T09:00:00Z",
        "households": 120,
    })
    assert r.status_code == 200 and r.json()["created"] is False
    # 再补一笔：前一笔沿用到该时刻
    r = client.post("/households", json={
        "area_id": "T1",
        "effective_time": "2026-11-23T10:30:00Z",
        "households": 50,
    })
    assert r.json()["valid_until"] is None
    # 台账列表
    r = client.get("/households", params={"area_id": "T1"})
    assert r.json()["records"] == [
        {"effective_time": "2026-11-23T09:00:00.000000Z", "households": 120},
        {"effective_time": "2026-11-23T10:30:00.000000Z", "households": 50},
    ]
    # 非法输入
    assert client.post("/households", json={
        "area_id": "T1",
        "effective_time": "2026-11-23T09:00:00Z",
        "households": -1,
    }).status_code == 422
    assert client.post("/households", json={
        "area_id": "T9",
        "effective_time": "2026-11-23T09:00:00Z",
        "households": 1,
    }).status_code == 400

    # 失电 [10:00, 11:00)
    client.post("/orders", json={
        "order_no": "D1", "switch_id": "K1",
        "change_time": "2026-11-23T10:00:00Z", "state": "断",
    })
    client.post("/orders", json={
        "order_no": "D2", "switch_id": "K3",
        "change_time": "2026-11-23T11:00:00Z", "state": "通",
    })
    r = client.get("/review", params={
        "area_id": "T1",
        "start": "2026-11-23T00:00:00Z",
        "end": "2026-11-24T00:00:00Z",
    })
    assert r.status_code == 200
    assert r.json()["periods"] == [
        {
            "start": "2026-11-23T10:00:00.000000Z",
            "end": "2026-11-23T11:00:00.000000Z",
            "household_seconds": 120 * 1800 + 50 * 1800,
            "missing_seconds": 0,
        }
    ]
    # 范围与台区校验
    assert client.get("/review", params={
        "area_id": "T1",
        "start": "2026-11-24T00:00:00Z",
        "end": "2026-11-23T00:00:00Z",
    }).status_code == 400
    assert client.get("/review", params={
        "area_id": "T9",
        "start": "2026-11-23T00:00:00Z",
        "end": "2026-11-24T00:00:00Z",
    }).status_code == 400
