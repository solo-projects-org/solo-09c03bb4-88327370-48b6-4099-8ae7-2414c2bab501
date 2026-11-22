"""Pydantic request/response models for the reconciliation API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class VolumeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    volume_label: str = Field(min_length=1, max_length=100)
    issues: list[str] = Field(
        min_length=1,
        max_length=24,
        description="Ordered issue labels; the manifest of one volume (max 24).",
    )


class CandidateGroup(BaseModel):
    """One candidate reading of a copy, e.g. a bound volume ``['3', '4']``."""

    model_config = ConfigDict(extra="forbid")

    issues: list[str] = Field(min_length=1, max_length=24)


class BatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    barcode: str = Field(min_length=1, max_length=100)
    action: Literal["add", "update"]
    candidates: list[list[str]] = Field(
        min_length=1,
        max_length=3,
        description="At most three candidate groups; one group is chosen per copy.",
    )


class BatchSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BatchItem] = Field(min_length=1, max_length=24)
    preview: bool = Field(
        default=False,
        description="Validate and show the reconciliation delta without writing.",
    )


class IssueDelta(BaseModel):
    issue: str
    before: str | None
    after: str | None
    before_zh: str | None
    after_zh: str | None


class GapReport(BaseModel):
    volume_id: int
    issues: list[str]
    held: list[str]
    missing: list[str]
    review: list[str]
    summary: dict[str, int]
    labels_zh: dict[str, str]


class CopyView(BaseModel):
    barcode: str
    candidates: list[list[str]]


class VolumeView(BaseModel):
    volume_id: int
    title: str
    volume_label: str
    issues: list[str]
    copies: list[CopyView]
    gap_report: dict
