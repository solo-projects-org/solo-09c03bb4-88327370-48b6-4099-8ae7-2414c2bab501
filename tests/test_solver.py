import itertools
import random

import pytest

from app.solver import evaluate

STATUSES = ("held", "missing", "review")


def brute(n, copies):
    """Reference implementation: enumerate every assignment."""
    masks = copies
    valid = []
    for choice in itertools.product(*[range(len(g)) for g in masks]):
        used = 0
        ok = True
        for ci, gi in enumerate(choice):
            m = masks[ci][gi]
            if used & m:
                ok = False
                break
            used |= m
        if ok:
            valid.append(used)
    if not valid:
        return False, None
    statuses = []
    for b in range(n):
        with_b = any(m >> b & 1 for m in valid)
        without_b = any(not (m >> b & 1) for m in valid)
        statuses.append("held" if with_b and not without_b else ("review" if with_b else "missing"))
    return True, statuses


@pytest.mark.parametrize("seed", range(60))
def test_matches_brute_force(seed):
    rng = random.Random(seed)
    n = rng.randint(1, 8)
    k = rng.randint(0, 8)
    copies = []
    for _ in range(k):
        groups = []
        for _ in range(rng.randint(1, 3)):
            mask = 0
            for i in range(n):
                if rng.random() < 0.3:
                    mask |= 1 << i
            if not mask:
                mask = 1 << rng.randrange(n)
            groups.append(mask)
        copies.append(list(dict.fromkeys(groups)))
    result = evaluate(n, copies)
    expected_sat, expected_statuses = brute(n, copies)
    assert result.satisfiable == expected_sat
    if expected_sat:
        assert result.statuses == expected_statuses
        used = 0
        for index, option in enumerate(result.assignment):
            mask = copies[index][option]
            assert used & mask == 0
            used |= mask
        assert used == result.used_mask


def test_single_copy_two_candidates_one_is_ambiguous():
    # copy may be 1 or 2 -> both review, 3 missing
    result = evaluate(3, [[0b001, 0b010]])
    assert result.statuses == ["review", "review", "missing"]


def test_two_copies_same_group_is_unsatisfiable():
    result = evaluate(4, [[0b0001], [0b0001]])
    assert not result.satisfiable


def test_more_copies_than_issues_is_unsatisfiable():
    result = evaluate(3, [[1], [2], [4], [1]])
    assert not result.satisfiable


def test_bound_group_forces_both_issues():
    # copy A is (1+2) or (1); copy B is 2; C is 3
    result = evaluate(3, [[0b011, 0b001], [0b010], [0b100]])
    # A=1+2/B=2 conflicts; A=1 then 2 held by B -> all held
    assert result.statuses == ["held", "held", "held"]


def test_extra_issues_are_missing_not_review():
    # 4 issues, copies cover 1..3, 4 is never mentioned -> missing
    result = evaluate(4, [[0b0001], [0b0010], [0b0100]])
    assert result.statuses == ["held", "held", "held", "missing"]


def test_24_issues_is_fast():
    copies = [[1 << i, 1 << ((i + 1) % 24), 1 << ((i + 2) % 24)] for i in range(24)]
    result = evaluate(24, copies)
    assert result.satisfiable
    assert result.statuses == ["held"] * 24
