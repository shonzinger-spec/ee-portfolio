import numpy as np
import pytest

from fta_risk import BDD, FaultTree, and_gate, brute_force_probability, or_gate, vote_gate
from fta_risk import pump
from fta_risk.bdd import min_cut_upper_bound, rare_event_approximation


def random_tree(rng, n_events=7, n_gates=6):
    """Random coherent DAG: each gate picks 2-3 inputs among events and earlier gates (repeats likely)."""
    events = [f"E{i}" for i in range(n_events)]
    gates, names = [], list(events)
    for g in range(n_gates):
        k = int(rng.integers(2, 4))
        inputs = list(rng.choice(names, size=min(k, len(names)), replace=False))
        kind = rng.choice(["AND", "OR", "VOTE"])
        name = f"G{g}"
        if kind == "AND":
            gates.append(and_gate(name, *inputs))
        elif kind == "OR":
            gates.append(or_gate(name, *inputs))
        else:
            gates.append(vote_gate(name, int(rng.integers(1, len(inputs) + 1)), *inputs))
        names.append(name)
    return FaultTree(events, gates, gates[-1].name)


@pytest.mark.parametrize("seed", range(25))
def test_bdd_and_mocus_match_brute_force_on_random_trees(seed):
    rng = np.random.default_rng(seed)
    tree = random_tree(rng)
    q = {e: float(rng.uniform(0.01, 0.9)) for e in tree.basic_events}
    assert tree.minimal_cut_sets() == tree.brute_force_minimal_cut_sets()
    assert abs(float(BDD(tree).probability(q)) - brute_force_probability(tree, q)) < 1e-12


def test_repeated_event_is_counted_once():
    tree = FaultTree(("A", "B"), [or_gate("G", "A", "B"), and_gate("TOP", "A", "G")], "TOP")
    assert float(BDD(tree).probability({"A": 0.3, "B": 0.6})) == pytest.approx(0.3, abs=1e-15)
    assert tree.minimal_cut_sets() == [frozenset({"A"})]


def test_vote_gate_threshold():
    tree = FaultTree(("A", "B", "C"), [vote_gate("V", 2, "A", "B", "C")], "V")
    q = 0.2
    assert float(BDD(tree).probability({"A": q, "B": q, "C": q})) == pytest.approx(3 * q**2 - 2 * q**3)
    assert len(tree.minimal_cut_sets()) == 3


def test_bdd_vectorised_over_time_arrays():
    tree = pump.fault_tree("H2_UNDER_DELIVERY")
    t = np.linspace(0.01, 0.2, 5)
    q = {e: t for e in pump.BASIC_EVENTS}
    vector = BDD(tree).probability(q)
    scalar = [float(BDD(tree).probability({e: x for e in pump.BASIC_EVENTS})) for x in t]
    assert np.allclose(vector, scalar, rtol=0, atol=1e-15)


def test_importance_measures_definitions():
    tree = pump.fault_tree("ANY_HAZARD")
    q = {e: 0.01 for e in pump.BASIC_EVENTS}
    bdd = BDD(tree)
    table = bdd.importance(q)
    top = float(bdd.probability(q))
    for event, row in table.items():
        assert row["birnbaum"] >= 0
        assert 0.0 <= row["fussell_vesely"] <= 1.0
        assert row["raw"] >= 1.0 and row["rrw"] >= 1.0
        assert row["raw"] == pytest.approx(float(bdd.conditional(q, event, 1.0)) / top)


def test_bounds_order_on_pump():
    tree = pump.fault_tree("ANY_HAZARD")
    q = {e: 0.05 for e in pump.BASIC_EVENTS}
    cuts = tree.minimal_cut_sets()
    exact = float(BDD(tree).probability(q))
    assert exact <= min_cut_upper_bound(cuts, q) <= rare_event_approximation(cuts, q)


@pytest.mark.parametrize("bad", [
    lambda: FaultTree(("A",), [and_gate("G", "A", "H"), or_gate("H", "G")], "G"),
    lambda: FaultTree(("A",), [and_gate("G", "A", "Z")], "G"),
    lambda: FaultTree(("A", "B"), [vote_gate("G", 0, "A", "B")], "G"),
    lambda: FaultTree(("A",), [and_gate("G", "A", "A")], "G"),
    lambda: FaultTree((), [], "G"),
    lambda: FaultTree(("A",), [or_gate("G")], "G"),
    lambda: FaultTree(("A",), [or_gate("G", "A")._replace if False else __import__("fta_risk").Gate("G", "XOR", ("A",))], "G"),
])
def test_invalid_trees_rejected(bad):
    with pytest.raises(ValueError):
        bad()


def test_probability_out_of_range_rejected():
    bdd = BDD(FaultTree(("A",), [or_gate("G", "A")], "G"))
    for value in (-0.1, 1.1, np.nan):
        with pytest.raises(ValueError):
            bdd.probability({"A": value})
