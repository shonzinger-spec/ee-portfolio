import numpy as np
import pytest

from fta_risk import IndependentComponentChain, TwoStateExponential, pump, risk, simulate
from fta_risk.components import Intervals
from fta_risk.montecarlo import intersect, union


def iv(traj, start, end):
    return Intervals(np.array(traj, dtype=np.int64), np.array(start, float), np.array(end, float))


def test_intersection_and_union_exact_on_hand_built_intervals():
    a = iv([0, 0, 1], [1, 10, 0], [5, 12, 3])
    b = iv([0, 1], [4, 2], [11, 8])
    both = intersect([a, b])
    assert list(zip(both.traj, both.start, both.end)) == [(0, 4, 5), (0, 10, 11), (1, 2, 3)]
    either = union([a, b])
    assert list(zip(either.traj, either.start, either.end)) == [(0, 1, 12), (1, 0, 8)]


def test_touching_intervals_do_not_overlap():
    a = iv([0], [0], [5])
    b = iv([0], [5], [9])
    assert intersect([a, b]).traj.size == 0


def test_single_event_hazard_counts_its_failures():
    model = TwoStateExponential("A", 1e-3, 10.0)
    result = simulate([model], {"T": [frozenset({"A"})]}, 5000.0, 20_000, seed=1)
    assert np.array_equal(result.hazards["T"].onsets, result.failure_counts["A"])


def test_simulation_reproducible_and_validated():
    models = [TwoStateExponential("A", 1e-3, 10.0), TwoStateExponential("B", 2e-3, 20.0)]
    cuts = {"T": [frozenset({"A", "B"})]}
    one = simulate(models, cuts, 5000.0, 5000, seed=3, chunk_size=1000)
    two = simulate(models, cuts, 5000.0, 5000, seed=3, chunk_size=1000)
    assert np.array_equal(one.hazards["T"].onsets, two.hazards["T"].onsets)
    with pytest.raises(ValueError):
        simulate(models, {"T": [frozenset({"C"})]}, 5000.0, 10, seed=1)
    with pytest.raises(ValueError):
        simulate(models, cuts, -1.0, 10, seed=1)


def test_markov_single_component_and_parallel_pair():
    lam, mu = 1e-3, 1e-1
    single = IndependentComponentChain([lam], [mu], [1])
    assert single.mttf_from_all_up() == pytest.approx(1 / lam)
    assert single.top_frequency() == pytest.approx(lam * mu / (lam + mu))
    pair = IndependentComponentChain([lam, lam], [mu, mu], [3])
    assert pair.mttf_from_all_up() == pytest.approx((3 * lam + mu) / (2 * lam**2))


def test_markov_steady_state_is_product_form():
    lam, mu = np.array([1e-3, 2e-3, 5e-4]), np.array([0.05, 0.02, 0.1])
    chain = IndependentComponentChain(lam, mu, [3, 4])
    q = lam / (lam + mu)
    states = np.arange(8)
    product = np.prod(np.where((states[:, None] >> np.arange(3)) & 1, q, 1 - q), axis=1)
    assert np.allclose(chain.steady_state(), product, atol=1e-14)
    assert np.allclose(chain.generator.sum(axis=1), 0.0)


def test_markov_first_passage_cdf_mean_matches_mttf():
    chain = IndependentComponentChain([1e-3, 2e-3], [0.05, 0.05], [3])
    t = np.linspace(0, 3e5, 3001)  # mean of a survival curve = integral of it; tail beyond 3e5 h ~ e^-22
    mean = np.trapezoid(1 - chain.first_passage_cdf(t), t)
    assert mean == pytest.approx(chain.mttf_from_all_up(), rel=1e-3)


def test_risk_levels_and_policy():
    assert risk.probability_level(0.0) == "Improbable"
    assert risk.probability_level(1e-5) == "Remote"
    assert risk.probability_level(9.99e-6) == "Improbable"
    assert risk.probability_level(0.5) == "Frequent"
    assert risk.acceptable(3, "Remote") and not risk.acceptable(3, "Occasional")
    assert risk.acceptable(4, "Improbable") and not risk.acceptable(4, "Remote")
    with pytest.raises(ValueError):
        risk.acceptable(6, "Remote")
    with pytest.raises(ValueError):
        risk.probability_level(-1.0)


def test_pump_model_consistent():
    tree = pump.fault_tree()
    assert set(tree.events_under()) == set(pump.BASIC_EVENTS)
    assert [m.name for m in pump.components()] == list(pump.BASIC_EVENTS)
    assert pump.components(pump.CONTROLLED_DESIGN)[pump.BASIC_EVENTS.index("VREF_DRIFT")].test_interval_h == 72.0


def test_two_latent_faults_on_one_test_grid_match_vesely():
    """Both restored at the same test instants; MC onsets must equal int (w_a q_b + q_a w_b) dt (review item 1)."""
    from fta_risk import BDD, Exponential, FaultTree, PeriodicallyTested, and_gate
    from fta_risk.analysis import component_curves, expected_onsets, hour_midpoints
    models = [PeriodicallyTested("A", Exponential(4e-3), 72.0), PeriodicallyTested("B", Exponential(6e-3), 72.0)]
    tree = FaultTree(("A", "B"), [and_gate("T", "A", "B")], "T")
    q, w = component_curves(models, hour_midpoints(20_000.0))
    analytic = expected_onsets(BDD(tree), q, w)
    result = simulate(models, {"T": tree.minimal_cut_sets()}, 20_000.0, 40_000, seed=11)
    mean, se = result.hazards["T"].expected_onsets()
    assert abs(mean - analytic) < 4 * se
    product_of_averages = np.mean(q["A"]) * np.mean(q["B"])
    assert np.mean(q["A"] * q["B"]) > 1.2 * product_of_averages  # in-phase sawtooths (review item 4)


def test_window_counts_split_the_total():
    model = TwoStateExponential("A", 1e-3, 10.0)
    windows = ((0.0, 1000.0), (1000.0, 3000.0), (3000.0, 5000.0))
    result = simulate([model], {"T": [frozenset({"A"})]}, 5000.0, 3000, seed=2, windows_h=windows)
    hazard = result.hazards["T"]
    assert np.array_equal(hazard.window_onsets.sum(axis=0), hazard.onsets)
    with pytest.raises(ValueError):
        simulate([model], {"T": [frozenset({"A"})]}, 5000.0, 10, seed=2, windows_h=((4000.0, 6000.0),))
