"""Fault tree structure in the style of IEC 61025: basic events joined by AND, OR and k-out-of-n gates.

A fault tree is a Boolean function of its basic events. With x_i = 1 when basic event i has occurred
(the component is failed or out of tolerance), each gate output y is

    AND:              y = x_1 x_2 ... x_n
    OR:               y = 1 - (1 - x_1)(1 - x_2) ... (1 - x_n)
    VOTE (k-of-n):    y = 1  if  x_1 + x_2 + ... + x_n >= k,  else 0

All three are monotone, so the tree is coherent: a failure can never repair the top event. A cut set
is a set of basic events whose joint occurrence makes the top event true; a minimal cut set is one
with no proper subset that is also a cut set. The top event is the OR of its minimal cut sets, each
an AND of its events:

    TOP = OR over minimal cut sets C of ( AND over i in C of x_i )

Gates may share inputs (a repeated event feeds several gates), so the tree is a directed acyclic
graph rather than a strict tree. That is why probabilities are computed with a binary decision
diagram (bdd.py), not by multiplying gate probabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Sequence

import numpy as np

GATE_KINDS = ("AND", "OR", "VOTE")
MAX_BRUTE_FORCE_EVENTS = 20  # 2**20 states is the most brute-force enumeration is allowed to build


@dataclass(frozen=True)
class Gate:
    """One logic gate. `inputs` name basic events or other gates; `k` is used by VOTE only."""

    name: str
    kind: str
    inputs: tuple[str, ...]
    k: int | None = None


def and_gate(name: str, *inputs: str) -> Gate:
    return Gate(name, "AND", tuple(inputs))


def or_gate(name: str, *inputs: str) -> Gate:
    return Gate(name, "OR", tuple(inputs))


def vote_gate(name: str, k: int, *inputs: str) -> Gate:
    return Gate(name, "VOTE", tuple(inputs), k)


class FaultTree:
    """A validated, acyclic fault tree with a named top event."""

    def __init__(self, basic_events: Sequence[str], gates: Sequence[Gate], top: str):
        self.basic_events = tuple(basic_events)
        self.gates = {gate.name: gate for gate in gates}
        self.top = top
        self._validate(gates)
        self.index = {name: i for i, name in enumerate(self.basic_events)}

    # ------------------------------------------------------------------ validation
    def _validate(self, gates: Sequence[Gate]) -> None:
        names = list(self.basic_events) + [gate.name for gate in gates]
        if len(set(names)) != len(names):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate event or gate names: {duplicates}")
        if not self.basic_events:
            raise ValueError("a fault tree needs at least one basic event")
        defined = set(names)
        for gate in gates:
            if gate.kind not in GATE_KINDS:
                raise ValueError(f"gate {gate.name}: unknown kind {gate.kind!r} (use {GATE_KINDS})")
            if not gate.inputs:
                raise ValueError(f"gate {gate.name}: no inputs")
            if len(set(gate.inputs)) != len(gate.inputs):
                raise ValueError(f"gate {gate.name}: the same input is listed twice")
            unknown = [name for name in gate.inputs if name not in defined]
            if unknown:
                raise ValueError(f"gate {gate.name}: undefined inputs {unknown}")
            if gate.kind == "VOTE":
                if gate.k is None or not 1 <= gate.k <= len(gate.inputs):
                    raise ValueError(f"gate {gate.name}: k must be 1..{len(gate.inputs)}, got {gate.k}")
            elif gate.k is not None:
                raise ValueError(f"gate {gate.name}: k is only used by VOTE gates")
        if self.top not in defined:
            raise ValueError(f"top event {self.top!r} is not defined")
        # Depth-first search with colours (1 = on the current path, 2 = done) finds any cycle.
        state: dict[str, int] = {}

        def visit(name: str, path: tuple[str, ...]) -> None:
            if name not in self.gates:
                return
            if state.get(name) == 1:
                raise ValueError(f"cycle in fault tree: {' -> '.join(path + (name,))}")
            if state.get(name) == 2:
                return
            state[name] = 1
            for child in self.gates[name].inputs:
                visit(child, path + (name,))
            state[name] = 2

        for gate in gates:
            visit(gate.name, ())

    # ------------------------------------------------------------------ helpers
    def is_gate(self, name: str) -> bool:
        return name in self.gates

    def events_under(self, name: str | None = None) -> list[str]:
        """Basic events reachable from `name` (default: top), in depth-first order of first visit."""
        seen: list[str] = []
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visited:
                return
            visited.add(node)
            if node in self.gates:
                for child in self.gates[node].inputs:
                    visit(child)
            else:
                seen.append(node)

        visit(self.top if name is None else name)
        return seen

    def subtree(self, top: str) -> "FaultTree":
        """The same events and gates with a different top (for example one hazard of several)."""
        return FaultTree(self.basic_events, list(self.gates.values()), top)

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, states: np.ndarray, node: str | None = None) -> np.ndarray:
        """Structure function on boolean states of shape (m, n_basic_events); returns (m,) booleans."""
        states = np.asarray(states, dtype=bool)
        if states.ndim != 2 or states.shape[1] != len(self.basic_events):
            raise ValueError(f"states must have shape (m, {len(self.basic_events)})")
        cache: dict[str, np.ndarray] = {}

        def value(name: str) -> np.ndarray:
            if name in cache:
                return cache[name]
            if name not in self.gates:
                result = states[:, self.index[name]]
            else:
                gate = self.gates[name]
                inputs = np.stack([value(child) for child in gate.inputs])
                if gate.kind == "AND":
                    result = inputs.all(axis=0)
                elif gate.kind == "OR":
                    result = inputs.any(axis=0)
                else:
                    result = inputs.sum(axis=0) >= gate.k
            cache[name] = result
            return result

        return value(self.top if node is None else node)

    def all_states(self) -> np.ndarray:
        """Every combination of basic-event states; row s has event i set when bit i of s is 1."""
        n = len(self.basic_events)
        if n > MAX_BRUTE_FORCE_EVENTS:
            raise ValueError(f"brute force over {n} events (2**{n} states) is not allowed")
        codes = np.arange(2**n, dtype=np.int64)
        return ((codes[:, None] >> np.arange(n)) & 1).astype(bool)

    # ------------------------------------------------------------------ cut sets
    def minimal_cut_sets(self) -> list[frozenset[str]]:
        """MOCUS (method of obtaining cut sets), top-down, followed by absorption.

        Start from the row {TOP}. Repeatedly take a row holding a gate G and replace G by its inputs:
        an AND gate puts all inputs in the same row, an OR gate splits the row into one row per
        input, and a k-of-n VOTE gate is the OR of the AND of every k-subset of its inputs. When no
        row holds a gate, every row is a cut set; absorption (A + A.B = A) removes the non-minimal ones.
        """
        finished: set[frozenset[str]] = set()
        pending = [frozenset([self.top])]
        while pending:
            row = pending.pop()
            gate_name = next((name for name in sorted(row) if name in self.gates), None)
            if gate_name is None:
                finished.add(row)
                continue
            gate = self.gates[gate_name]
            rest = row - {gate_name}
            if gate.kind == "AND":
                pending.append(rest | frozenset(gate.inputs))
            elif gate.kind == "OR":
                pending.extend(rest | {child} for child in gate.inputs)
            else:
                pending.extend(rest | frozenset(combo) for combo in combinations(gate.inputs, gate.k))
        return sort_cut_sets(absorb(finished))

    def brute_force_minimal_cut_sets(self) -> list[frozenset[str]]:
        """Minimal cut sets by enumeration: states where TOP holds and clearing any one event clears it.

        For a coherent tree this is exactly the set of minimal cut sets, so it checks MOCUS
        independently. Only usable for small trees (2**n states).
        """
        top_true = self.evaluate(self.all_states())
        n = len(self.basic_events)
        minimal = []
        for code in np.flatnonzero(top_true):
            bits = [i for i in range(n) if code >> i & 1]
            if all(not top_true[code & ~(1 << i)] for i in bits):
                minimal.append(frozenset(self.basic_events[i] for i in bits))
        return sort_cut_sets(minimal)

    def cut_set_masks(self, cut_sets: Iterable[frozenset[str]] | None = None) -> list[int]:
        """Cut sets as integer bit masks over basic-event indices."""
        cut_sets = self.minimal_cut_sets() if cut_sets is None else cut_sets
        return [sum(1 << self.index[name] for name in cut_set) for cut_set in cut_sets]


def absorb(cut_sets: Iterable[frozenset[str]]) -> list[frozenset[str]]:
    """Keep only cut sets that contain no other cut set (Boolean absorption A + A.B = A)."""
    ordered = sorted(set(cut_sets), key=len)
    kept: list[frozenset[str]] = []
    for candidate in ordered:
        if not any(existing <= candidate for existing in kept):
            kept.append(candidate)
    return kept


def sort_cut_sets(cut_sets: Iterable[frozenset[str]]) -> list[frozenset[str]]:
    return sorted(cut_sets, key=lambda c: (len(c), sorted(c)))
