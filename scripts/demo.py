#!/usr/bin/env python3
"""End-to-end demo for the periodical gap reconciliation API.

Run against a live server (default http://127.0.0.1:8000):

    python scripts/demo.py            # local uvicorn
    docker compose exec api python scripts/demo.py   # inside the container

It walks through:
  1. creating a volume manifest (4 issues),
  2. adding an ambiguous bound copy (candidate group [1,2] or [3,4]),
  3. previewing a new copy that rules out the old candidate and shows how
     issue 4 flips 待翻册 -> 缺刊,
  4. committing that batch,
  5. submitting a conflicting batch that is rejected in full while both the
     holdings and the gap conclusion remain queryable,
  6. concurrent submissions where the loser is evaluated after the winner,
  7. interleaved GET /volumes/{id} and /gaps reads while a correction batch
     commits: every response is one whole committed state, never a splice.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000").rstrip("/")


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def show(title: str, gaps: dict) -> None:
    print(f"\n== {title} ==")
    print(f"  有藏   : {gaps.get('held')}")
    print(f"  缺刊   : {gaps.get('missing')}")
    print(f"  待翻册 : {gaps.get('review')}")
    if gaps.get("summary"):
        print(f"  合计   : {gaps['summary']}")


def main() -> int:
    status, health = call("GET", "/healthz")
    if status != 200:
        print(f"API not reachable at {BASE}: {health}")
        return 1

    issues = ["第1期", "第2期", "第3期", "第4期"]
    status, volume = call(
        "POST",
        "/volumes",
        {"title": "旧期刊演示", "volume_label": "第12卷", "issues": issues},
    )
    if status == 409:
        print("演示卷已存在，请使用新的数据库或删除 data/periodicals.sqlite3")
        return 1
    if status != 201:
        print("创建失败", status, volume)
        return 1
    volume_id = volume["volume_id"]
    print(f"创建卷 #{volume_id}: {volume['title']} {volume['volume_label']} {issues}")
    show("初始状态", volume["gap_report"])

    # A bound copy: the cover is unclear, it may be 第1+2期 or 第3+4期.
    status, body = call(
        "POST",
        f"/volumes/{volume_id}/batches",
        {
            "items": [
                {
                    "barcode": "BC-001",
                    "action": "add",
                    "candidates": [["第1期", "第2期"], ["第3期", "第4期"]],
                }
            ]
        },
    )
    print(f"\n增补合刊册 BC-001 -> {status} {body.get('status')}")
    show("加入 BC-001 后", body["reconciliation"]["after"])

    # Preview only: a new copy known to be 第3期 rules out BC-001 = [3,4].
    status, preview = call(
        "POST",
        f"/volumes/{volume_id}/batches",
        {
            "items": [
                {"barcode": "BC-002", "action": "add", "candidates": [["第3期"]]}
            ],
            "preview": True,
        },
    )
    print(f"\n预览增补 BC-002 -> {status} {preview.get('status')}")
    show("预览：BC-002 排除 BC-001 的 [第3,4期] 候选后", preview["reconciliation"]["after"])
    print("  变化明细:")
    for change in preview["reconciliation"]["changes"]:
        print(
            f"    {change['issue']}: {change['before_zh']} -> {change['after_zh']}"
        )

    status, committed = call(
        "POST",
        f"/volumes/{volume_id}/batches",
        {
            "items": [
                {"barcode": "BC-002", "action": "add", "candidates": [["第3期"]]}
            ]
        },
    )
    print(f"\n提交 BC-002 -> {status} {committed.get('status')} (batch #{committed.get('batch_id')})")
    status, gaps = call("GET", f"/volumes/{volume_id}/gaps")
    show("提交后：第4期确认缺刊", gaps)

    # A conflicting batch: BC-001 must now be [1,2], so BC-003 cannot also be [1,2].
    status, rejected = call(
        "POST",
        f"/volumes/{volume_id}/batches",
        {
            "items": [
                {
                    "barcode": "BC-003",
                    "action": "add",
                    "candidates": [["第1期", "第2期"]],
                }
            ]
        },
    )
    print(f"\n冲突批次 BC-003 -> {status} {rejected.get('status')}: {rejected.get('reason')}")
    status, gaps = call("GET", f"/volumes/{volume_id}/gaps")
    show("整批退回后，缺刊结论不变", gaps)
    status, snapshot = call("GET", f"/volumes/{volume_id}")
    print(f"  当前馆藏条码: {[c['barcode'] for c in snapshot['copies']]}")
    status, batches = call("GET", f"/volumes/{volume_id}/batches")
    print(f"  审计批次: {[(b['seq'], b['status']) for b in batches]}")

    # Concurrent submissions of the same free issue: exactly one commits.
    volume2 = call(
        "POST",
        "/volumes",
        {"title": "旧期刊演示", "volume_label": "第13卷", "issues": ["第1期", "第2期"]},
    )
    if volume2[0] == 201:
        vid = volume2[1]["volume_id"]
        results: list[tuple[str, dict]] = []
        lock = threading.Lock()

        def submit(barcode: str) -> None:
            status, body = call(
                "POST",
                f"/volumes/{vid}/batches",
                {
                    "items": [
                        {
                            "barcode": barcode,
                            "action": "add",
                            "candidates": [["第1期"]],
                        }
                    ]
                },
            )
            with lock:
                results.append((barcode, body))

        threads = [threading.Thread(target=submit, args=(bc,)) for bc in ("C-1", "C-2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        print("\n== 并发提交同一期 ==")
        for barcode, body in sorted(results):
            print(f"  {barcode}: {body.get('status')}")
        print(f"  提交顺序(seq): {[(b['seq'], b['status']) for b in call('GET', f'/volumes/{vid}/batches')[1]]}")

    # Interleaved reads vs a correction batch.  Each GET is served inside one
    # snapshot transaction (manifest + copies + candidate groups + gap report
    # at the same commit point), so every response must match either the
    # pre-commit state or the post-commit state as a whole.
    volume3 = call(
        "POST",
        "/volumes",
        {"title": "旧期刊演示", "volume_label": "第14卷", "issues": ["第1期", "第2期", "第3期"]},
    )
    if volume3[0] == 201:
        vid3 = volume3[1]["volume_id"]
        assert call(
            "POST",
            f"/volumes/{vid3}/batches",
            {"items": [{"barcode": "D-1", "action": "add", "candidates": [["第1期"]]}]},
        )[0] == 201
        pre = call("GET", f"/volumes/{vid3}")[1]
        pre_signature = (
            tuple(c["barcode"] for c in pre["copies"]),
            tuple(pre["gap_report"]["held"]),
        )

        barrier = threading.Barrier(2)
        observations: list[tuple[str, tuple, tuple]] = []
        obs_lock = threading.Lock()

        def reader() -> None:
            barrier.wait()
            for _ in range(6):
                snap_status, snap = call("GET", f"/volumes/{vid3}")
                gap_status, gaps = call("GET", f"/volumes/{vid3}/gaps")
                assert snap_status == 200 and gap_status == 200
                # Internal consistency: the embedded report equals /gaps
                # inputs at the same logical point and partitions the manifest.
                assert snap["gap_report"]["summary"] == gaps["summary"]
                signature = (
                    tuple(c["barcode"] for c in snap["copies"]),
                    tuple(gaps["held"]),
                )
                with obs_lock:
                    observations.append(("snapshot+gaps",) + signature)

        def corrector() -> None:
            barrier.wait()
            status, body = call(
                "POST",
                f"/volumes/{vid3}/batches",
                {
                    "items": [
                        {"barcode": "D-1", "action": "update", "candidates": [["第2期"]]},
                        {"barcode": "D-2", "action": "add", "candidates": [["第3期"]]},
                    ]
                },
            )
            assert status == 201 and body["status"] == "committed", (status, body)

        threads = [threading.Thread(target=reader), threading.Thread(target=corrector)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        post = call("GET", f"/volumes/{vid3}")[1]
        post_signature = (
            tuple(c["barcode"] for c in post["copies"]),
            tuple(post["gap_report"]["held"]),
        )
        print("\n== 读取与更正交错：每个响应都是完整的同一时点 ==")
        print(f"  更正前状态: {pre_signature}")
        print(f"  更正后状态: {post_signature}")
        seen = sorted(set(obs[1:] for obs in observations))
        print(f"  交错期间实际观测到的状态: {seen}")
        assert set(seen) <= {pre_signature, post_signature}, "observed a spliced state!"
        assert pre_signature != post_signature
        print("  所有 GET /volumes/{id} 与 /gaps 均只落在更正前或更正后的完整状态上")

    print("\nSwagger UI: " + BASE + "/docs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
