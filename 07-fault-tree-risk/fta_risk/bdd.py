"""Exact top-event probability with a reduced ordered binary decision diagram (ROBDD).

Shannon decomposition on one basic event x at a time:

    f = x . f|x=1  +  (1 - x) . f|x=0                                         (1)

A BDD node is (x, low = f|x=0, high = f|x=1). Building the diagram with a fixed variable order and
merging identical nodes gives a graph in which every path visits each event at most once and the two
branches of a node are disjoint, so with independent basic events

    P(node) = q_x . P(high) + (1 - q_x) . P(low),   P(0) = 0, P(1) = 1          (2)

is exact even when an event feeds several gates. Multiplying gate probabilities (AND = product,
OR = 1 - product of complements) is exact only when no event is repeated.

Importance measures at a given set of event probabilities q (Q = P(TOP)):

    Birnbaum            I_B(i)  = Q(q_i = 1) - Q(q_i = 0)  = dQ/dq_i                (3)
    Fussell-Vesely      FV(i)   = (Q - Q(q_i = 0)) / Q                                (4)
    risk achievement    RAW(i)  = Q(q_i = 1) / Q                                      (5)
    risk reduction      RRW(i)  = Q / Q(q_i = 0)                                      (6)

Q is linear in each q_i (eq. 2), so (3) is the exact partial derivative.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np

from .tree import FaultTree

FALSE, TRUE = 0, 1


class BDD:
    """ROBDD of one fault tree's top event."""

    def __init__(self, tree: FaultTree, order: Sequence[str] | None = None):
        self.tree = tree
        self.order = tuple(tree.events_under() if order is None else order)
        missing = set(tree.events_under()) - set(self.order)
        if missing:
            raise ValueError(f"variable order is missing events {sorted(missing)}")
        self.level = {name: i for i, name in enumerate(self.order)}
        # nodes[i] = (level, low, high); ids 0 and 1 are the terminals.
        self.nodes: list[tuple[int, int, int]] = [(len(self.order), -1, -1), (len(self.order), -1, -1)]
        self._unique: dict[tuple[int, int, int], int] = {}
        self._apply_cache: dict[tuple[str, int, int], int] = {}
        self.root = self._build(tree.top, {})

    # ------------------------------------------------------------------ construction
    def _mk(self, level: int, low: int, high: int) -> int:
        if low == high:  # redundant test
            return low
        key = (level, low, high)
        node = self._unique.get(key)
        if node is None:
            node = len(self.nodes)
            self.nodes.append(key)
            self._unique[key] = node
        return node

    def _apply(self, op: str, u: int, v: int) -> int:
        if op == "AND":
            if u == FALSE or v == FALSE:
                return FALSE
            if u == TRUE:
                return v
            if v == TRUE or u == v:
                return u
        else:
            if u == TRUE or v == TRUE:
                return TRUE
            if u == FALSE:
                return v
            if v == FALSE or u == v:
                return u
        if u > v:  # both operations are commutative
            u, v = v, u
        key = (op, u, v)
        cached = self._apply_cache.get(key)
        if cached is not None:
            return cached
        level_u, low_u, high_u = self.nodes[u]
        level_v, low_v, high_v = self.nodes[v]
        level = min(level_u, level_v)
        u0, u1 = (low_u, high_u) if level_u == level else (u, u)
        v0, v1 = (low_v, high_v) if level_v == level else (v, v)
        result = self._mk(level, self._apply(op, u0, v0), self._apply(op, u1, v1))
        self._apply_cache[key] = result
        return result

    def _threshold(self, k: int, children: tuple[int, ...], memo: dict) -> int:
        """At least k of `children` true: T(k, f1..fn) = f1 . T(k-1, f2..fn) + T(k, f2..fn)."""
        if k <= 0:
            return TRUE
        if len(children) < k:
            return FALSE
        key = (k, children)
        if key not in memo:
            first, rest = children[0], children[1:]
            memo[key] = self._apply(
                "OR", self._apply("AND", first, self._threshold(k - 1, rest, memo)), self._threshold(k, rest, memo))
        return memo[key]

    def _build(self, name: str, memo: dict[str, int]) -> int:
        if name in memo:
            return memo[name]
        if not self.tree.is_gate(name):
            result = self._mk(self.level[name], FALSE, TRUE)
        else:
            gate = self.tree.gates[name]
            children = tuple(self._build(child, memo) for child in gate.inputs)
            if gate.kind == "VOTE":
                result = self._threshold(gate.k, children, {})
            else:
                result = children[0]
                for child in children[1:]:
                    result = self._apply(gate.kind, result, child)
        memo[name] = result
        return result

    @property
    def size(self) -> int:
        """Internal (non-terminal) nodes."""
        return len(self.nodes) - 2

    # ------------------------------------------------------------------ evaluation
    def probability(self, q: Mapping[str, float | np.ndarray]) -> np.ndarray:
        """P(TOP) by eq. (2). q maps event names to probabilities (scalars or equal-shape arrays)."""
        values = {name: np.asarray(q[name], dtype=float) for name in self.order}
        for name, value in values.items():
            if np.any(value < 0.0) or np.any(value > 1.0) or np.any(~np.isfinite(value)):
                raise ValueError(f"probability of {name} must be within [0, 1]")
        shape = np.broadcast_shapes(*(value.shape for value in values.values()))
        prob: list[np.ndarray] = [np.zeros(shape), np.ones(shape)]
        for level, low, high in self.nodes[2:]:  # children always have smaller ids
            p = values[self.order[level]]
            prob.append(p * prob[high] + (1.0 - p) * prob[low])
        return prob[self.root]

    def conditional(self, q: Mapping[str, float | np.ndarray], event: str, value: float) -> np.ndarray:
        changed = dict(q)
        changed[event] = np.full(np.shape(q[event]), float(value))
        return self.probability(changed)

    def birnbaum(self, q: Mapping[str, float | np.ndarray], event: str) -> np.ndarray:
        """Eq. (3). Events outside this top event's tree have I_B = 0."""
        if event not in self.level:
            return np.zeros(np.shape(q[event]))
        return self.conditional(q, event, 1.0) - self.conditional(q, event, 0.0)

    def importance(self, q: Mapping[str, float]) -> dict[str, dict[str, float]]:
        """Eqs. (3)-(6) for every event in the tree, at scalar probabilities q."""
        top = float(self.probability(q))
        table = {}
        for event in self.order:
            q_one = float(self.conditional(q, event, 1.0))
            q_zero = float(self.conditional(q, event, 0.0))
            table[event] = {
                "birnbaum": q_one - q_zero,
                "fussell_vesely": (top - q_zero) / top if top > 0 else 0.0,
                "raw": q_one / top if top > 0 else np.inf,
                "rrw": top / q_zero if q_zero > 0 else np.inf,
            }
        return table


def brute_force_probability(tree: FaultTree, q: Mapping[str, float]) -> float:
    """P(TOP) as the sum over all 2**n event states of P(state) for states where TOP holds."""
    states = tree.all_states()
    top = tree.evaluate(states)
    p = np.array([float(q[name]) for name in tree.basic_events])
    state_prob = np.prod(np.where(states, p, 1.0 - p), axis=1)
    return float(state_prob[top].sum())


def rare_event_approximation(cut_sets, q: Mapping[str, float]) -> float:
    """Q ~ sum over minimal cut sets of prod q_i (an upper bound for small q, can exceed 1)."""
    return float(sum(np.prod([q[name] for name in cut_set]) for cut_set in cut_sets))


def min_cut_upper_bound(cut_sets, q: Mapping[str, float]) -> float:
    """Q <= 1 - prod over cut sets (1 - P(cut set)) (Esary-Proschan bound for coherent trees)."""
    return float(1.0 - np.prod([1.0 - np.prod([q[name] for name in cut_set]) for cut_set in cut_sets]))
