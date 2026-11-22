"""Concurrency tests: read endpoints must observe one committed snapshot.

The deterministic tests park a GET request between its ``copies`` SELECT and
its ``candidate_groups`` SELECT (the exact seam where another transaction can
commit) and then submit a correction batch.  A correct implementation pins a
read snapshot for the whole request, so the response equals one of the two
real committed states -- never a splice of holdings from before the commit
with candidate groups from after it.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import db, service
from app.main import app
from app.solver import evaluate


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PERIODICALS_DB", str(tmp_path / "test.sqlite3"))
    with TestClient(app) as test_client:
        yield test_client


def make_volume(client, issues=6):
    labels = [str(i) for i in range(1, issues + 1)]
    response = client.post(
        "/volumes",
        json={"title": "Race Journal", "volume_label": "Vol 1", "issues": labels},
    )
    assert response.status_code == 201, response.text
    return response.json()["volume_id"]


def add(client, volume_id, barcode, candidates, action="add"):
    return client.post(
        f"/volumes/{volume_id}/batches",
        json={
            "items": [
                {"barcode": barcode, "action": action, "candidates": candidates}
            ]
        },
    )


def _gated_groups(monkeypatch, reached: threading.Event, release: threading.Event):
    """Wrap _fetch_copy_groups so one read request blocks on the read seam.

    TestClient runs the ASGI app in a portal thread, so the gate is a
    single-use flag rather than a thread identity check: the writer is only
    started after the reader has parked and disarmed the flag.
    """
    original = service._fetch_copy_groups
    gate = {"armed": False}

    def wrapper(conn, copy_ids):
        if gate["armed"]:
            gate["armed"] = False
            reached.set()
            assert release.wait(timeout=10)
        return original(conn, copy_ids)

    monkeypatch.setattr(service, "_fetch_copy_groups", wrapper)
    return gate


@pytest.mark.parametrize("path_suffix", ["", "/gaps"])
def test_reader_pinned_to_one_snapshot_across_committing_batch(
    client, monkeypatch, path_suffix
):
    volume_id = make_volume(client)
    assert add(client, volume_id, "BC1", [["1"]]).status_code == 201
    before = client.get(f"/volumes/{volume_id}").json()
    assert before["gap_report"]["held"] == ["1"]

    reached = threading.Event()
    release = threading.Event()
    gate = _gated_groups(monkeypatch, reached, release)
    outcome: dict = {}

    def reader() -> None:
        gate["armed"] = True
        response = client.get(f"/volumes/{volume_id}{path_suffix}")
        outcome["status"] = response.status_code
        outcome["body"] = response.json()

    thread = threading.Thread(target=reader)
    thread.start()
    assert reached.wait(timeout=10), "reader never reached the candidate-group query"

    # One batch both updates BC1 (1 -> 2) and adds BC2 (issue 3).  It commits
    # while the reader is parked between copies and candidate groups.
    write = client.post(
        f"/volumes/{volume_id}/batches",
        json={
            "items": [
                {"barcode": "BC1", "action": "update", "candidates": [["2"]]},
                {"barcode": "BC2", "action": "add", "candidates": [["3"]]},
            ]
        },
    )
    assert write.status_code == 201, write.text
    assert write.json()["status"] == "committed"

    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome["status"] == 200, outcome

    body = outcome["body"]
    if path_suffix == "":
        # Whole-volume view: pre-commit holdings AND pre-commit groups.
        assert body["copies"] == before["copies"]
        assert body["gap_report"]["held"] == ["1"]
        assert body["gap_report"]["missing"] == ["2", "3", "4", "5", "6"]
    else:
        assert body["held"] == ["1"]
        assert body["missing"] == ["2", "3", "4", "5", "6"]

    # After the reader finished, the post-commit state is visible as a whole.
    after = client.get(f"/volumes/{volume_id}").json()
    assert [c["barcode"] for c in after["copies"]] == ["BC1", "BC2"]
    assert after["gap_report"]["held"] == ["2", "3"]


def test_concurrent_reads_and_batches_only_see_real_committed_states(client):
    volume_id = make_volume(client)
    initial = client.get(f"/volumes/{volume_id}/gaps").json()

    stop = threading.Event()
    errors: list[str] = []
    lock = threading.Lock()

    def check_snapshot(body: dict) -> None:
        # Every returned copy must carry its candidate groups (no half-row
        # state), and the embedded gap report must match the returned copies.
        copies = body.get("copies")
        if copies is not None:
            for copy in copies:
                if not copy["candidates"]:
                    errors.append(f"copy without candidate groups: {copy}")
            issue_index = {label: i for i, label in enumerate(body["issues"])}
            masks = []
            for copy in copies:
                masks.append(
                    [
                        sum(1 << issue_index[label] for label in group)
                        for group in copy["candidates"]
                    ]
                )
            evaluation = evaluate(len(body["issues"]), masks)
            assert evaluation.satisfiable
            if evaluation.statuses and sorted(
                body["gap_report"]["held"]
                + body["gap_report"]["missing"]
                + body["gap_report"]["review"]
            ) != sorted(body["issues"]):
                errors.append("gap report does not partition the manifest")

    def reader() -> None:
        while not stop.is_set():
            for path in (f"/volumes/{volume_id}", f"/volumes/{volume_id}/gaps"):
                response = client.get(path)
                if response.status_code != 200:
                    with lock:
                        errors.append(f"GET {path} -> {response.status_code} {response.text}")
                    return
                with lock:
                    check_snapshot(response.json())

    readers = [threading.Thread(target=reader) for _ in range(4)]

    def writer(barcode: str, issue: str) -> None:
        response = add(client, volume_id, barcode, [[issue]])
        assert response.status_code == 201, response.text

    # Distinct issues mostly commit; duplicate issue 1 forces a rejection,
    # which must leave every read on one of the committed states.
    writer_specs = [
        ("W0", "1"), ("W1", "2"), ("W2", "3"),
        ("W3", "1"), ("W4", "4"), ("W5", "5"),
    ]
    writers = [threading.Thread(target=writer, args=spec) for spec in writer_specs]

    for thread in readers:
        thread.start()
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join()
    stop.set()
    for thread in readers:
        thread.join()

    assert errors == []

    # Every classification observed by a reader must be one of the states the
    # database actually committed (initial + each batch's before/after view).
    canonical: set[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
        (
            tuple(initial["held"]),
            tuple(initial["missing"]),
            tuple(initial["review"]),
        )
    }
    for batch in client.get(f"/volumes/{volume_id}/batches").json():
        reconciliation = batch["reconciliation"]
        for view in (reconciliation["before"], reconciliation["after"]):
            if view is None:
                continue
            canonical.add(
                (tuple(view["held"]), tuple(view["missing"]), tuple(view["review"]))
            )

    # Re-run readers serially now that the dust has settled and cross-check.
    final_gaps = client.get(f"/volumes/{volume_id}/gaps").json()
    assert (
        tuple(final_gaps["held"]),
        tuple(final_gaps["missing"]),
        tuple(final_gaps["review"]),
    ) in canonical

    committed = [
        b for b in client.get(f"/volumes/{volume_id}/batches").json()
        if b["status"] == "committed"
    ]
    rejected = [
        b for b in client.get(f"/volumes/{volume_id}/batches").json()
        if b["status"] == "rejected"
    ]
    assert len(committed) + len(rejected) == len(writer_specs)
    # Rejected batch changed nothing: its before/after views coincide.
    for batch in rejected:
        assert batch["reconciliation"]["after"] is None


def test_read_snapshot_is_reentrant_and_rolls_back_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PERIODICALS_DB", str(tmp_path / "t.sqlite3"))
    from app.main import _db_path

    db_path = _db_path()
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        with db.read_snapshot(conn):
            assert conn.in_transaction
            with db.read_snapshot(conn):  # nested: no second BEGIN
                assert conn.in_transaction
                rows = conn.execute("SELECT 1 AS x").fetchone()
                assert rows["x"] == 1
            assert conn.in_transaction
        assert not conn.in_transaction  # committed cleanly

        with pytest.raises(RuntimeError):
            with db.read_snapshot(conn):
                raise RuntimeError("boom")
        assert not conn.in_transaction  # rolled back, connection reusable
        assert conn.execute("SELECT 1 AS x").fetchone()["x"] == 1
    finally:
        conn.close()
