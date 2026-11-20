from __future__ import annotations

import pytest

from lab import loadgen

MIX = {"short": 0.6, "medium": 0.3, "long": 0.1}
CLASSES = {"short": {"words": 50, "max_tokens": 64}, "medium": {"words": 400, "max_tokens": 64}, "long": {"words": 1200, "max_tokens": 64}}


def test_arrivals_are_deterministic():
    segs = [{"at": 0, "rps_x": 1.0, "mix": MIX}]
    a = loadgen.build_arrivals(segs, 100, 2.0, seed=7)
    b = loadgen.build_arrivals(segs, 100, 2.0, seed=7)
    c = loadgen.build_arrivals(segs, 100, 2.0, seed=8)
    assert a == b
    assert a != c


def test_arrival_rate_matches_target():
    arr = loadgen.build_arrivals([{"at": 0, "rps_x": 1.0, "mix": MIX}], 2000, 2.0, seed=1)
    assert 3600 <= len(arr) <= 4400  # E=4000, sd~63: +-10% is >6 sigma
    assert all(0 <= x.t_sched < 2000 for x in arr)
    assert [x.t_sched for x in arr] == sorted(x.t_sched for x in arr)
    assert [x.idx for x in arr] == list(range(len(arr)))


def test_burst_segment_raises_rate_threefold():
    segs = [{"at": 0, "rps_x": 1.0, "mix": MIX}, {"at": 1000, "rps_x": 3.0}, {"at": 2000, "rps_x": 1.0}]
    arr = loadgen.build_arrivals(segs, 3000, 2.0, seed=3)
    n1 = sum(1 for x in arr if x.t_sched < 1000)
    n2 = sum(1 for x in arr if 1000 <= x.t_sched < 2000)
    assert 2.7 <= n2 / n1 <= 3.3


def test_mix_shift_and_inheritance():
    shifted = {"short": 0.2, "medium": 0.3, "long": 0.5}
    segs = [{"at": 0, "rps_x": 1.0, "mix": MIX}, {"at": 1000, "rps_x": 1.0, "mix": shifted}, {"at": 2000, "rps_x": 1.0}]
    arr = loadgen.build_arrivals(segs, 3000, 3.0, seed=5)

    def frac_long(lo, hi):
        sel = [x for x in arr if lo <= x.t_sched < hi]
        return sum(1 for x in sel if x.cls == "long") / len(sel)

    assert 0.07 <= frac_long(0, 1000) <= 0.13
    assert 0.45 <= frac_long(1000, 2000) <= 0.55
    assert 0.45 <= frac_long(2000, 3000) <= 0.55  # third segment inherits the shifted mix


@pytest.mark.parametrize(
    "segs,msg",
    [
        ([], "no load segments"),
        ([{"at": 5, "rps_x": 1.0, "mix": MIX}], "start at t=0"),
        ([{"at": 0, "rps_x": 1.0}], "define `mix`"),
        ([{"at": 0, "rps_x": 1.0, "mix": {"short": 0.5, "long": 0.4}}], "sum to 1"),
    ],
)
def test_invalid_segments_rejected(segs, msg):
    with pytest.raises(ValueError, match=msg):
        loadgen.build_arrivals(segs, 10, 1.0, seed=1)


def test_bodies_are_deterministic_unique_and_sized():
    arr = loadgen.build_arrivals([{"at": 0, "rps_x": 1.0, "mix": MIX}], 60, 2.0, seed=9)
    b1 = loadgen.prepare_bodies(arr, CLASSES, seed=9)
    b2 = loadgen.prepare_bodies(arr, CLASSES, seed=9)
    assert b1 == b2
    assert len({b["prompt"] for b in b1}) > 0.95 * len(b1)  # prompts differ => no accidental cache hits
    for a, b in zip(arr, b1):
        assert len(b["prompt"].split()) == CLASSES[a.cls]["words"]
        assert b["prompt_class"] == a.cls


def test_weight_matches_gateway(apps_modules):
    gw = apps_modules["gateway_app"]
    for prompt, mt in [("hello", 64), ("x" * 1001, 8), ("é" * 37, 512)]:
        assert loadgen.weight_of(prompt, mt) == gw.weight_of(prompt, mt)
