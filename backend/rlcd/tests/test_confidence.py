import pytest

from rlcd.confidence import choice_confidence, noul_confidence, score_confidence


@pytest.mark.parametrize("p,expected", [(0.5, 0.0), (1.0, 1.0), (0.0, 1.0), (0.8, 0.6), (0.2, 0.6)])
def test_noul(p, expected):
    assert noul_confidence(p) == pytest.approx(expected)


def test_choice_worked_example():
    assert choice_confidence([0.6, 0.3, 0.1]) == pytest.approx(0.4)
    assert choice_confidence([1 / 3] * 3) == pytest.approx(0.0)
    assert choice_confidence([1.0, 0.0]) == pytest.approx(1.0)
    assert choice_confidence([0.5, 0.5]) == pytest.approx(0.0)


def test_score_worked_examples():
    assert score_confidence([0, 0.5, 0.5]) == pytest.approx(0.25)      # split between adjacent levels
    assert score_confidence([0.5, 0, 0.5]) == pytest.approx(0.0)       # split between opposite ends
    assert score_confidence([0, 0.57, 0.43]) == pytest.approx(0.355)
    assert score_confidence([0, 0, 1]) == pytest.approx(1.0)
    assert score_confidence([0.25] * 4) < 0.3


def test_score_is_distance_aware_where_choice_is_not():
    near, far = [0.6, 0.4, 0.0, 0.0, 0.0], [0.6, 0.0, 0.0, 0.0, 0.4]
    assert choice_confidence(near) == pytest.approx(choice_confidence(far))
    assert score_confidence(near) > score_confidence(far)
