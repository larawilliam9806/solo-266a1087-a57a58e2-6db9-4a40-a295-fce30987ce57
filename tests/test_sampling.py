"""抽页方案 API 的端到端测试。

覆盖：入参校验、暂停供数拦截、共用页上限、最少选页连通、无法连通分组、
按页固化件号，以及查询时三类不可用复核（暂停供数 / 组内拆页 / 两组合页）。
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def link(client, evidence_id, a, b):
    r = client.post(
        "/same-page-links",
        json={"evidence_id": evidence_id, "scan_a": a, "scan_b": b},
    )
    assert r.status_code in (200, 201), r.text
    return r


def unlink(client, evidence_id):
    r = client.delete(f"/same-page-links/{evidence_id}")
    assert r.status_code == 200, r.text
    return r


def register_training(client, scan_id):
    r = client.post("/training-scans", json={"scan_id": scan_id})
    assert r.status_code in (200, 201), r.text
    return r


def publish(client, items):
    r = client.post("/manifests", json={"items": items})
    assert r.status_code == 201, r.text
    return r.json()["manifest_id"]


def create_plan(client, manifest_ids, expected=201):
    r = client.post("/sampling-plans", json={"manifest_ids": manifest_ids})
    assert r.status_code == expected, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------

def test_reject_wrong_count(client):
    m1 = publish(client, ["A1"])
    r = client.post("/sampling-plans", json={"manifest_ids": [m1]})
    assert r.status_code == 422

    ids = [publish(client, [f"S{i}"]) for i in range(7)]
    r = client.post("/sampling-plans", json={"manifest_ids": ids})
    assert r.status_code == 422


def test_reject_duplicate_or_blank_manifest_ids(client):
    m1 = publish(client, ["A1"])
    m2 = publish(client, ["B1"])
    r = client.post(
        "/sampling-plans", json={"manifest_ids": [m1, m1, m2]}
    )
    assert r.status_code == 422
    r = client.post(
        "/sampling-plans", json={"manifest_ids": [m1, "  "]}
    )
    assert r.status_code == 422


def test_unknown_manifest_404(client):
    m1 = publish(client, ["A1"])
    body = create_plan(client, [m1, "MFT-NOPE"], expected=404)
    assert body["missing_manifest_ids"] == ["MFT-NOPE"]


def test_suspended_manifest_rejected(client):
    # m1 发布后因新同页判定暂停供数
    m1 = publish(client, ["A1"])
    m2 = publish(client, ["B1"])
    link(client, "E1", "A1", "B1")  # 先建立共用页
    register_training(client, "T1")
    link(client, "E2", "A1", "T1")  # 两批同时进入暂停
    body = create_plan(client, [m1, m2], expected=409)
    assert body["suspended_manifest_ids"] == [m1, m2]
    # 恢复供数后可正常抽页
    unlink(client, "E2")
    body = create_plan(client, [m1, m2])
    assert body["status"] == "ready"


# ---------------------------------------------------------------------------
# 共用页上限
# ---------------------------------------------------------------------------

def test_shared_pages_over_limit_asks_narrow_range(client):
    a_items = [f"A{i}" for i in range(15)]
    b_items = [f"B{i}" for i in range(15)]
    m1 = publish(client, a_items)
    m2 = publish(client, b_items)
    for i in range(15):  # 15 个互不相连的共用原页
        link(client, f"E{i}", f"A{i}", f"B{i}")
    body = create_plan(client, [m1, m2], expected=400)
    assert body["shared_page_count"] == 15
    assert body["limit"] == 14
    assert "缩小范围" in body["detail"]
    # 未产生任何方案
    assert client.get("/sampling-plans/PLN-UNKNOWN").status_code == 404


# ---------------------------------------------------------------------------
# 无法连通：返回各自连通的批次组
# ---------------------------------------------------------------------------

def test_infeasible_returns_connected_groups(client):
    m1 = publish(client, ["A1", "A2"])
    m2 = publish(client, ["B1"])
    m3 = publish(client, ["C1"])
    link(client, "E1", "A1", "B1")  # 仅 m1 与 m2 共用一页，m3 孤立
    body = create_plan(client, [m1, m2, m3], expected=409)
    assert body["groups"] == [[m1, m2], [m3]]
    # 失败不落方案
    assert client.get("/sampling-plans/PLN-UNKNOWN").status_code == 404


def test_no_shared_page_at_all(client):
    m1 = publish(client, ["A1"])
    m2 = publish(client, ["B1"])
    body = create_plan(client, [m1, m2], expected=409)
    assert body["groups"] == [[m1], [m2]]


# ---------------------------------------------------------------------------
# 最少选页连通
# ---------------------------------------------------------------------------

def test_single_shared_page_connecting_all(client):
    m1 = publish(client, ["A1"])
    m2 = publish(client, ["B1"])
    m3 = publish(client, ["C1"])
    link(client, "E1", "A1", "B1")
    link(client, "E2", "B1", "C1")  # 三批同属一个原页
    body = create_plan(client, [m1, m2, m3])
    assert len(body["pages"]) == 1
    page = body["pages"][0]
    assert {p["manifest_id"] for p in page["picks"]} == {m1, m2, m3}
    assert {p["scan_id"] for p in page["picks"]} == {"A1", "B1", "C1"}


def test_minimum_connector_on_chain_with_decoys(client):
    # 页 X、Z 都只覆盖 {m1,m2}，页 Y 覆盖 {m2,m3}：连通三批至少要 2 页
    m1 = publish(client, ["A1", "A2"])
    m2 = publish(client, ["B1", "B2", "B3"])
    m3 = publish(client, ["C1"])
    link(client, "E1", "A1", "B1")  # 页 X = {m1,m2}
    link(client, "E2", "A2", "B2")  # 页 Z = {m1,m2}（诱饵：选它也连不上 m3）
    link(client, "E3", "B3", "C1")  # 页 Y = {m2,m3}
    body = create_plan(client, [m1, m2, m3])
    assert len(body["pages"]) == 2  # 不会选 3 页
    # 每张选中页的批次并集应覆盖全部三批，且整体连通
    covered = set()
    for page in body["pages"]:
        covered |= {p["manifest_id"] for p in page["picks"]}
    assert covered == {m1, m2, m3}
    # 含 m3 的页必被选中（它是唯一桥）
    assert any(
        {p["manifest_id"] for p in page["picks"]} == {m2, m3}
        for page in body["pages"]
    )


def test_picks_one_per_manifest_and_smallest_scan(client):
    # 同批两件在同一原页内时，取件号最小者
    m1 = publish(client, ["A9", "A1"])  # 去重保序存储，选号取最小
    m2 = publish(client, ["B2", "B1"])
    link(client, "E1", "A9", "A1")
    link(client, "E2", "A1", "B1")
    link(client, "E3", "B1", "B2")
    body = create_plan(client, [m1, m2])
    assert len(body["pages"]) == 1
    picks = body["pages"][0]["picks"]
    by_manifest = {p["manifest_id"]: p["scan_id"] for p in picks}
    assert by_manifest == {m1: "A1", m2: "B1"}
    # 入参顺序回显
    assert body["manifest_ids"] == [m1, m2]


# ---------------------------------------------------------------------------
# 持久化：凭编号查到当初选的件号
# ---------------------------------------------------------------------------

def test_plan_persisted_and_queryable(client):
    m1 = publish(client, ["A1"])
    m2 = publish(client, ["B1"])
    m3 = publish(client, ["C1"])
    link(client, "E1", "A1", "B1")
    link(client, "E2", "B1", "C1")
    created = create_plan(client, [m1, m2, m3])
    plan_id = created["plan_id"]
    assert plan_id.startswith("PLN-")

    got = client.get(f"/sampling-plans/{plan_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["status"] == "ready"
    assert body["manifest_ids"] == [m1, m2, m3]
    assert body["pages"] == created["pages"]
    assert "unavailable_reasons" not in body


def test_unknown_plan_404(client):
    assert client.get("/sampling-plans/PLN-UNKNOWN").status_code == 404


# ---------------------------------------------------------------------------
# 查询复核：三类不可用情形
# ---------------------------------------------------------------------------

def test_query_marks_unavailable_when_manifest_suspended(client):
    m1 = publish(client, ["A1"])
    m2 = publish(client, ["B1"])
    link(client, "E1", "A1", "B1")
    plan_id = create_plan(client, [m1, m2])["plan_id"]
    snapshot = client.get(f"/sampling-plans/{plan_id}").json()["pages"]

    # 来源清单暂停供数 -> 方案标不可用，但当初件号仍可查
    register_training(client, "T1")
    link(client, "E2", "A1", "T1")
    body = client.get(f"/sampling-plans/{plan_id}").json()
    assert body["status"] == "unavailable"
    reason_codes = {r["code"] for r in body["unavailable_reasons"]}
    assert reason_codes == {"manifest_suspended"}
    assert set(body["unavailable_reasons"][0]["manifest_ids"]) == {m1, m2}
    assert body["pages"] == snapshot  # 固化快照不变

    # 恢复供数 -> 方案重新可用
    unlink(client, "E2")
    body = client.get(f"/sampling-plans/{plan_id}").json()
    assert body["status"] == "ready"
    assert body["pages"] == snapshot


def test_query_marks_unavailable_when_picks_split_into_pages(client):
    # 原页内：A 批 A1/A2，B 批 B1；边 E2(A2,B1) 是唯一桥
    m1 = publish(client, ["A1", "A2"])
    m2 = publish(client, ["B1"])
    link(client, "E1", "A1", "A2")
    link(client, "E2", "A2", "B1")
    plan_id = create_plan(client, [m1, m2])["plan_id"]
    assert client.get(f"/sampling-plans/{plan_id}").json()["status"] == "ready"

    # 撤回桥接判定 -> A1 与 B1 分属不同原页 -> 组内件号分属不同页
    unlink(client, "E2")
    body = client.get(f"/sampling-plans/{plan_id}").json()
    assert body["status"] == "unavailable"
    reason_codes = {r["code"] for r in body["unavailable_reasons"]}
    assert reason_codes == {"picks_page_split"}
    # 当初选中的件号仍可查
    scans = {p["scan_id"] for p in body["pages"][0]["picks"]}
    assert scans == {"A1", "B1"}


def test_query_marks_unavailable_when_two_pages_merge(client):
    # 三批链式：页 X={m1,m2}、页 Y={m2,m3}，最少必须选 2 页
    m1 = publish(client, ["A1"])
    m2 = publish(client, ["B1", "B2"])
    m3 = publish(client, ["C1"])
    link(client, "E1", "A1", "B1")  # 原页 X
    link(client, "E2", "B2", "C1")  # 原页 Y，与 X 互不相连
    plan_id = create_plan(client, [m1, m2, m3])["plan_id"]
    assert len(client.get(f"/sampling-plans/{plan_id}").json()["pages"]) == 2

    # 新判定把两页合到同一原页
    link(client, "E3", "B1", "B2")
    body = client.get(f"/sampling-plans/{plan_id}").json()
    assert body["status"] == "unavailable"
    reason_codes = {r["code"] for r in body["unavailable_reasons"]}
    assert reason_codes == {"pages_merged"}
    assert body["unavailable_reasons"][0]["page_indices"] == [0, 1]


def test_multiple_unavailable_reasons_reported_together(client):
    # 同时触发拆页与暂停供数，两类原因都应返回
    m1 = publish(client, ["A1", "A2"])
    m2 = publish(client, ["B1"])
    link(client, "E1", "A1", "A2")
    link(client, "E2", "A2", "B1")
    plan_id = create_plan(client, [m1, m2])["plan_id"]

    register_training(client, "T1")
    link(client, "E3", "T1", "B1")  # 暂停供数（同页，不拆分量）
    unlink(client, "E2")            # 同时拆页
    body = client.get(f"/sampling-plans/{plan_id}").json()
    assert body["status"] == "unavailable"
    assert {r["code"] for r in body["unavailable_reasons"]} == {
        "manifest_suspended",
        "picks_page_split",
    }
