import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PERIODICALS_DB", str(tmp_path / "test.sqlite3"))
    with TestClient(app) as test_client:
        yield test_client


def make_volume(client, issues=8):
    labels = [str(i) for i in range(1, issues + 1)]
    response = client.post(
        "/volumes",
        json={"title": "Test Journal", "volume_label": "Vol 1", "issues": labels},
    )
    assert response.status_code == 201, response.text
    return response.json()["volume_id"]


def add(client, volume_id, barcode, candidates, preview=False):
    return client.post(
        f"/volumes/{volume_id}/batches",
        json={
            "items": [
                {
                    "barcode": barcode,
                    "action": "add",
                    "candidates": candidates,
                }
            ],
            "preview": preview,
        },
    )


def test_manifest_rejects_more_than_24(tmp_path, monkeypatch):
    monkeypatch.setenv("PERIODICALS_DB", str(tmp_path / "t.sqlite3"))
    with TestClient(app) as client:
        response = client.post(
            "/volumes",
            json={
                "title": "Too long",
                "volume_label": "V",
                "issues": [str(i) for i in range(25)],
            },
        )
        assert response.status_code == 422


def test_volume_manifest_and_empty_gaps(client):
    volume_id = make_volume(client, 4)
    gaps = client.get(f"/volumes/{volume_id}/gaps").json()
    assert gaps["missing"] == ["1", "2", "3", "4"]
    assert gaps["held"] == []


def test_add_copies_and_classification(client):
    volume_id = make_volume(client, 4)
    response = add(client, volume_id, "BC1", [["1"]])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "committed"
    assert body["reconciliation"]["before"]["missing"] == ["1", "2", "3", "4"]
    assert body["reconciliation"]["after"]["held"] == ["1"]

    gaps = client.get(f"/volumes/{volume_id}/gaps").json()
    assert gaps["held"] == ["1"]
    assert gaps["missing"] == ["2", "3", "4"]


def test_multi_group_copy_marks_ambiguous_issues_for_review(client):
    volume_id = make_volume(client, 3)
    response = add(client, volume_id, "BC1", [["1"], ["2"]])
    assert response.status_code == 201, response.text
    gaps = client.get(f"/volumes/{volume_id}/gaps").json()
    assert gaps["review"] == ["1", "2"]
    assert gaps["missing"] == ["3"]


def test_unsatisfiable_batch_is_rejected_whole_and_audited(client):
    volume_id = make_volume(client, 3)
    assert add(client, volume_id, "BC1", [["1"]]).status_code == 201
    before = client.get(f"/volumes/{volume_id}/gaps").json()

    response = add(client, volume_id, "BC2", [["1"]])
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "rejected"
    assert body["reason"]
    assert body["items"][0]["outcome"] == "rejected"

    after = client.get(f"/volumes/{volume_id}/gaps").json()
    assert after["held"] == before["held"]
    assert after["missing"] == before["missing"]
    # original holding still present, rejected batch queryable
    snapshot = client.get(f"/volumes/{volume_id}").json()
    assert [c["barcode"] for c in snapshot["copies"]] == ["BC1"]
    batches = client.get(f"/volumes/{volume_id}/batches").json()
    assert [b["status"] for b in batches] == ["committed", "rejected"]
    assert batches[1]["batch_id"] == body["batch_id"]
    assert batches[1]["reconciliation"]["after"] is None
    assert batches[1]["reconciliation"]["before"]["held"] == ["1"]


def test_multi_item_batch_rolls_back_everything_on_failure(client):
    volume_id = make_volume(client, 4)
    assert add(client, volume_id, "BC1", [["1"]]).status_code == 201
    response = client.post(
        f"/volumes/{volume_id}/batches",
        json={
            "items": [
                {"barcode": "NEW", "action": "add", "candidates": [["4"]]},
                {"barcode": "BAD", "action": "add", "candidates": [["1"]]},
            ]
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "rejected"
    snapshot = client.get(f"/volumes/{volume_id}").json()
    assert [c["barcode"] for c in snapshot["copies"]] == ["BC1"]


def test_update_replaces_candidates_and_can_uncover_old_issue(client):
    volume_id = make_volume(client, 3)
    assert add(client, volume_id, "BC1", [["1"]]).status_code == 201
    assert client.get(f"/volumes/{volume_id}/gaps").json()["held"] == ["1"]

    # Correct the barcode to issue 2: issue 1 becomes missing again.
    response = client.post(
        f"/volumes/{volume_id}/batches",
        json={
            "items": [
                {"barcode": "BC1", "action": "update", "candidates": [["2"]]}
            ]
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "committed"
    changes = response.json()["reconciliation"]["changes"]
    assert {c["issue"] for c in changes if c["after"] == "missing"} == {"1"}
    gaps = client.get(f"/volumes/{volume_id}/gaps").json()
    assert gaps["missing"] == ["1", "3"]
    assert gaps["held"] == ["2"]


def test_preview_does_not_persist(client):
    volume_id = make_volume(client, 2)
    response = add(client, volume_id, "BC1", [["1"]], preview=True)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "would_commit"
    assert body["reconciliation"]["after"]["held"] == ["1"]
    assert client.get(f"/volumes/{volume_id}").json()["copies"] == []
    assert client.get(f"/volumes/{volume_id}/batches").json() == []


def test_preview_reports_rejection_without_writing(client):
    volume_id = make_volume(client, 2)
    assert add(client, volume_id, "BC1", [["1"]]).status_code == 201
    response = add(client, volume_id, "BC2", [["1"]], preview=True)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "would_reject"
    assert body["reconciliation"]["after"] is None
    assert len(client.get(f"/volumes/{volume_id}/batches").json()) == 1


def test_validation_errors(client):
    volume_id = make_volume(client, 3)
    bad_issue = add(client, volume_id, "X", [["99"]])
    assert bad_issue.status_code == 422
    assert bad_issue.json()["error"]["code"] == "unknown_issue"

    four_groups = add(client, volume_id, "Y", [["1"], ["2"], ["3"], ["1"]])
    assert four_groups.status_code == 422

    dup_in_batch = client.post(
        f"/volumes/{volume_id}/batches",
        json={
            "items": [
                {"barcode": "Z", "action": "add", "candidates": [["1"]]},
                {"barcode": "Z", "action": "add", "candidates": [["2"]]},
            ]
        },
    )
    assert dup_in_batch.status_code == 422
    assert dup_in_batch.json()["error"]["code"] == "duplicate_barcode"

    update_missing = client.post(
        f"/volumes/{volume_id}/batches",
        json={
            "items": [{"barcode": "NOPE", "action": "update", "candidates": [["1"]]}]
        },
    )
    assert update_missing.status_code == 404


def test_concurrent_submissions_follow_commit_order(client, monkeypatch):
    volume_id = make_volume(client, 4)
    results = []
    lock = threading.Lock()

    def worker(barcode, issue):
        response = add(client, volume_id, barcode, [[issue]])
        with lock:
            results.append((response.status_code, response.json()))

    threads = [
        threading.Thread(target=worker, args=("A", "1")),
        threading.Thread(target=worker, args=("B", "1")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    statuses = sorted(body["status"] for _, body in results)
    # One wins the issue, the other is rejected because the first committed.
    assert statuses == ["committed", "rejected"]
    gaps = client.get(f"/volumes/{volume_id}/gaps").json()
    assert gaps["held"] == ["1"]
    committed = [b for b in client.get(f"/volumes/{volume_id}/batches").json() if b["status"] == "committed"]
    assert len(committed) == 1
    # seq numbers reflect actual commit order
    seqs = [b["seq"] for b in client.get(f"/volumes/{volume_id}/batches").json()]
    assert seqs == sorted(seqs)


def test_barcode_cannot_move_between_volumes(client):
    first = make_volume(client, 2)
    second_response = client.post(
        "/volumes",
        json={"title": "Another", "volume_label": "Vol 2", "issues": ["1", "2"]},
    )
    second = second_response.json()["volume_id"]
    assert add(client, first, "SHARED", [["1"]]).status_code == 201

    conflict = add(client, second, "SHARED", [["1"]])
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "barcode_exists"
    assert client.get(f"/volumes/{second}").json()["copies"] == []
