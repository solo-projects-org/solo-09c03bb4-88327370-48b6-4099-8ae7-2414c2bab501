"""Exact reconciliation for bound-periodical candidate groups.

The problem
-----------
A volume expects ``n`` issues (``n <= 24``).  Every physical copy has between
one and three *candidate groups* of issues.  A group models a combined / bound
volume ("issues 3+4 together").  A valid reconciliation assigns exactly one
candidate group to every copy such that no issue is claimed by two copies
("同一期最多归一册").

Definitions used by the API
---------------------------
For an issue ``i``, look at the set of *all* valid assignments:

* every assignment covers ``i``        -> ``held``   (有藏)
* no assignment covers ``i``            -> ``missing`` (缺刊)
* some cover it, some do not            -> ``review``  (留待翻册)

Algorithm
---------
A reachability DP over bitmasks of used issues.  For ``n <= 24`` the state
space is at most ``2**24`` bits (2 MiB as a Python int).  A pattern mask
``free(i)`` has bit ``s`` set exactly when issue ``i`` is **not** in state
``s``; intersecting the patterns of a candidate group yields the states that
can accept that group.  Each copy widens the reachable set by

    next = OR over groups g of (reachable & free(g)) << g

Python's arbitrary precision integers make both the intersection and the
shift-over-all-states operations run in a handful of C-level big-int
operations per copy.  The DP is exact, including the "all valid assignments"
classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_ISSUES = 24
MAX_CANDIDATES_PER_COPY = 3

HELD = "held"
MISSING = "missing"
REVIEW = "review"


@dataclass(slots=True)
class Evaluation:
    """Result of reconciling one volume."""

    satisfiable: bool
    statuses: list[str] = field(default_factory=list)
    assignment: list[int] = field(default_factory=list)
    used_mask: int = 0
    reason: str | None = None

    def count(self, status: str) -> int:
        return sum(1 for s in self.statuses if s == status)


def _free_pattern(issue: int, n: int) -> int:
    """Return a mask whose bit ``s`` is set iff state ``s`` does not use ``issue``."""
    m = 1 << n
    nbytes = (m + 7) // 8
    if issue == 0:
        data = b"\x55" * nbytes
    elif issue == 1:
        data = b"\x33" * nbytes
    elif issue == 2:
        data = b"\x0f" * nbytes
    else:
        # Blocks of 2**issue states: the first half has the bit clear, the
        # second half has it set.
        half = 1 << (issue - 3)
        data = (b"\xff" * half + b"\x00" * half) * (nbytes // (2 * half))
    return int.from_bytes(data, "little")


def evaluate(
    n: int,
    candidates: list[list[int]],
    *,
    want_assignment: bool = True,
) -> Evaluation:
    """Reconcile ``n`` issues against per-copy candidate issue masks.

    ``candidates[i]`` is the list of candidate groups of copy ``i``; every
    group is a bitmask over issue indices.  No copy may be omitted and no
    issue may be used twice.
    """
    if n < 0 or n > MAX_ISSUES:
        raise ValueError(f"issue count must be between 0 and {MAX_ISSUES}")
    k = len(candidates)
    if n == 0:
        if k == 0:
            return Evaluation(True)
        return Evaluation(False, reason="no issues but copies were supplied")
    if k == 0:
        return Evaluation(True, [MISSING] * n, [], 0)
    if k > n:
        # k non-empty disjoint groups need at least k distinct issues.
        return Evaluation(
            False,
            reason=f"{k} copies cannot fit into {n} issues without sharing an issue",
        )
    for groups in candidates:
        if not groups:
            raise ValueError("every copy needs at least one candidate group")
        if len(groups) > MAX_CANDIDATES_PER_COPY:
            raise ValueError("a copy may have at most three candidate groups")
        for mask in groups:
            if mask <= 0 or mask >= (1 << n):
                raise ValueError("candidate group contains an issue outside the manifest")

    all_states = (1 << (1 << n)) - 1
    pattern: dict[int, int] = {}

    def free(issue: int) -> int:
        cached = pattern.get(issue)
        if cached is None:
            cached = _free_pattern(issue, n)
            pattern[issue] = cached
        return cached

    # layers[i] = reachable used-issue sets after the first i copies.
    layers: list[int] = [1]
    reachable = 1
    for groups in candidates:
        widened = 0
        for mask in groups:
            allowed = all_states
            remaining = mask
            while remaining:
                low = remaining & -remaining
                allowed &= free(low.bit_length() - 1)
                remaining ^= low
            widened |= (reachable & allowed) << mask
        reachable = widened
        layers.append(reachable)
        if not reachable:
            return Evaluation(
                False,
                reason="no assignment of candidate groups is disjoint",
            )

    # Exclude the empty assignment: with at least one copy every valid
    # assignment uses at least one issue.
    non_empty = reachable & ~1
    statuses: list[str] = []
    for issue in range(n):
        pattern_issue = free(issue)
        with_issue = non_empty & ~pattern_issue
        without_issue = non_empty & pattern_issue
        if not with_issue:
            statuses.append(MISSING)
        elif without_issue:
            statuses.append(REVIEW)
        else:
            statuses.append(HELD)

    assignment: list[int] = []
    used_mask = 0
    if want_assignment and reachable:
        state = (reachable & -reachable).bit_length() - 1
        used_mask = state
        choices: list[int] = [0] * k
        for index in range(k - 1, -1, -1):
            groups = candidates[index]
            for option_index, mask in enumerate(groups):
                if state & mask != mask:
                    continue
                previous = state ^ mask
                if (layers[index] >> previous) & 1:
                    choices[index] = option_index
                    state = previous
                    break
            else:  # pragma: no cover - defensive
                raise RuntimeError("failed to reconstruct a valid assignment")
        assignment = choices

    return Evaluation(True, statuses, assignment, used_mask)
