"""Business logic: manifests, holdings batches and gap reconciliation."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

from . import db
from .solver import (
    HELD,
    MAX_CANDIDATES_PER_COPY,
    MAX_ISSUES,
    MISSING,
    REVIEW,
    Evaluation,
    evaluate,
)

STATUS_LABELS = {
    HELD: "有藏",
    MISSING: "缺刊",
    REVIEW: "待翻册",
}

_BATCH_STATUSES = ("committed", "rejected")


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(slots=True)
class CopyState:
    barcode: str
    groups: list[list[int]]  # issue bitmasks per candidate group


@dataclass(slots=True)
class VolumeState:
    id: int
    title: str
    volume_label: str
    issue_labels: list[str]
    copies: list[CopyState]

    @property
    def issue_count(self) -> int:
        return len(self.issue_labels)

    def label(self, bitmask: int) -> list[str]:
        return [
            self.issue_labels[i]
            for i in range(self.issue_count)
            if bitmask >> i & 1
        ]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _fetch_issue_labels(conn: sqlite3.Connection, volume_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT position, label FROM issues WHERE volume_id = ? ORDER BY position",
        (volume_id,),
    ).fetchall()
    if not rows:
        raise ApiError(404, "volume_not_found", f"volume {volume_id} does not exist")
    expected = [r["position"] for r in rows]
    if expected != list(range(1, len(rows) + 1)):
        raise ApiError(500, "manifest_corrupt", "issue positions are not contiguous")
    return [r["label"] for r in rows]


def _fetch_copies(conn: sqlite3.Connection, volume_id: int) -> list[CopyState]:
    copy_rows = conn.execute(
        "SELECT id, barcode FROM copies WHERE volume_id = ? ORDER BY id",
        (volume_id,),
    ).fetchall()
    by_id: dict[int, CopyState] = {}
    order: list[int] = []
    for row in copy_rows:
        by_id[row["id"]] = CopyState(row["barcode"], [])
        order.append(row["id"])
    if not order:
        return []
    placeholders = ",".join("?" * len(order))
    group_rows = conn.execute(
        f"""
        SELECT g.id, g.copy_id, g.position, gi.issue_id, i.position AS issue_position
        FROM candidate_groups g
        LEFT JOIN candidate_group_issues gi ON gi.group_id = g.id
        LEFT JOIN issues i ON i.id = gi.issue_id
        WHERE g.copy_id IN ({placeholders})
        ORDER BY g.copy_id, g.position, i.position
        """,
        order,
    ).fetchall()
    groups: dict[int, dict[int, int]] = {}
    for row in group_rows:
        per_copy = groups.setdefault(row["copy_id"], {})
        mask = per_copy.get(row["position"], 0)
        if row["issue_position"] is not None:
            mask |= 1 << (row["issue_position"] - 1)
        per_copy[row["position"]] = mask
    copies: list[CopyState] = []
    for copy_id in order:
        masks = [groups[copy_id][pos] for pos in sorted(groups.get(copy_id, {}))]
        copies.append(CopyState(by_id[copy_id].barcode, masks))
    return copies


def get_volume_state(conn: sqlite3.Connection, volume_id: int) -> VolumeState:
    row = conn.execute(
        "SELECT id, title, volume_label FROM volumes WHERE id = ?", (volume_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "volume_not_found", f"volume {volume_id} does not exist")
    return VolumeState(
        id=row["id"],
        title=row["title"],
        volume_label=row["volume_label"],
        issue_labels=_fetch_issue_labels(conn, volume_id),
        copies=_fetch_copies(conn, volume_id),
    )


def _describe(evaluation: Evaluation, volume: VolumeState) -> dict:
    grouped: dict[str, list[str]] = {HELD: [], MISSING: [], REVIEW: []}
    for index, status in enumerate(evaluation.statuses):
        grouped[status].append(volume.issue_labels[index])
    return {
        "satisfiable": evaluation.satisfiable,
        "held": grouped[HELD],
        "missing": grouped[MISSING],
        "review": grouped[REVIEW],
        "summary": {
            "held": len(grouped[HELD]),
            "missing": len(grouped[MISSING]),
            "review": len(grouped[REVIEW]),
        },
        "labels_zh": STATUS_LABELS,
    }


def reconcile(volume: VolumeState, *, want_assignment: bool = True) -> Evaluation:
    return evaluate(
        volume.issue_count,
        [copy.groups for copy in volume.copies],
        want_assignment=want_assignment,
    )


def volume_snapshot(volume: VolumeState) -> dict:
    evaluation = reconcile(volume)
    if not evaluation.satisfiable:
        raise ApiError(
            500,
            "holdings_inconsistent",
            "persisted holdings are not reconcilable; database was modified externally",
        )
    return {
        "volume_id": volume.id,
        "title": volume.title,
        "volume_label": volume.volume_label,
        "issues": volume.issue_labels,
        "copies": [
            {
                "barcode": copy.barcode,
                "candidates": [volume.label(mask) for mask in copy.groups],
            }
            for copy in volume.copies
        ],
        "gap_report": _describe(evaluation, volume),
    }


def gap_report(conn: sqlite3.Connection, volume_id: int) -> dict:
    volume = get_volume_state(conn, volume_id)
    evaluation = reconcile(volume)
    if not evaluation.satisfiable:
        raise ApiError(
            500,
            "holdings_inconsistent",
            "persisted holdings are not reconcilable; database was modified externally",
        )
    report = _describe(evaluation, volume)
    report["volume_id"] = volume.id
    report["issues"] = volume.issue_labels
    return report


# ---------------------------------------------------------------------------
# Volume creation
# ---------------------------------------------------------------------------


def create_volume(
    conn: sqlite3.Connection,
    *,
    title: str,
    volume_label: str,
    issue_labels: Iterable[str],
) -> dict:
    labels = [label.strip() for label in issue_labels]
    if not title.strip() or not volume_label.strip():
        raise ApiError(422, "invalid_volume", "title and volume_label must not be blank")
    if not 1 <= len(labels) <= MAX_ISSUES:
        raise ApiError(
            422,
            "invalid_manifest",
            f"a volume must have between 1 and {MAX_ISSUES} issues",
        )
    if any(not label for label in labels):
        raise ApiError(422, "invalid_manifest", "issue labels must not be blank")
    if len(set(labels)) != len(labels):
        raise ApiError(422, "invalid_manifest", "issue labels must be unique")

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM volumes WHERE title = ? AND volume_label = ?",
            (title, volume_label),
        ).fetchone()
        if existing:
            raise ApiError(
                409,
                "volume_exists",
                f"volume {title!r} {volume_label!r} already exists",
            )
        now = db.utcnow()
        cursor = conn.execute(
            "INSERT INTO volumes (title, volume_label, created_at) VALUES (?, ?, ?)",
            (title, volume_label, now),
        )
        volume_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO issues (volume_id, position, label) VALUES (?, ?, ?)",
            [(volume_id, pos, label) for pos, label in enumerate(labels, start=1)],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return volume_snapshot(get_volume_state(conn, volume_id))


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BatchPlan:
    volume: VolumeState
    merged: list[CopyState]
    issue_index: dict[str, int]
    item_masks: list[list[int]]
    outcomes: list[str]


def _normalise_items(
    items: list[dict],
    volume: VolumeState,
    other_volume_barcodes: dict[str, int] | None = None,
) -> BatchPlan:
    """Validate a batch against the manifest and merge it with current holdings."""
    other_volume_barcodes = other_volume_barcodes or {}
    if not items:
        raise ApiError(422, "empty_batch", "a batch must contain at least one item")
    issue_index = {label: i for i, label in enumerate(volume.issue_labels)}
    seen: set[str] = set()
    updates: dict[str, list[int]] = {}
    additions: list[tuple[str, list[int]]] = []
    item_masks: list[list[int]] = []
    outcomes: list[str] = []

    for item in items:
        barcode = item["barcode"].strip()
        if not barcode:
            raise ApiError(422, "invalid_barcode", "barcode must not be blank")
        if barcode in seen:
            raise ApiError(
                422,
                "duplicate_barcode",
                f"barcode {barcode!r} appears more than once in the batch",
            )
        seen.add(barcode)
        action = item["action"]
        groups_in = item["candidates"]
        if not groups_in:
            raise ApiError(
                422,
                "missing_candidates",
                f"copy {barcode!r} must list at least one candidate group",
            )
        if len(groups_in) > MAX_CANDIDATES_PER_COPY:
            raise ApiError(
                422,
                "too_many_candidates",
                f"copy {barcode!r} has {len(groups_in)} candidate groups; max is "
                f"{MAX_CANDIDATES_PER_COPY}",
            )
        masks: list[int] = []
        for group in groups_in:
            if not group:
                raise ApiError(
                    422,
                    "empty_candidate_group",
                    f"copy {barcode!r} has an empty candidate group",
                )
            mask = 0
            for label in group:
                key = label.strip()
                if key not in issue_index:
                    raise ApiError(
                        422,
                        "unknown_issue",
                        f"copy {barcode!r} references issue {label!r} which is not in "
                        f"the manifest",
                    )
                bit = 1 << issue_index[key]
                if mask & bit:
                    raise ApiError(
                        422,
                        "duplicate_issue_in_group",
                        f"copy {barcode!r} lists issue {label!r} twice in one group",
                    )
                mask |= bit
            if mask in masks:
                raise ApiError(
                    422,
                    "duplicate_candidate_group",
                    f"copy {barcode!r} has two identical candidate groups",
                )
            masks.append(mask)
        item_masks.append(masks)

        if action == "add":
            if any(copy.barcode == barcode for copy in volume.copies):
                raise ApiError(
                    409,
                    "barcode_exists",
                    f"barcode {barcode!r} already exists in this volume; use action=update",
                )
            if barcode in other_volume_barcodes:
                raise ApiError(
                    409,
                    "barcode_exists",
                    f"barcode {barcode!r} already exists in volume "
                    f"{other_volume_barcodes[barcode]}; a physical copy belongs to one "
                    f"volume",
                )
            additions.append((barcode, masks))
            outcomes.append("added")
        else:
            if not any(copy.barcode == barcode for copy in volume.copies):
                if barcode in other_volume_barcodes:
                    raise ApiError(
                        409,
                        "barcode_exists",
                        f"barcode {barcode!r} belongs to volume "
                        f"{other_volume_barcodes[barcode]}, not this one",
                    )
                raise ApiError(
                    404,
                    "copy_not_found",
                    f"barcode {barcode!r} does not exist in this volume; use action=add",
                )
            if barcode in updates:
                raise ApiError(
                    422, "duplicate_barcode", "barcode updated twice in one batch"
                )
            updates[barcode] = masks
            outcomes.append("updated")

    merged = [copy for copy in volume.copies if copy.barcode not in updates]
    for copy in volume.copies:
        if copy.barcode in updates:
            merged.append(CopyState(copy.barcode, updates[copy.barcode]))
    for barcode, masks in additions:
        merged.append(CopyState(barcode, masks))
    # Preserve a deterministic order: existing copies first, then additions.
    return BatchPlan(volume, merged, issue_index, item_masks, outcomes)


def _other_volume_barcodes(
    conn: sqlite3.Connection, volume_id: int, items: list[dict]
) -> dict[str, int]:
    """Map barcodes in this batch that already exist in a *different* volume."""
    barcodes = sorted({item["barcode"].strip() for item in items if item["barcode"].strip()})
    if not barcodes:
        return {}
    placeholders = ",".join("?" * len(barcodes))
    rows = conn.execute(
        f"""
        SELECT barcode, volume_id FROM copies
        WHERE barcode IN ({placeholders}) AND volume_id <> ?
        """,
        [*barcodes, volume_id],
    ).fetchall()
    return {row["barcode"]: row["volume_id"] for row in rows}


def _issue_changes(before: dict, after: dict, volume: VolumeState) -> list[dict]:
    before_state = {}
    for status in (HELD, MISSING, REVIEW):
        for label in before[status]:
            before_state[label] = status
    after_state = {}
    for status in (HELD, MISSING, REVIEW):
        for label in after[status]:
            after_state[label] = status
    changes = []
    for label in volume.issue_labels:
        old = before_state.get(label)
        new = after_state.get(label)
        if old != new:
            changes.append(
                {
                    "issue": label,
                    "before": old,
                    "after": new,
                    "before_zh": STATUS_LABELS.get(old, old),
                    "after_zh": STATUS_LABELS.get(new, new),
                }
            )
    return changes


def _load_batch(conn: sqlite3.Connection, volume_id: int, batch_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM batches WHERE id = ? AND volume_id = ?",
        (batch_id, volume_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "batch_not_found", f"batch {batch_id} does not exist")
    items = conn.execute(
        "SELECT * FROM batch_items WHERE batch_id = ? ORDER BY position",
        (batch_id,),
    ).fetchall()
    result = json.loads(row["result"]) if row["result"] else {}
    return {
        "batch_id": row["id"],
        "seq": row["seq"],
        "volume_id": row["volume_id"],
        "status": row["status"],
        "reason": row["reason"],
        "created_at": row["created_at"],
        "items": [
            {
                "position": item["position"],
                "barcode": item["barcode"],
                "action": item["action"],
                "candidates": json.loads(item["candidates"]),
                "outcome": item["outcome"],
                "message": item["message"],
            }
            for item in items
        ],
        "reconciliation": result.get("reconciliation"),
    }


def list_batches(conn: sqlite3.Connection, volume_id: int) -> list[dict]:
    get_volume_state(conn, volume_id)  # 404 if missing
    rows = conn.execute(
        "SELECT id FROM batches WHERE volume_id = ? ORDER BY seq", (volume_id,)
    ).fetchall()
    return [_load_batch(conn, volume_id, row["id"]) for row in rows]


def get_batch(conn: sqlite3.Connection, volume_id: int, batch_id: int) -> dict:
    get_volume_state(conn, volume_id)
    return _load_batch(conn, volume_id, batch_id)


def _store_rejected(
    conn: sqlite3.Connection,
    plan: BatchPlan,
    items: list[dict],
    reason: str,
    before: dict,
) -> dict:
    """Persist a rejected batch record (holdings are untouched)."""
    now = db.utcnow()
    cursor = conn.execute(
        """
        INSERT INTO batches (volume_id, seq, status, reason, request, result, created_at)
        VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM batches), 'rejected', ?, ?, ?, ?)
        """,
        (
            plan.volume.id,
            reason,
            json.dumps({"items": items}, ensure_ascii=False),
            json.dumps(
                {
                    "reconciliation": {
                        "before": before,
                        "after": None,
                        "changes": [],
                    }
                },
                ensure_ascii=False,
            ),
            now,
        ),
    )
    batch_id = int(cursor.lastrowid)
    conn.executemany(
        """
        INSERT INTO batch_items
            (batch_id, position, barcode, action, candidates, outcome, message)
        VALUES (?, ?, ?, ?, ?, 'rejected', ?)
        """,
        [
            (
                batch_id,
                pos,
                item["barcode"].strip(),
                item["action"],
                json.dumps(item["candidates"], ensure_ascii=False),
                reason,
            )
            for pos, item in enumerate(items)
        ],
    )
    return _load_batch(conn, plan.volume.id, batch_id)


def submit_batch(
    conn: sqlite3.Connection,
    volume_id: int,
    items: list[dict],
    *,
    preview: bool = False,
) -> tuple[dict, bool]:
    """Validate and (unless preview) commit a batch atomically.

    Returns ``(body, created)`` where ``created`` is true when a batch record
    was written (committed or rejected).
    """
    if preview:
        conn.execute("BEGIN")
        try:
            volume = get_volume_state(conn, volume_id)
            plan = _normalise_items(
                items, volume, _other_volume_barcodes(conn, volume_id, items)
            )
            before_eval = reconcile(volume)
            if not before_eval.satisfiable:
                raise ApiError(500, "holdings_inconsistent", "persisted holdings are not reconcilable")
            before = _describe(before_eval, volume)
            after_eval = evaluate(
                volume.issue_count, [copy.groups for copy in plan.merged]
            )
            if not after_eval.satisfiable:
                return (
                    {
                        "volume_id": volume_id,
                        "status": "would_reject",
                        "reason": after_eval.reason
                        or "no assignment of candidate groups is disjoint",
                        "preview": True,
                        "items": [
                            {
                                "barcode": item["barcode"].strip(),
                                "action": item["action"],
                                "candidates": item["candidates"],
                                "outcome": "would_reject",
                            }
                            for item in items
                        ],
                        "reconciliation": {
                            "before": before,
                            "after": None,
                            "changes": [],
                        },
                    },
                    False,
                )
            after = _describe(after_eval, volume)
            return (
                {
                    "volume_id": volume_id,
                    "status": "would_commit",
                    "reason": None,
                    "preview": True,
                    "items": [
                        {
                            "barcode": item["barcode"].strip(),
                            "action": item["action"],
                            "candidates": item["candidates"],
                            "outcome": plan.outcomes[pos],
                        }
                        for pos, item in enumerate(items)
                    ],
                    "reconciliation": {
                        "before": before,
                        "after": after,
                        "changes": _issue_changes(before, after, volume),
                    },
                },
                False,
            )
        finally:
            conn.execute("ROLLBACK")

    # Serialise writers before reading holdings.  The lock acquisition order is
    # the commit order, so concurrent submissions are evaluated against every
    # batch that committed before them.
    conn.execute("BEGIN IMMEDIATE")
    try:
        volume = get_volume_state(conn, volume_id)
        plan = _normalise_items(
            items, volume, _other_volume_barcodes(conn, volume_id, items)
        )
        before_eval = reconcile(volume)
        if not before_eval.satisfiable:
            raise ApiError(
                500,
                "holdings_inconsistent",
                "persisted holdings are not reconcilable; database was modified externally",
            )
        before = _describe(before_eval, volume)
        after_eval = evaluate(volume.issue_count, [copy.groups for copy in plan.merged])
        if not after_eval.satisfiable:
            reason = after_eval.reason or "no assignment of candidate groups is disjoint"
            record = _store_rejected(conn, plan, items, reason, before)
            conn.execute("COMMIT")
            return record, True

        now = db.utcnow()
        for item, masks in zip(items, plan.item_masks, strict=True):
            barcode = item["barcode"].strip()
            existing = conn.execute(
                "SELECT id FROM copies WHERE barcode = ?", (barcode,)
            ).fetchone()
            if item["action"] == "add":
                if existing is not None:
                    raise ApiError(
                        409,
                        "barcode_exists",
                        f"barcode {barcode!r} was inserted by another transaction",
                    )
                cursor = conn.execute(
                    "INSERT INTO copies (volume_id, barcode, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (volume_id, barcode, now, now),
                )
                copy_id = int(cursor.lastrowid)
            else:
                copy_id = int(existing["id"])
                conn.execute(
                    "UPDATE copies SET updated_at = ? WHERE id = ?", (now, copy_id)
                )
                conn.execute(
                    "DELETE FROM candidate_groups WHERE copy_id = ?", (copy_id,)
                )
            for pos, mask in enumerate(masks):
                group_cursor = conn.execute(
                    "INSERT INTO candidate_groups (copy_id, position) VALUES (?, ?)",
                    (copy_id, pos),
                )
                group_id = int(group_cursor.lastrowid)
                conn.executemany(
                    """
                    INSERT INTO candidate_group_issues (group_id, issue_id)
                    SELECT ?, id FROM issues WHERE volume_id = ? AND position = ?
                    """,
                    [
                        (group_id, volume_id, bit + 1)
                        for bit in range(volume.issue_count)
                        if mask >> bit & 1
                    ],
                )

        after = _describe(after_eval, volume)
        changes = _issue_changes(before, after, volume)
        cursor = conn.execute(
            """
            INSERT INTO batches (volume_id, seq, status, reason, request, result, created_at)
            VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM batches), 'committed', NULL, ?, ?, ?)
            """,
            (
                volume_id,
                json.dumps({"items": items}, ensure_ascii=False),
                json.dumps(
                    {
                        "reconciliation": {
                            "before": before,
                            "after": after,
                            "changes": changes,
                        }
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )
        batch_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO batch_items
                (batch_id, position, barcode, action, candidates, outcome, message)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            [
                (
                    batch_id,
                    pos,
                    item["barcode"].strip(),
                    item["action"],
                    json.dumps(item["candidates"], ensure_ascii=False),
                    plan.outcomes[pos],
                )
                for pos, item in enumerate(items)
            ],
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    record = _load_batch(conn, volume_id, batch_id)
    return record, True
