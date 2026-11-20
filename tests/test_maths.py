import math

import pytest

from paper import maths


def test_indian_grouping():
    assert maths.indian(100000) == "1,00,000"
    assert maths.indian(12345678) == "1,23,45,678"
    assert maths.indian(999) == "999"


def test_compute_and_readable():
    ex = maths.fallback_exercise("Test Co", 100, 100000, 90.0)
    maths.compute(ex)
    price_inr = 100 * 90.0
    margin = price_inr * 0.6
    assert ex["answer_value"] == math.ceil((40000 + 100000) / margin)
    assert "×" in ex["steps"][0]["shown"]


def test_rejects_unsafe_expressions():
    with pytest.raises(maths.MathsError):
        maths.safe_eval("__import__('os').system('x')", {})
    with pytest.raises(maths.MathsError):
        maths.safe_eval("a + b", {"a": 1})
