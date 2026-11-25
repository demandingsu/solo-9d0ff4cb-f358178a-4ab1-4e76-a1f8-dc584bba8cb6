"""班次报送端到端试跑（标准库即可运行，依赖 fastapi/uvicorn 在 PYTHONPATH 中）。

用法（项目根目录）：
    PYTHONPATH=/tmp/pylibs:. python3 tests/run_shift_e2e.py

会自启真实 uvicorn 子进程（端口可用 PORT 覆盖）与独立临时 SQLite 库，覆盖：
  1. 北京时间月界拆分、回执行号/分钟数；班次工时参与供数与纠错；
  2. 修订语义：原样重报返回原回执、旧号/同号不同内容退回、失败保留原数据；
  3. 行号固定：移出月后补回仍用原号；手工占用保留号整批退回；
  4. 整月覆盖只替换手工工时；有班次工时的月份拒绝清空；
  5. 跨月修订 × 手工覆盖完整交错场景；
  6. 并发报送：相同内容全部返回同一回执，同号异内容恰好一成一败；
  7. 入参校验与接口别名、/docs、/openapi.json。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

PORT = os.environ.get("PORT", "8123")
BASE = os.environ.get("BASE_URL", f"http://127.0.0.1:{PORT}")
DB_PATH = os.environ.get("APP_DB_PATH", f"/tmp/shift-e2e-{os.getpid()}.db")
PASS = "[PASS]"
FAIL = "[FAIL]"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = PASS if cond else FAIL
    print(f"  {mark} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode()), {
                k.lower(): v for k, v in resp.headers.items()
            }
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), {
            k.lower(): v for k, v in e.headers.items()
        }


def wait_ready(proc: subprocess.Popen) -> None:
    for _ in range(60):
        if proc.poll() is not None:
            raise RuntimeError("服务进程提前退出")
        try:
            status, _, _ = call("GET", "/health")
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("服务未在 30s 内就绪")


def line_of(receipt: dict, month: str) -> dict | None:
    for line in receipt.get("months", []):
        if line["month"] == month:
            return line
    return None


def supply_map(month: str) -> dict[int, dict]:
    status, body, _ = call("GET", f"/months/{month}/supply")
    assert status == 200, body
    return {x["work_row_id"]: x for x in body["suppliable"] + body["withheld"]}


# ---------------------------------------------------------------------------
# 场景 1：月界拆分 + 供数 + 纠错
# ---------------------------------------------------------------------------
def scenario_split_supply_correct() -> None:
    print("场景 1：跨月拆分、供数与纠错")
    for m in ("2026-10", "2026-11"):
        call("DELETE", f"/months/{m}")

    status, r, _ = call("POST", "/shifts", {
        "shift_id": "S-E2E-1",
        "revision": 1,
        "work_order": "WO-SHIFT",
        "start_at": "2026-10-31T22:00",
        "end_at": "2026-11-01T06:00",
    })
    check("报送跨月班次 200", status == 200, f"{status} {r}")
    check("总分钟数 480（8 小时）", r.get("total_minutes") == 480, str(r.get("total_minutes")))
    oct_line = line_of(r, "2026-10")
    nov_line = line_of(r, "2026-11")
    check("10 月拆分 120 分钟、行号 1",
          oct_line and oct_line["minutes"] == 120 and oct_line["row_id"] == 1, str(oct_line))
    check("11 月拆分 360 分钟、行号 1",
          nov_line and nov_line["minutes"] == 360 and nov_line["row_id"] == 1, str(nov_line))
    check("回执行带 work_row_id 别名且等于 row_id",
          oct_line and oct_line.get("work_row_id") == oct_line["row_id"], str(oct_line))

    oct_rid = oct_line["row_id"]
    nov_rid = nov_line["row_id"]

    rows = supply_map("2026-10")
    check("供数视图含 10 月班次工时 120 分钟",
          rows.get(oct_rid, {}).get("minutes") == 120
          and rows[oct_rid]["work_order"] == "WO-SHIFT", str(rows.get(oct_rid)))
    rows_n = supply_map("2026-11")
    check("供数视图含 11 月班次工时 360 分钟",
          rows_n.get(nov_rid, {}).get("minutes") == 360, str(rows_n.get(nov_rid)))

    # 给班次工单造两条路径 -> 重复扣留 -> 用班次行号纠错
    call("POST", "/months/2026-10/dispatches", {"rows": [
        {"row_id": 1, "work_order": "WO-SHIFT", "project": "P1"},
        {"row_id": 2, "work_order": "WO-SHIFT", "project": "P2"},
    ]})
    call("POST", "/months/2026-10/attributions", {"rows": [
        {"row_id": 1, "project": "P1", "budget_subject": "SUBJ-S1"},
        {"row_id": 2, "project": "P2", "budget_subject": "SUBJ-S2"},
    ]})
    rows = supply_map("2026-10")
    check("班次工时出现双路径扣留", rows.get(oct_rid, {}).get("path_count") == 2, str(rows.get(oct_rid)))

    status, r, _ = call("POST", "/months/2026-10/corrections", {
        "work_row_id": oct_rid, "remove": "dispatch", "remove_row_id": 2,
    })
    check("对班次工时行纠错成功 200", status == 200, f"{status} {r}")
    check("恢复行号就是班次行号", r.get("restored_work_row_ids") == [oct_rid], str(r))
    rows = supply_map("2026-10")
    check("纠错后班次行可供数且科目 SUBJ-S1",
          rows.get(oct_rid, {}).get("budget_subject") == "SUBJ-S1"
          and oct_rid in {x["work_row_id"] for x in r["suppliable"]}, str(rows.get(oct_rid)))


# ---------------------------------------------------------------------------
# 场景 2：修订与幂等/冲突规则
# ---------------------------------------------------------------------------
def scenario_revisions() -> None:
    print("场景 2：修订号规则、原样重报、失败保留")
    body1 = {
        "shift_id": "S-REV", "revision": 1, "work_order": "WO-REV",
        "start_at": "2026-05-10T08:00", "end_at": "2026-05-10T16:00",
    }
    status, r1, h1 = call("POST", "/shifts", body1)
    check("首次报送 200", status == 200, f"{status} {r1}")
    may = line_of(r1, "2026-05")
    check("5 月单月 480 分钟", may and may["minutes"] == 480, str(may))

    # 原样重报（时间写成空格分隔，语义相同）
    replay_body = dict(body1, start_at="2026-05-10 08:00", end_at="2026-05-10 16:00")
    status, r1b, hb = call("POST", "/shifts", replay_body)
    check("最新版原样重报 200", status == 200, f"{status}")
    check("重报响应体与原回执完全一致", r1b == r1, "回执不一致")
    check("重报带 X-Idempotent-Replay 头", hb.get("x-idempotent-replay") == "true", str(hb))

    # 同号不同内容
    bad = dict(body1, end_at="2026-05-10T17:00")
    status, rbad, _ = call("POST", "/shifts", bad)
    check("同号不同内容退回 409", status == 409, f"{status} {rbad}")
    rows = supply_map("2026-05")
    check("退回后原工时仍是 480 分钟", rows[may["row_id"]]["minutes"] == 480, str(rows[may["row_id"]]))
    status, got, _ = call("GET", "/shifts/S-REV")
    check("退回后查询仍为修订 1、结束 16:00",
          status == 200 and got["revision"] == 1 and got["end_at"] == "2026-05-10T16:00", str(got)[:200])

    # 旧号
    body2 = dict(body1, revision=2, end_at="2026-05-10T17:00")
    status, r2, _ = call("POST", "/shifts", body2)
    check("更新修订 2 成功", status == 200 and line_of(r2, "2026-05")["minutes"] == 540, f"{status} {r2}")
    status, old, _ = call("POST", "/shifts", body1)
    check("旧修订号 1 被拒 409", status == 409, f"{status} {old}")
    rows = supply_map("2026-05")
    check("旧号拒绝后工时保持修订 2 的 540 分钟",
          rows[may["row_id"]]["minutes"] == 540, str(rows[may["row_id"]]))

    # 允许跳号到更新的修订
    body5 = dict(body1, revision=5, start_at="2026-05-11T08:00", end_at="2026-05-11T10:00")
    status, r5, _ = call("POST", "/shifts", body5)
    check("更新的跳号修订 5 接受", status == 200, f"{status} {r5}")
    status, old2, _ = call("POST", "/shifts", body2)
    check("此后修订 2 沦为旧号被拒", status == 409, f"{status} {old2}")
    rows = supply_map("2026-05")
    check("跳号修订后工时为 120 分钟且行号不变",
          rows[may["row_id"]]["minutes"] == 120, str(rows[may["row_id"]]))

    # 校验类
    status, _, _ = call("POST", "/shifts", dict(body1, shift_id="S-BAD", revision=0))
    check("修订号 0 返回 422", status == 422, str(status))
    status, r, _ = call("POST", "/shifts", {
        "shift_id": "S-TIME1", "revision": 1, "work_order": "W",
        "start_at": "2026-06-02T10:00", "end_at": "2026-06-02T09:00"})
    check("结束早于开始返回 422", status == 422, f"{status} {r}")
    status, r, _ = call("POST", "/shifts", {
        "shift_id": "S-TIME2", "revision": 1, "work_order": "W",
        "start_at": "2026-06-01T00:00", "end_at": "2026-06-02T00:01"})
    check("超过 24 小时返回 422", status == 422, f"{status} {r}")
    status, r, _ = call("POST", "/shifts", {
        "shift_id": "S-24H", "revision": 1, "work_order": "W",
        "start_at": "2026-06-01T00:00", "end_at": "2026-06-02T00:00"})
    check("恰好 24 小时接受", status == 200 and line_of(r, "2026-06")["minutes"] == 1440, f"{status} {r}")
    status, r, _ = call("POST", "/shifts", {
        "shift_id": "S-TIME3", "revision": 1, "work_order": "W",
        "start_at": "not-a-time", "end_at": "2026-06-02T00:00"})
    check("无法解析的时刻返回 422", status == 422, f"{status}")

    # 带时区的时刻换算到北京时间月界
    status, r, _ = call("POST", "/shifts", {
        "shift_id": "S-TZ", "revision": 1, "work_order": "W",
        "start_at": "2026-03-31T23:30:00+08:00",
        "end_at": "2026-04-01T00:30:00+08:00"})
    check("+08:00 时刻按北京月界 30/30 拆分",
          status == 200
          and line_of(r, "2026-03") and line_of(r, "2026-03")["minutes"] == 30
          and line_of(r, "2026-04") and line_of(r, "2026-04")["minutes"] == 30, str(r))


# ---------------------------------------------------------------------------
# 场景 3：固定行号 + 手工覆盖交错（核心）
# ---------------------------------------------------------------------------
def scenario_sticky_manual_interleave() -> None:
    print("场景 3：跨月修订 × 手工覆盖交错")
    call("DELETE", "/months/2026-07")
    call("DELETE", "/months/2026-08")

    # 修订 1：跨 7/8 月
    status, r1, _ = call("POST", "/shifts", {
        "shift_id": "S-MIX", "revision": 1, "work_order": "WO-MIX",
        "start_at": "2026-07-31T23:00", "end_at": "2026-08-01T01:00"})
    check("S-MIX rev1 报送 200", status == 200, f"{status} {r1}")
    j = line_of(r1, "2026-07")
    a = line_of(r1, "2026-08")
    check("7 月 60 分钟行号 1", j and j["minutes"] == 60 and j["row_id"] == 1, str(j))
    check("8 月 60 分钟行号 1", a and a["minutes"] == 60 and a["row_id"] == 1, str(a))
    jid, aid = j["row_id"], a["row_id"]

    # 先录一条普通手工行 50（追加）
    status, r, _ = call("POST", "/months/2026-07/work-hours", {
        "rows": [{"row_id": 50, "work_order": "WO-M50", "minutes": 10}], "replace": False})
    check("手工追加行 50 成功", status == 200, f"{status} {r}")

    # 混用保留行号 -> 整批退回（含同批 51 也不得写入）
    status, r, _ = call("POST", "/months/2026-07/work-hours", {
        "rows": [
            {"row_id": jid, "work_order": "WO-HACK", "minutes": 5},
            {"row_id": 51, "work_order": "WO-M51", "minutes": 7},
        ],
        "replace": False,
    })
    check("占用班次保留行号整批退回（4xx）", status in (409, 422), f"{status} {r}")
    rows = supply_map("2026-07")
    check("退回后班次行内容不变（60/WO-MIX）",
          rows.get(jid, {}).get("minutes") == 60 and rows[jid]["work_order"] == "WO-MIX",
          str(rows.get(jid)))
    check("整批退回：同批行 51 未写入", 51 not in rows, str(sorted(rows)))

    # 整月覆盖为手工行 7：只替换手工，班次行保留；旧手工 50 被替换掉
    status, r, _ = call("POST", "/months/2026-07/work-hours", {
        "rows": [{"row_id": 7, "work_order": "WO-M7", "minutes": 30}], "replace": True})
    check("整月覆盖手工工时 200", status == 200, f"{status} {r}")
    rows = supply_map("2026-07")
    check("覆盖后班次行仍在且为 60 分钟", rows.get(jid, {}).get("minutes") == 60, str(rows.get(jid)))
    check("覆盖后新手工行 7 在", rows.get(7, {}).get("minutes") == 30, str(rows.get(7)))
    check("覆盖只替换手工：旧手工行 50 已删除", 50 not in rows, str(sorted(rows)))

    # replace=true 的批次若含保留号：先整批拒绝，已有手工行 7 必须保留
    status, r, _ = call("POST", "/months/2026-07/work-hours", {
        "rows": [
            {"row_id": jid, "work_order": "WO-HACK2", "minutes": 1},
            {"row_id": 8, "work_order": "WO-M8", "minutes": 2},
        ],
        "replace": True,
    })
    check("覆盖批次含保留号被退回", status in (409, 422), f"{status} {r}")
    rows = supply_map("2026-07")
    check("退回后原手工行 7 仍在（未先删后败）", rows.get(7, {}).get("minutes") == 30, str(rows.get(7)))
    check("退回后批次行 8 未写入", 8 not in rows, str(sorted(rows)))
    check("退回后班次行仍在", rows.get(jid, {}).get("minutes") == 60, str(rows.get(jid)))

    # 修订 2：班次完全移到 8 月另一时段 -> 7 月工时行删除，8 月仍用原号 aid
    status, r2, _ = call("POST", "/shifts", {
        "shift_id": "S-MIX", "revision": 2, "work_order": "WO-MIX",
        "start_at": "2026-08-10T09:00", "end_at": "2026-08-10T12:00"})
    check("S-MIX rev2 成功", status == 200, f"{status} {r2}")
    check("rev2 只剩 8 月一行 180 分钟",
          len(r2["months"]) == 1 and r2["months"][0]["month"] == "2026-08"
          and r2["months"][0]["minutes"] == 180, str(r2["months"]))
    check("8 月行号固定为原号", r2["months"][0]["row_id"] == aid, str(r2["months"][0]))
    rows_j = supply_map("2026-07")
    check("移出后 7 月不再有班次工时",
          all(x["work_order"] != "WO-MIX" for x in rows_j.values()), str(sorted(rows_j)))
    check("移出不影响手工行 7", rows_j.get(7, {}).get("minutes") == 30, str(rows_j.get(7)))

    # 7 月原号仍被保留：手工依旧不能用
    status, r, _ = call("POST", "/months/2026-07/work-hours", {
        "rows": [{"row_id": jid, "work_order": "WO-HACK3", "minutes": 9}], "replace": False})
    check("移出该月后原行号仍禁止手工占用", status in (409, 422), f"{status} {r}")

    # 有班次工时的 8 月拒绝清空
    status, r, _ = call("DELETE", "/months/2026-08")
    check("有班次工时的月份拒绝清空 409", status == 409, f"{status} {r}")
    rows_a = supply_map("2026-08")
    check("拒绝清空后 8 月班次工时仍在 180", rows_a.get(aid, {}).get("minutes") == 180, str(rows_a.get(aid)))

    # 7 月已无当前班次工时 -> 允许清空（保留号映射仍在）
    status, r, _ = call("DELETE", "/months/2026-07")
    check("无当前班次工时的 7 月允许清空", status == 200, f"{status} {r}")

    # 修订 3：补回 7 月 -> 仍用最初的行号 jid
    status, r3, _ = call("POST", "/shifts", {
        "shift_id": "S-MIX", "revision": 3, "work_order": "WO-MIX",
        "start_at": "2026-07-15T09:00", "end_at": "2026-07-15T11:00"})
    check("S-MIX rev3 补回 7 月成功", status == 200, f"{status} {r3}")
    j3 = line_of(r3, "2026-07")
    check("补回 7 月仍用原行号且 120 分钟",
          j3 and j3["row_id"] == jid and j3["minutes"] == 120, str(j3))
    status, _, _ = call("DELETE", "/months/2026-07")
    check("补回后 7 月再次拒绝清空", status == 409, str(status))


# ---------------------------------------------------------------------------
# 场景 4：并发报送
# ---------------------------------------------------------------------------
def scenario_concurrency() -> None:
    print("场景 4：并发报送串行化")
    n = 6
    results: list[tuple[int, dict]] = []
    barrier = threading.Barrier(n)

    def same_worker() -> None:
        barrier.wait()
        code, body, _ = call("POST", "/shifts", {
            "shift_id": "S-CONC", "revision": 1, "work_order": "WO-C",
            "start_at": "2026-03-10T08:00", "end_at": "2026-03-10T20:00"})
        results.append((code, body))

    threads = [threading.Thread(target=same_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(c for c, _ in results)
    check("6 个相同首报并发全部 200", codes == [200] * n, str(codes))
    receipts = {json.dumps(b.get("months"), sort_keys=True) for _, b in results}
    check("6 个响应回执月份/行号/分钟完全一致", len(receipts) == 1, str(receipts))

    # 同号不同内容的 rev2 竞争：恰好一成一败
    race: list[tuple[int, dict]] = []
    barrier2 = threading.Barrier(2)

    def diff_worker(wo: str) -> None:
        barrier2.wait()
        code, body, _ = call("POST", "/shifts", {
            "shift_id": "S-CONC", "revision": 2, "work_order": wo,
            "start_at": "2026-03-11T08:00", "end_at": "2026-03-11T09:00"})
        race.append((code, body))

    t1 = threading.Thread(target=diff_worker, args=("WO-A",))
    t2 = threading.Thread(target=diff_worker, args=("WO-B",))
    t1.start(); t2.start(); t1.join(); t2.join()
    ok = [(c, b) for c, b in race if c == 200]
    bad = [(c, b) for c, b in race if c != 200]
    check("同号异内容并发恰好 1 成 1 败", len(ok) == 1 and len(bad) == 1, str([c for c, _ in race]))
    check("失败方给出中文原因且为 409", bad and bad[0][0] == 409 and bool(bad[0][1].get("detail")),
          str(bad[0][0] if bad else None))

    status, got, _ = call("GET", "/shifts/S-CONC")
    check("最终修订为 2", status == 200 and got["revision"] == 2, str(got.get("revision")))
    winner_wo = ok[0][1]["work_order"]
    check("最终工单号与获胜事务一致", got["work_order"] == winner_wo,
          f"{got.get('work_order')} != {winner_wo}")
    rows = supply_map("2026-03")
    march = line_of(got, "2026-03")
    check("供数中该班次数值与获胜回执一致（60 分钟）",
          march and rows[march["row_id"]]["minutes"] == 60
          and rows[march["row_id"]]["work_order"] == winner_wo, str(march))

    # 3 个相同 rev3 并发 -> 全部 200 且同回执（后到者按最新版重放）
    results3: list[tuple[int, dict]] = []
    barrier3 = threading.Barrier(3)

    def same3() -> None:
        barrier3.wait()
        results3.append(call("POST", "/shifts", {
            "shift_id": "S-CONC", "revision": 3, "work_order": "WO-C3",
            "start_at": "2026-03-12T08:00", "end_at": "2026-03-12T10:00"})[:2])

    ts = [threading.Thread(target=same3) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("相同 rev3 三路并发全部 200", sorted(c for c, _ in results3) == [200, 200, 200],
          str([c for c, _ in results3]))
    check("相同 rev3 回执一致",
          len({json.dumps(b, sort_keys=True) for _, b in results3}) == 1, "回执不一致")


# ---------------------------------------------------------------------------
# 场景 5：别名、查询接口与 /docs
# ---------------------------------------------------------------------------
def scenario_aliases_docs() -> None:
    print("场景 5：接口/字段别名、查询与文档")
    status, r, _ = call("POST", "/shift-reports", {
        "shift_no": "S-ALIAS", "revision_no": 1, "work_order": "WO-ALIAS",
        "start_time": "2026-02-01T00:00", "end_time": "2026-02-01T02:00"})
    check("POST /shift-reports + 别名字段 200", status == 200, f"{status} {r}")
    check("规范字段 shift_id 正确回显", r.get("shift_id") == "S-ALIAS", str(r.get("shift_id")))
    check("rows 别名与 months 内容一致", r.get("rows") == r.get("months"), "rows != months")

    status, r, _ = call("PUT", "/shifts/S-PUT", {
        "revision": 1, "work_order": "WO-PUT",
        "start_at": "2026-04-01T00:00", "end_at": "2026-04-01T01:00"})
    check("PUT /shifts/{id} 路径带班次号 200", status == 200 and r.get("shift_id") == "S-PUT",
          f"{status} {str(r)[:200]}")

    status, r, _ = call("POST", "/months/2026-09/shifts", {
        "shift_id": "S-MONTHALIAS", "revision": 1, "work_order": "W",
        "start_at": "2026-09-01T00:00", "end_at": "2026-09-01T01:00"})
    check("月份路径别名 200", status == 200, f"{status} {r}")

    status, r, _ = call("GET", "/shifts/S-ALIAS")
    check("GET /shifts/{id} 200 且含修订历史",
          status == 200 and r.get("revision") == 1 and len(r.get("revisions", [])) == 1, str(r)[:200])
    status, r, _ = call("GET", "/shifts/NO-SUCH-SHIFT")
    check("未知班次 GET 返回 404 中文说明", status == 404 and bool(r.get("detail")), f"{status} {r}")
    status, r, _ = call("GET", "/shifts")
    check("GET /shifts 列表含多个班次", status == 200 and r.get("total", 0) >= 5, str(r.get("total")))

    for path in ("/docs", "/openapi.json", "/health"):
        try:
            with urllib.request.urlopen(BASE + path, timeout=10) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        check(f"GET {path} 200", code == 200, str(code))


def main() -> int:
    # 独立临时库，删掉历史文件保证“新月第一行=1”等分配断言成立
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        **os.environ,
        "APP_DB_PATH": DB_PATH,
        "PYTHONPATH": root + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", PORT],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_ready(proc)
        scenario_split_supply_correct()
        scenario_revisions()
        scenario_sticky_manual_interleave()
        scenario_concurrency()
        scenario_aliases_docs()
    finally:
        proc.terminate()
        try:
            out = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.wait(timeout=10)
        if failures:
            tail = "\n".join(proc.stdout.read().decode(errors="replace").splitlines()[-30:])
            print("\n--- uvicorn 末尾日志 ---\n" + tail)

    print()
    if failures:
        print(f"{FAIL} 共 {len(failures)} 项失败: {failures}")
        return 1
    print(f"{PASS} 班次全部断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
