"""FastAPI application for periodical gap reconciliation."""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import db, service
from .schemas import BatchSubmit, VolumeCreate
from .solver import MAX_CANDIDATES_PER_COPY, MAX_ISSUES

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "periodicals.sqlite3"


def _db_path() -> Path:
    return Path(os.environ.get("PERIODICALS_DB", str(DEFAULT_DB)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    path = _db_path()
    db.init_db(path)
    app.state.db_path = path
    yield


app = FastAPI(
    title="期刊补缺核对 API",
    version="1.0.0",
    description=(
        "给一卷旧期刊补缺前，把「有藏 / 缺刊 / 待翻册」分清楚。\n\n"
        "* 一卷最多 24 期，按条码成批增补或更正馆藏。\n"
        f"* 每册最多 {MAX_CANDIDATES_PER_COPY} 组候选期次；每组内期次必须一并计入。\n"
        "* 所有合法方案都覆盖的期次为「有藏」，都未覆盖的为「缺刊」，其余为「待翻册」。\n"
        "* 一批改动与其余馆藏一起核对；无解则整批退回，并保留原馆藏与审计记录。\n"
        "* 并发提交以实际入库（提交）顺序为准。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(service.ApiError)
async def _api_error_handler(request: Request, exc: service.ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


def get_conn(request: Request):
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


@app.get("/healthz", tags=["system"])
def healthz():
    return {"status": "ok", "max_issues": MAX_ISSUES}


@app.post("/volumes", status_code=201, tags=["volumes"])
def create_volume(payload: VolumeCreate, conn: Conn):
    """Create a volume manifest (1–24 ordered issue labels)."""
    return service.create_volume(
        conn,
        title=payload.title,
        volume_label=payload.volume_label,
        issue_labels=payload.issues,
    )


@app.get("/volumes/{volume_id}", tags=["volumes"])
def read_volume(volume_id: int, conn: Conn):
    """Show the manifest, current copies and the live gap report."""
    return service.volume_snapshot(service.get_volume_state(conn, volume_id))


@app.get("/volumes/{volume_id}/gaps", tags=["reconciliation"])
def read_gaps(volume_id: int, conn: Conn):
    """Current 有藏 / 缺刊 / 待翻册 classification."""
    return service.gap_report(conn, volume_id)


@app.post(
    "/volumes/{volume_id}/batches",
    status_code=201,
    tags=["batches"],
    responses={
        200: {"description": "Preview only (`preview=true`); nothing was written."},
        201: {"description": "Committed or rejected batch record."},
    },
)
def submit_batch(volume_id: int, payload: BatchSubmit, conn: Conn):
    """Submit one batch of copy additions/corrections.

    The batch is merged with all other holdings and reconciled as a whole.  If
    no disjoint assignment exists the batch is rejected in full: holdings and
    the gap report are unchanged, but the attempt stays queryable.

    With ``preview=true`` the same validation and reconciliation runs inside a
    read transaction that is rolled back, so the response shows the would-be
    result and deltas without persisting anything.
    """
    items = [
        {
            "barcode": item.barcode,
            "action": item.action,
            "candidates": item.candidates,
        }
        for item in payload.items
    ]
    body, created = service.submit_batch(
        conn, volume_id, items, preview=payload.preview
    )
    if payload.preview:
        return JSONResponse(status_code=200, content=body)
    return JSONResponse(status_code=201, content=body)


@app.get("/volumes/{volume_id}/batches", tags=["batches"])
def list_batches(volume_id: int, conn: Conn):
    """Audit trail of committed and rejected batches, in commit order."""
    return service.list_batches(conn, volume_id)


@app.get("/volumes/{volume_id}/batches/{batch_id}", tags=["batches"])
def read_batch(volume_id: int, batch_id: int, conn: Conn):
    return service.get_batch(conn, volume_id, batch_id)
