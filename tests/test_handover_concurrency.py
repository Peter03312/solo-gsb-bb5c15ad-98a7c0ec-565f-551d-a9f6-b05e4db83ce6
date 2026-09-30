"""交接并发赛跑：确认 / 撤回 / 见证同意 按已提交顺序产生唯一结果。

使用真实 uvicorn 服务（后台线程）+ 多线程 httpx 同时发起请求。
"""
import datetime as dt
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])
from tests.test_concurrency import _free_port, _reg, _digest  # noqa: E402

import uvicorn  # noqa: E402


def _serve(monkeypatch, tmp_path, interval="1"):
    db = tmp_path / "handover-live.db"
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", interval)
    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    end = time.time() + 10
    while time.time() < end:
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("server did not start")
    return base, server, thread


def _iso(minutes: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()


def _stop(server, thread):
    server.should_exit = True
    thread.join(timeout=5)


def _prepare_doc(base, c, *, with_request=False, votes=0):
    owner = _reg(c, base, f"own-{threading.get_ident()}-{id(base)}")
    return owner


def test_concurrent_confirm_vs_revoke_single_outcome(tmp_path, monkeypatch):
    base, server, thread = _serve(monkeypatch, tmp_path)
    try:
        with httpx.Client(timeout=10) as c:
            owner = _reg(c, base, "hc-owner")
            w1 = _reg(c, base, "hc-w1")
            w2 = _reg(c, base, "hc-w2")
            w3 = _reg(c, base, "hc-w3")
            new_owner = _reg(c, base, "hc-new")
            body = "交接并发正文 CONFIRM-REVOKE-SECRET"
            doc_id = c.post(base + "/documents", json={"body": body, "witnesses": ["hc-w1", "hc-w2", "hc-w3"]},
                            headers={"X-API-Key": owner}).json()["document_id"]
            inv_id = c.post(f"{base}/documents/{doc_id}/handovers",
                            json={"invitee": "hc-new", "deadline": _iso(30)},
                            headers={"X-API-Key": owner}).json()["invitation_id"]

        barrier = threading.Barrier(2)

        def confirm():
            barrier.wait()
            with httpx.Client(timeout=10) as c:
                return c.post(f"{base}/handovers/{inv_id}/confirm",
                              headers={"X-API-Key": new_owner}).status_code

        def revoke():
            barrier.wait()
            with httpx.Client(timeout=10) as c:
                return c.post(f"{base}/handovers/{inv_id}/revoke", json={"reason": "赛跑"},
                              headers={"X-API-Key": owner}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            cf_code, rv_code = list(pool.map(lambda f: f(), [confirm, revoke]))

        # 唯一已提交结果：200 恰好出现一次
        assert sorted([cf_code, rv_code]) != [200, 200]
        assert 200 in (cf_code, rv_code)

        with httpx.Client(timeout=10) as c:
            if cf_code == 200:
                # 交接成功：撤回必败；owner 已变更，原 owner 无权
                assert rv_code == 409
                assert c.get(f"{base}/documents/{doc_id}", headers={"X-API-Key": owner}).status_code == 403
                got = c.get(f"{base}/documents/{doc_id}", headers={"X-API-Key": new_owner}).json()
                assert got["status"] == "sealed" and "body" not in got
                # 再来一次确认也必败（已确认）
                assert c.post(f"{base}/handovers/{inv_id}/confirm",
                              headers={"X-API-Key": new_owner}).status_code == 409
            else:
                # 撤回成功：确认必败，owner 未变
                assert cf_code == 409 and rv_code == 200
                assert c.get(f"{base}/documents/{doc_id}", headers={"X-API-Key": owner}).status_code == 200
                assert c.get(f"{base}/documents/{doc_id}",
                             headers={"X-API-Key": new_owner}).status_code == 403
            records = c.get(f"{base}/documents/{doc_id}/audit",
                            headers={"X-API-Key": owner if cf_code != 200 else new_owner}).json()["records"]
            assert len({r["action"] for r in records} & {"handover_confirmed", "handover_revoked"}) == 1
    finally:
        _stop(server, thread)


def test_concurrent_double_confirm_only_one_transfer(tmp_path, monkeypatch):
    base, server, thread = _serve(monkeypatch, tmp_path)
    try:
        with httpx.Client(timeout=10) as c:
            owner = _reg(c, base, "dc-owner")
            for n in ("dc-w1", "dc-w2", "dc-w3"):
                _reg(c, base, n)
            new_owner = _reg(c, base, "dc-new")
            body = "双重确认正文 DOUBLE-CONFIRM-SECRET"
            doc_id = c.post(base + "/documents",
                            json={"body": body, "witnesses": ["dc-w1", "dc-w2", "dc-w3"]},
                            headers={"X-API-Key": owner}).json()["document_id"]
            inv_id = c.post(f"{base}/documents/{doc_id}/handovers",
                            json={"invitee": "dc-new", "deadline": _iso(30)},
                            headers={"X-API-Key": owner}).json()["invitation_id"]

        barrier = threading.Barrier(2)

        def do_confirm():
            barrier.wait()
            with httpx.Client(timeout=10) as c:
                return c.post(f"{base}/handovers/{inv_id}/confirm",
                              headers={"X-API-Key": new_owner}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(lambda _: do_confirm(), range(2)))

        assert sorted(codes) == [200, 409]
        with httpx.Client(timeout=10) as c:
            got = c.get(f"{base}/documents/{doc_id}", headers={"X-API-Key": new_owner}).json()
            assert got["latest_handover"]["status"] == "confirmed"
            records = c.get(f"{base}/documents/{doc_id}/audit",
                            headers={"X-API-Key": new_owner}).json()["records"]
            assert [r["action"] for r in records].count("handover_confirmed") == 1
    finally:
        _stop(server, thread)


def test_concurrent_confirm_vs_second_approval_single_outcome(tmp_path, monkeypatch):
    """确认交接 与 第二票同意 赛跑：要么交接成功旧请求 superseded（未开封），
    要么两票开封交接失败，二者只产生一个已提交终态。"""
    base, server, thread = _serve(monkeypatch, tmp_path)
    try:
        with httpx.Client(timeout=10) as c:
            owner = _reg(c, base, "ra-owner")
            w1 = _reg(c, base, "ra-w1")
            w2 = _reg(c, base, "ra-w2")
            w3 = _reg(c, base, "ra-w3")
            new_owner = _reg(c, base, "ra-new")
            body = "确认-同意赛跑正文 RACE-APPROVE-SECRET"
            dg = _digest(body)
            doc_id = c.post(base + "/documents",
                            json={"body": body, "witnesses": ["ra-w1", "ra-w2", "ra-w3"]},
                            headers={"X-API-Key": owner}).json()["document_id"]
            req_id = c.post(f"{base}/documents/{doc_id}/open-requests",
                            json={"reason": "赛跑", "deadline": _iso(60)},
                            headers={"X-API-Key": owner}).json()["request_id"]
            c.post(f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
                   headers={"X-API-Key": w1})
            inv_id = c.post(f"{base}/documents/{doc_id}/handovers",
                            json={"invitee": "ra-new", "deadline": _iso(60)},
                            headers={"X-API-Key": owner}).json()["invitation_id"]

        barrier = threading.Barrier(2)

        def confirm():
            barrier.wait()
            with httpx.Client(timeout=10) as c:
                return c.post(f"{base}/handovers/{inv_id}/confirm",
                              headers={"X-API-Key": new_owner}).status_code

        def second_vote():
            barrier.wait()
            with httpx.Client(timeout=10) as c:
                return c.post(f"{base}/open-requests/{req_id}/approve", json={"digest": dg},
                              headers={"X-API-Key": w2}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            cf_code, ap_code = list(pool.map(lambda f: f(), [confirm, second_vote]))

        with httpx.Client(timeout=10) as c:
            if cf_code == 200:
                # 交接先提交：第二票必败（请求已 superseded），资料保持封存
                assert ap_code == 409
                got = c.get(f"{base}/documents/{doc_id}", headers={"X-API-Key": new_owner}).json()
                assert got["status"] == "sealed"
                assert got["latest_open_request"]["status"] == "superseded"
                assert "body" not in got
            else:
                # 第二票先提交：资料开封，交接必败
                assert ap_code == 200 and cf_code == 409
                got = c.get(f"{base}/documents/{doc_id}", headers={"X-API-Key": owner}).json()
                assert got["status"] == "opened" and got["body"] == body
                assert c.get(f"{base}/documents/{doc_id}",
                             headers={"X-API-Key": new_owner}).status_code == 403
    finally:
        _stop(server, thread)
