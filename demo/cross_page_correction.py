#!/usr/bin/env python3
"""跨页更正演示：开快照 → 翻页途中更正数据 → 验证快照页不受影响的完整流程。

用法：
    python demo/cross_page_correction.py [--base http://localhost:8001]
                                         [--read-base http://localhost:8002]

--base       写入与默认读取的服务地址（docker compose 中 app1 映射到 8001）
--read-base  开启快照与翻页的服务地址；指定为另一进程（app2，8002）
             可演示两个服务进程共享同一 SQLite 数据库时快照语义依然成立。

脚本只依赖 Python 标准库。退出码 0 表示全部断言通过。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

SEED = [
    ("R-2026-001", "2026-10-02", 12.5),
    ("R-2026-002", "2026-10-03", 8.0),
    ("R-2026-003", "2026-10-05", 30.1),
    ("R-2026-004", "2026-10-07", 15.4),
    ("R-2026-005", "2026-10-09", 6.6),
    ("R-2026-006", "2026-10-12", 11.0),
    ("R-2026-007", "2026-10-14", 9.9),
    ("R-2026-008", "2026-10-16", 22.2),
    ("R-2026-009", "2026-10-19", 4.4),
    ("R-2026-010", "2026-10-21", 18.8),
    ("R-2026-011", "2026-10-23", 7.7),
    ("R-2026-012", "2026-10-27", 13.3),
]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"    {mark} {label}" + (f"（{detail}）" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label} {detail}")


def call(method: str, base: str, path: str, payload: dict | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def put(result_id: str, test_date: str, value, project: str = "COD") -> dict:
    return {
        "op": "put",
        "id": result_id,
        "sample_code": f"WS-{result_id[-3:]}",
        "project": project,
        "test_date": test_date,
        "value": value,
    }


def show_items(items: list[dict]) -> str:
    return "、".join(f"{it['id'][-3:]}={it['value']}" for it in items)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8001")
    parser.add_argument("--read-base", default=None,
                        help="开启快照/翻页用的服务地址，缺省同 --base")
    args = parser.parse_args()
    write_base = args.base
    read_base = (args.read_base or args.base).rstrip("/")

    print("=" * 72)
    print("跨页更正演示：快照翻页期间发生更正、删除、移出、新增")
    print(f"写入进程: {write_base}    取数进程: {read_base}")
    print("=" * 72)

    status, _ = call("GET", write_base, "/health")
    if status != 200:
        print(f"无法连接 {write_base}，请先启动服务（见 README）。")
        return 1

    print("\n[0] 批次 0：清理可能残留的演示记录（删除不存在的 ID 为幂等空操作）")
    all_ids = [rid for rid, _, _ in SEED] + ["R-2026-013", "R-2026-101", "R-2026-102"]
    status, body = call("POST", write_base, "/api/v1/batches",
                        {"operations": [{"op": "delete", "id": rid} for rid in all_ids]})
    check("清理批次提交成功", status == 200, f"HTTP {status}: {body}")

    print("\n[1] 批次 1：写入 12 条 10 月 COD 结果（另写 2 条对照记录）")
    ops = [put(rid, date, value) for rid, date, value in SEED]
    ops.append(put("R-2026-101", "2026-10-10", 0.5, project="NH3N"))  # 其它项目
    ops.append(put("R-2026-102", "2026-11-02", 99.0))                 # 区间外日期
    status, body = call("POST", write_base, "/api/v1/batches", {"operations": ops})
    check("批次 1 提交成功", status == 200, f"HTTP {status}: {body}")
    rev1 = body["revision"]
    print(f"    → 修订号 {rev1}（{body['puts']} 条写入）")

    print("\n[2] 开启快照：项目 COD，2026-10-01 ~ 2026-10-31，页大小 5")
    status, snap = call("POST", read_base, "/api/v1/snapshots", {
        "project": "COD", "date_from": "2026-10-01",
        "date_to": "2026-10-31", "page_size": 5,
    })
    check("快照创建成功", status == 200, f"HTTP {status}: {snap}")
    sid = snap["snapshot_id"]
    check("快照固定在批次 1 的修订号", snap["revision"] == rev1,
          f"snapshot revision={snap['revision']}, 期望 {rev1}")
    page1 = snap["page"]
    print(f"    第 1 页: {show_items(page1['items'])}")
    check("第 1 页为前 5 条", [it["id"] for it in page1["items"]]
          == [rid for rid, _, _ in SEED[:5]])

    print("\n[3] 批次 2（跨页更正）：改第1页的值、删第2页记录、"
          "把第3页记录移到 11 月、新增 1 条")
    corrections = [
        put("R-2026-003", "2026-10-05", 28.4),   # 更正第 1 页已读记录的值
        {"op": "delete", "id": "R-2026-006"},     # 删除将在第 2 页出现的记录
        put("R-2026-011", "2026-11-02", 7.7),     # 把第 3 页记录移出日期范围
        put("R-2026-013", "2026-10-25", 5.5),     # 新增一条区间内记录
    ]
    status, body = call("POST", write_base, "/api/v1/batches", {"operations": corrections})
    check("批次 2 提交成功", status == 200, f"HTTP {status}: {body}")
    rev2 = body["revision"]
    print(f"    → 修订号 {rev2}")

    print("\n[4] 继续用旧游标翻页（快照仍固定在修订 "
          f"{snap['revision']}）")
    pages = [page1]
    cursor = page1["next_cursor"]
    while cursor:
        status, resp = call(
            "GET", read_base,
            f"/api/v1/snapshots/{sid}/pages?cursor={urllib.parse.quote(cursor)}")
        check("翻页成功", status == 200, f"HTTP {status}: {resp}")
        pages.append(resp["page"])
        cursor = resp["page"]["next_cursor"]
    for page in pages:
        print(f"    第 {page['number']} 页: {show_items(page['items'])}")

    all_items = [it for page in pages for it in page["items"]]
    seed_ids = [rid for rid, _, _ in SEED]
    seed_values = {rid: value for rid, _, value in SEED}
    check("快照仍返回原来的 12 条（不重不漏）",
          [it["id"] for it in all_items] == seed_ids)
    check("被更正的 R-2026-003 仍是原值 30.1",
          next((it["value"] for it in all_items if it["id"] == "R-2026-003"),
               None) == 30.1)
    check("被删除的 R-2026-006 仍在快照中",
          "R-2026-006" in {it["id"] for it in all_items})
    check("被移到 11 月的 R-2026-011 仍在快照中且日期为 2026-10-23",
          next((it["test_date"] for it in all_items if it["id"] == "R-2026-011"),
               None) == "2026-10-23")
    check("新增的 R-2026-013 不出现在快照中",
          "R-2026-013" not in {it["id"] for it in all_items})
    check("所有值与修订 1 一致",
          all(seed_values.get(it["id"]) == it["value"] for it in all_items))

    print("\n[5] 同一游标重试：返回完全相同的页")
    cursor2 = page1["next_cursor"]
    _, first = call("GET", read_base,
                    f"/api/v1/snapshots/{sid}/pages?cursor={urllib.parse.quote(cursor2)}")
    _, again = call("GET", read_base,
                    f"/api/v1/snapshots/{sid}/pages?cursor={urllib.parse.quote(cursor2)}")
    check("两次响应字节级一致", first == again)

    print("\n[6] 重新开启快照：看到修订 " + str(rev2) + " 的当前状态")
    status, snap2 = call("POST", read_base, "/api/v1/snapshots", {
        "project": "COD", "date_from": "2026-10-01",
        "date_to": "2026-10-31", "page_size": 100,
    })
    items2 = snap2["page"]["items"]
    ids2 = [it["id"] for it in items2]
    print(f"    当前共 {len(ids2)} 条: {show_items(items2)}")
    check("新快照修订号为批次 2", snap2["revision"] == rev2)
    check("R-2026-003 显示更正后的值 28.4",
          next((it["value"] for it in items2 if it["id"] == "R-2026-003"),
               None) == 28.4)
    check("已删除的 R-2026-006 不在新快照中", "R-2026-006" not in ids2)
    check("已移出的 R-2026-011 不在新快照中", "R-2026-011" not in ids2)
    check("新增的 R-2026-013 在新快照中", "R-2026-013" in ids2)
    check("对照记录被正确过滤（NH3N 项目与 11 月记录均不在内）",
          "R-2026-101" not in ids2 and "R-2026-102" not in ids2)

    print("\n[7] 修订历史（重启后仍保留）")
    _, revisions = call("GET", read_base, "/api/v1/revisions")
    for rev in revisions[-3:]:
        print(f"    修订 {rev['revision']}: {rev['puts']} 写入 / "
              f"{rev['deletes']} 删除 @ {rev['created_at']}")
    check("修订号单调递增",
          all(a["revision"] < b["revision"]
              for a, b in zip(revisions, revisions[1:])))

    print("\n" + "=" * 72)
    if failures:
        print(f"演示失败，共 {len(failures)} 项断言未通过：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("全部断言通过：快照翻页不受跨页更正影响，月报取数不重不漏。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
