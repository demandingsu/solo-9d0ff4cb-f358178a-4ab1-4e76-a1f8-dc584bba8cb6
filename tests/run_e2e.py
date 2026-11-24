"""端到端试跑（标准库即可运行）。

用法（在项目根目录）：
    APP_DB_PATH=/tmp/test-maintenance.db \
    PYTHONPATH=/tmp/pylibs:. python3 -I tests/run_e2e.py

覆盖：
  A. 共享归科：两条分派共用一条归科，撤除归科会打掉其他可供数工时 -> 必须拒绝；
     同科目重复路径仍判重复；撤除正确分派后恢复供数，原有可供数工时不受影响。
  B. 并发纠错：两个独立重复工时同时撤除，串行化后各自成功；
     并发打同一工时只允许一个成功，另一个基于已提交数据失败，数据不被破坏。
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

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
MONTH = "2026-11"
PASS = "[PASS]"
FAIL = "[FAIL]"
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = PASS if cond else FAIL
    print(f"  {mark} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def wait_ready(proc: subprocess.Popen) -> None:
    for _ in range(60):
        if proc.poll() is not None:
            raise RuntimeError("服务进程提前退出")
        try:
            status, _ = call("GET", "/health")
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("服务未在 30s 内就绪")


def load_data() -> None:
    call("DELETE", f"/months/{MONTH}")
    # 工时：H1/H2 重复，H3 正常，H4 无路径
    _, r = call("POST", f"/months/{MONTH}/work-hours", {
        "rows": [
            {"row_id": 1, "work_order": "WO1", "minutes": 120},
            {"row_id": 2, "work_order": "WO2", "minutes": 90},
            {"row_id": 3, "work_order": "WO3", "minutes": 60},
            {"row_id": 4, "work_order": "WO4", "minutes": 30},
        ]
    })
    check("录入工时 4 行", r.get("inserted") == 4, str(r))
    # 分派：D1/D2 -> H1（同项目同科目，验证同科目也算重复）；
    #       D3/D4 -> H2（共享归科 A2）；D5 -> H3；WO4 无分派
    _, r = call("POST", f"/months/{MONTH}/dispatches", {
        "rows": [
            {"row_id": 1, "work_order": "WO1", "project": "P1"},
            {"row_id": 2, "work_order": "WO1", "project": "P1"},
            {"row_id": 3, "work_order": "WO2", "project": "P2"},
            {"row_id": 4, "work_order": "WO2", "project": "P3"},
            {"row_id": 5, "work_order": "WO3", "project": "P2"},
        ]
    })
    check("录入分派 5 行", r.get("inserted") == 5, str(r))
    _, r = call("POST", f"/months/{MONTH}/attributions", {
        "rows": [
            {"row_id": 1, "project": "P1", "budget_subject": "SUBJ-A"},
            {"row_id": 2, "project": "P2", "budget_subject": "SUBJ-B"},
            {"row_id": 3, "project": "P3", "budget_subject": "SUBJ-C"},
        ]
    })
    check("录入归科 3 行", r.get("inserted") == 3, str(r))


def scenario_shared_attribution() -> None:
    print("场景 A：共享归科 + 同科目重复路径")
    load_data()
    status, r = call("GET", f"/months/{MONTH}/supply")
    check("查询返回 200", status == 200, str(r))
    sup = {x["work_row_id"]: x for x in r["suppliable"]}
    wh = {x["work_row_id"]: x for x in r["withheld"]}

    check("H3 可供数且科目为 SUBJ-B", sup.get(3, {}).get("budget_subject") == "SUBJ-B", str(sup))
    check("H1 扣留且路径数=2（同科目也算重复）", wh.get(1, {}).get("path_count") == 2, str(wh))
    check("H2 扣留且路径数=2（共享归科）", wh.get(2, {}).get("path_count") == 2, str(wh))
    check("H4 扣留且路径数=0、原因 no_path",
          wh.get(4, {}).get("path_count") == 0 and wh.get(4, {}).get("reason") == "no_path", str(wh))
    check("可供数清单恰好 1 行", len(r["suppliable"]) == 1, str(r["suppliable"]))
    check("扣留清单恰好 3 行", len(r["withheld"]) == 3, str(r["withheld"]))

    # A1: 撤除共享归科 A2 会打掉 H3 的唯一路径 -> 拒绝并保持数据
    status, r = call("POST", f"/months/{MONTH}/corrections", {
        "work_row_id": 2, "remove": "attribution", "remove_row_id": 2
    })
    check("撤除共享归科 A2 被拒（409）", status == 409, f"{status} {r}")
    status, r = call("GET", f"/months/{MONTH}/supply")
    sup = {x["work_row_id"]: x for x in r["suppliable"]}
    check("拒绝后 H3 仍保有科目", sup.get(3, {}).get("budget_subject") == "SUBJ-B", str(sup))
    check("拒绝后扣留行仍是 H1/H2/H4",
          sorted(x["work_row_id"] for x in r["withheld"]) == [1, 2, 4], str(r["withheld"]))

    # A2: 撤除不在目标路径上的记录 -> 422 且不动数据
    status, r = call("POST", f"/months/{MONTH}/corrections", {
        "work_row_id": 1, "remove": "dispatch", "remove_row_id": 3
    })
    check("撤除无关分派 D3 被拒（422）", status == 422, f"{status} {r}")

    # A3: 对本就可供数的 H3 做撤除 -> 409
    status, r = call("POST", f"/months/{MONTH}/corrections", {
        "work_row_id": 3, "remove": "attribution", "remove_row_id": 2
    })
    check("对可供数工时撤除被拒（409）", status == 409, f"{status} {r}")

    # A4: 正确纠错：撤除 H1 的一条分派 D2，恢复供数且不影响他人
    status, r = call("POST", f"/months/{MONTH}/corrections", {
        "work_row_id": 1, "remove": "dispatch", "remove_row_id": 2
    })
    check("撤除 D2 成功（200）", status == 200, f"{status} {r}")
    check("返回恢复供数行号 [1]", r.get("restored_work_row_ids") == [1], str(r))
    sup = {x["work_row_id"]: x for x in r["suppliable"]}
    check("H1 恢复供数且科目 SUBJ-A", sup.get(1, {}).get("budget_subject") == "SUBJ-A", str(sup))
    check("H3 仍然可供数（保科）", 3 in sup, str(sup))
    check("可供数清单为 H1/H3", sorted(sup) == [1, 3], str(sup))
    check("扣留清单为 H2/H4", sorted(x["work_row_id"] for x in r["withheld"]) == [2, 4],
          str(r["withheld"]))

    # A5: 审计可查
    status, r = call("GET", f"/months/{MONTH}/audit")
    check("审计记录 1 条且内容正确",
          len(r["audits"]) == 1 and r["audits"][0]["removed_table"] == "dispatch"
          and r["audits"][0]["removed_row_id"] == 2, str(r))


def scenario_concurrency() -> None:
    print("场景 B：并发纠错")
    load_data()

    # B1: 两个独立重复工时 H1/H2 同时纠错，均应成功
    results: list[tuple[int, dict]] = []
    barrier = threading.Barrier(2)

    def worker(body: dict) -> None:
        barrier.wait()  # 尽量同时发出请求
        results.append(call("POST", f"/months/{MONTH}/corrections", body))

    t1 = threading.Thread(target=worker, args=(
        {"work_row_id": 1, "remove": "dispatch", "remove_row_id": 2},
    ))
    t2 = threading.Thread(target=worker, args=(
        {"work_row_id": 2, "remove": "dispatch", "remove_row_id": 4},
    ))
    t1.start(); t2.start(); t1.join(); t2.join()
    codes = sorted(c for c, _ in results)
    check("两个独立并发纠错均成功（200,200）", codes == [200, 200], str(results))
    restored = sorted(x for _, b in results for x in b.get("restored_work_row_ids", []))
    check("恢复行号汇总为 [1,2]", restored == [1, 2], str(results))

    status, r = call("GET", f"/months/{MONTH}/supply")
    sup = {x["work_row_id"]: x for x in r["suppliable"]}
    check("并发后 H1/H2/H3 全部可供数", sorted(sup) == [1, 2, 3], str(sup))
    check("H1 科目 SUBJ-A", sup.get(1, {}).get("budget_subject") == "SUBJ-A", str(sup))
    check("H2 科目 SUBJ-B（撤除的是 P3 那条）", sup.get(2, {}).get("budget_subject") == "SUBJ-B", str(sup))
    check("H3 科目仍为 SUBJ-B（共享归科未被破坏）", sup.get(3, {}).get("budget_subject") == "SUBJ-B", str(sup))
    check("仅剩 H4 无路径扣留", [x["work_row_id"] for x in r["withheld"]] == [4], str(r["withheld"]))

    # B2: 重置后，两个请求同时打同一工时，只允许一个成功，另一个基于已提交数据失败
    call("DELETE", f"/months/{MONTH}")
    load_data()
    results.clear()
    barrier2 = threading.Barrier(2)

    def worker_same(body: dict) -> None:
        barrier2.wait()
        results.append(call("POST", f"/months/{MONTH}/corrections", body))

    t1 = threading.Thread(target=worker_same, args=(
        {"work_row_id": 1, "remove": "dispatch", "remove_row_id": 1},
    ))
    t2 = threading.Thread(target=worker_same, args=(
        {"work_row_id": 1, "remove": "dispatch", "remove_row_id": 2},
    ))
    t1.start(); t2.start(); t1.join(); t2.join()
    ok = [b for c, b in results if c == 200]
    bad = [(c, b) for c, b in results if c != 200]
    check("同一工时并发纠错恰好 1 个成功", len(ok) == 1 and len(bad) == 1, str(results))
    check("失败方状态为 404/409/422 之一且给出原因",
          bad and bad[0][0] in (404, 409, 422) and bool(bad[0][1].get("detail")), str(bad))

    status, r = call("GET", f"/months/{MONTH}/supply")
    sup = {x["work_row_id"]: x for x in r["suppliable"]}
    check("冲突后数据自洽：H1 恰好一条路径可供数",
          1 in sup and sup[1]["path_count"] == 1, str(r))
    check("冲突后 H3 始终可供数", 3 in sup, str(sup))
    call("DELETE", f"/months/{MONTH}")


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
             + os.pathsep + os.environ.get("PYTHONPATH", "")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_ready(proc)
        scenario_shared_attribution()
        scenario_concurrency()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if failures:
            print("\n".join(proc.stdout.read().decode().splitlines()[-20:]))
    print()
    if failures:
        print(f"{FAIL} 共 {len(failures)} 项失败: {failures}")
        return 1
    print(f"{PASS} 全部断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
