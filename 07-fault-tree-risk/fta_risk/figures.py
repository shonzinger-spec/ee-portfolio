"""Figures for main.py (matplotlib, saved as PNG to docs/img/)."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

from . import pump, risk  # noqa: E402
from .analysis import brownian_drift_paths, top_intensity  # noqa: E402
from .components import HOURS_PER_YEAR  # noqa: E402

DAYS = 24.0
GATE_SYMBOL = {"AND": "AND", "OR": "OR"}


def make_all(img_dir, initial, controlled, estimates, chain, twin_first_onsets, vref, crossings, drift_rng_seed):
    fault_tree_diagram(img_dir / "fault_tree.png", initial)
    component_models(img_dir / "component_models.png", initial, controlled, vref, crossings, drift_rng_seed)
    hazard_intensity(img_dir / "hazard_intensity.png", initial, controlled)
    markov_first_passage(img_dir / "markov_first_passage.png", chain, twin_first_onsets)
    risk_matrix(img_dir / "risk_matrix.png", estimates)
    importance(img_dir / "importance.png", initial, controlled)


# ---------------------------------------------------------------------------------------- tree
def _layout(tree, name, depth, x_next, nodes, edges, seen):
    """Tidy tree layout; a repeated basic event is drawn again (marked) under each parent."""
    if tree.is_gate(name):
        xs = []
        for child in tree.gates[name].inputs:
            child_x = _layout(tree, child, depth + 1, x_next, nodes, edges, seen)
            xs.append(child_x)
            edges.append((name, depth, child_x, depth + 1))
        x = float(np.mean([xs[0], xs[-1]]))
        nodes.append((name, x, depth, True, False))
        return x
    x = x_next[0]
    x_next[0] += 1.0
    nodes.append((name, x, depth, False, name in seen))
    seen.add(name)
    return x


def fault_tree_diagram(path, initial):
    tree = initial["trees"]["ANY_HAZARD"]
    nodes, edges = [], []
    _layout(tree, tree.top, 0, [0.0], nodes, edges, set())
    position = {}
    for name, x, depth, _, _ in nodes:
        position.setdefault((name, depth), x)
    models = {m.name: m for m in initial["models"]}
    fig, ax = plt.subplots(figsize=(17, 8.2))
    x_of = {}
    for name, x, depth, is_gate, _ in nodes:
        if is_gate:
            x_of[name] = x
    for parent, depth, child_x, child_depth in edges:
        px = x_of[parent]
        ax.plot([px, px, child_x, child_x], [-depth - 0.28, -depth - 0.55, -depth - 0.55, -child_depth + 0.28],
                color="0.45", lw=0.9, zorder=1)
    for name, x, depth, is_gate, repeated in nodes:
        y = -depth
        if is_gate:
            gate = tree.gates[name]
            symbol = f"{gate.k}-of-{len(gate.inputs)}" if gate.kind == "VOTE" else GATE_SYMBOL[gate.kind]
            hazard = name in pump.HAZARD_TOPS
            colour = "#f4c7c3" if hazard else "#d4e3f5"
            ax.add_patch(FancyBboxPatch((x - 0.46, y - 0.24), 0.92, 0.48, boxstyle="round,pad=0.02",
                                        fc=colour, ec="0.25", lw=1.0, zorder=2))
            ax.text(x, y + 0.07, name.replace("_", " "), ha="center", va="center", fontsize=5.8, zorder=3)
            ax.text(x, y - 0.13, symbol, ha="center", va="center", fontsize=7.5, weight="bold", zorder=3)
        else:
            model = models[name]
            latent = not model.revealed
            ax.add_patch(plt.Circle((x, y), 0.27, fc="#fff7d6" if latent else "#e3f2d9", ec="0.25",
                                    lw=1.0, ls="--" if repeated else "-", zorder=2))
            ax.text(x, y + 0.04, name.replace("_", "\n", 1), ha="center", va="center", fontsize=6.2, zorder=3)
            ax.text(x, y - 0.38, f"q {initial['q_bar'][name]:.1e}", ha="center", va="top", fontsize=6, color="0.3")
    ax.set_xlim(-0.8, max(x for _, x, *_ in nodes) + 0.8)
    ax.set_ylim(-max(d for *_, d, _, _ in [(n[0], n[1], n[2], n[3], n[4]) for n in nodes]) - 0.9, 0.6)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Fault tree of the hypothetical insulin pump (IEC 61025 gates). Circles: basic events, "
                 "yellow = latent (found at a periodic test), green = revealed; dashed = repeated event; "
                 "q = life-average probability, initial design", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------------------------------- components
def component_models(path, initial, controlled, vref, crossings, seed):
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    models = {m.name: m for m in initial["models"]}
    t = np.linspace(0.0, 30 * DAYS, 3000)
    ax = axes[0, 0]
    for name, style in (("CUTOFF_FAIL", "-"), ("ALARM_FAIL", "-"), ("DRV_LATCH", "--"), ("OCCL", ":")):
        ax.plot(t / DAYS, models[name].unavailability(t), style, label=f"{name} ({'latent' if not models[name].revealed else 'revealed'})")
    ax.set_yscale("log")
    ax.set_ylim(1e-6, 2e-2)
    ax.set_xlabel("days in service")
    ax.set_ylabel("q(t)")
    ax.set_title("Unavailability: latent faults build up until the 72 h self-test", fontsize=9)
    ax.legend(fontsize=7)

    ax = axes[0, 1]
    rng = np.random.default_rng(seed)
    step = 24.0
    n_steps = int(pump.SERVICE_LIFE_H / step)
    paths = np.cumsum(vref.drift_per_h * step + vref.diffusion_per_sqrt_h * np.sqrt(step) * rng.standard_normal((40, n_steps)), axis=1)
    years = np.arange(1, n_steps + 1) * step / HOURS_PER_YEAR
    for p in paths:
        ax.plot(years, p, lw=0.6, color="0.55")
    ax.axhline(vref.tolerance, color="tab:red", lw=1.2, label=f"tolerance {vref.tolerance:g} %")
    for k in range(1, 4):
        ax.axvline(k, color="tab:blue", lw=0.6, ls=":")
    ax.set_xlabel("years")
    ax.set_ylabel("reference error (%)")
    ax.set_title("VREF drift: 40 sample paths of v t + sigma W(t); dotted = yearly check", fontsize=9)
    ax.legend(fontsize=7)

    ax = axes[1, 0]
    grid = np.linspace(1.0, pump.SERVICE_LIFE_H, 400)
    ax.plot(grid / HOURS_PER_YEAR, vref.cdf(grid), label="inverse Gaussian, eq. (1)")
    finite = np.sort(crossings[np.isfinite(crossings)])
    ax.step(finite / HOURS_PER_YEAR, np.arange(1, finite.size + 1) / crossings.size, where="post", lw=0.9,
            label=f"{crossings.size} Brownian paths, bridge-corrected")
    ax.set_xlabel("years")
    ax.set_ylabel("P(out of tolerance by t)")
    ax.set_title("Drift first passage: analytic vs path simulation", fontsize=9)
    ax.legend(fontsize=7)

    ax = axes[1, 1]
    life = np.arange(0.0, pump.SERVICE_LIFE_H, 6.0) + 0.5
    for data, label in ((initial, "yearly service check"), (controlled, "power-up cross-check (72 h)")):
        model = {m.name: m for m in data["models"]}["VREF_DRIFT"]
        tau = model.test_interval_h
        peaks = np.arange(tau - 0.5, pump.SERVICE_LIFE_H, tau)  # value just before each check
        curve = life if tau > 1000 else peaks
        ax.plot(curve / HOURS_PER_YEAR, np.maximum(model.unavailability(curve), 1e-12), lw=1,
                label=label if tau > 1000 else label + ", peak before each check")
    ax.set_yscale("log")
    ax.set_ylim(1e-9, 1e-1)
    ax.set_xlabel("years")
    ax.set_ylabel("q(t) of VREF_DRIFT")
    ax.set_title("Risk control: checking the reference at every power-up", fontsize=9)
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


# ---------------------------------------------------------------------------------------- hazards
def hazard_intensity(path, initial, controlled):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))
    years = initial["midpoints"] / HOURS_PER_YEAR
    for data, label, style in ((initial, "initial", "-"), (controlled, "with risk control", "--")):
        for top, colour in (("H2_UNDER_DELIVERY", "tab:blue"), ("H1_OVER_DELIVERY", "tab:red")):
            w_top = top_intensity(data["bdds"][top], data["q"], data["w"])
            smooth = np.convolve(w_top, np.ones(72) / 72, mode="valid")  # 72 h average for display
            short = top.split("_")[0]
            if label == "initial" or short == "H2":
                name = "H1 (same in both designs)" if short == "H1" else f"H2 {label}"
                axes[0].plot(years[35:35 + smooth.size], smooth * HOURS_PER_YEAR, style, color=colour, lw=1, label=name)
            cumulative = np.cumsum(w_top)
            mc = data["mc"]
            edges = mc.onset_bins_h
            mc_cum = np.cumsum(mc.hazards[top].onset_histogram) / mc.n_traj
            axes[1].plot(years, cumulative, style, color=colour, lw=1, label=f"{short} {label}: analytic")
            axes[1].plot(edges[1:] / HOURS_PER_YEAR, mc_cum, "o", ms=3, color=colour, mfc="none" if style == "--" else colour)
    axes[0].set_yscale("log")
    axes[0].set_xlabel("years")
    axes[0].set_ylabel("hazardous situations per device-year")
    axes[0].set_title("Hazard intensity w_TOP(t) = sum I_B w_i (72 h average)", fontsize=9)
    axes[0].legend(fontsize=7)
    axes[1].set_yscale("log")
    axes[1].set_xlabel("years")
    axes[1].set_ylabel("expected hazardous situations E[N(0, t)] per device")
    axes[1].set_title("Cumulative: lines analytic (Vesely), circles Monte Carlo (1,000,000 devices)", fontsize=9)
    axes[1].legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def markov_first_passage(path, chain, first_onsets):
    times = np.sort(first_onsets[np.isfinite(first_onsets)])
    grid = np.linspace(0.0, times[-1], 400)
    fig, ax = plt.subplots(figsize=(7, 4.2))
    ax.plot(grid / 1e3, chain.first_passage_cdf(grid), lw=2, label="CTMC: 1 - e0 exp(Q_TT t) 1")
    ax.step(times / 1e3, np.arange(1, times.size + 1) / times.size, where="post", lw=0.9,
            label=f"Monte Carlo, {times.size} devices")
    ax.axvline(chain.mttf_from_all_up() / 1e3, color="0.4", ls=":", lw=1, label="CTMC MTTF")
    ax.set_xlabel("time to first hazard (1000 h)")
    ax.set_ylabel("cumulative probability")
    ax.set_title("Markov twin of H2: time to first hazard from all components up", fontsize=9)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def risk_matrix(path, estimates):
    levels = [name for name, _ in risk.PROBABILITY_LEVELS][::-1]  # Improbable .. Frequent
    severities = sorted(risk.SEVERITY_LEVELS)
    fig, ax = plt.subplots(figsize=(7.5, 5))
    for i, level in enumerate(levels):
        for j, severity in enumerate(severities):
            ok = risk.acceptable(severity, level)
            ax.add_patch(plt.Rectangle((j, i), 1, 1, fc="#cfe8c4" if ok else "#f2c1bd", ec="white", lw=2))
    for key, marker in (("H1_OVER_DELIVERY", "s"), ("H2_UNDER_DELIVERY", "o")):
        points = []
        for label, offset in (("initial", -0.18), ("control", 0.18)):
            est = estimates[label][key]
            row = levels.index(est.level)
            lower = dict(risk.PROBABILITY_LEVELS)[est.level]
            upper = {name: low for name, low in risk.PROBABILITY_LEVELS}
            names = [n for n, _ in risk.PROBABILITY_LEVELS]
            top = upper[names[names.index(est.level) - 1]] if names.index(est.level) > 0 else lower * 10
            frac = 0.5 if lower == 0 else np.clip(np.log10(est.harm_per_device_year / lower) / np.log10(top / lower), 0.1, 0.9)
            x = severities.index(est.hazard.severity) + 0.5 + offset
            y = row + frac
            points.append((x, y))
            if label == "control" and est.harm_per_device_year == estimates["initial"][key].harm_per_device_year:
                ax.plot(points[0][0], points[0][1], marker, ms=10, mfc="k", mec="k")
                ax.text(points[0][0] + 0.08, points[0][1] + 0.08, f"{est.hazard.key} (both designs)", fontsize=7)
                points[-1] = points[0]
                continue
            unchanged = estimates["initial"][key].harm_per_device_year == estimates["control"][key].harm_per_device_year
            if unchanged and label == "initial":
                continue
            ax.plot(x, y, marker, ms=10, mfc="none" if label == "initial" else "k", mec="k")
            ax.text(x + 0.08, y + 0.08, f"{est.hazard.key} {label}", fontsize=7)
        if estimates["initial"][key].harm_per_device_year != estimates["control"][key].harm_per_device_year:
            ax.annotate("", xy=points[1], xytext=points[0], arrowprops=dict(arrowstyle="->", lw=1))
    ax.set_xticks(np.arange(len(severities)) + 0.5, [risk.SEVERITY_LEVELS[s] for s in severities], fontsize=8)
    bounds = {name: low for name, low in risk.PROBABILITY_LEVELS}
    ax.set_yticks(np.arange(len(levels)) + 0.5, [f"{n}\n(>= {bounds[n]:g}/y)" if bounds[n] else f"{n}\n(< 1e-05/y)" for n in levels], fontsize=7)
    ax.set_xlim(0, len(severities))
    ax.set_ylim(0, len(levels))
    ax.set_xlabel("severity of harm")
    ax.set_ylabel("probability of harm per device-year")
    ax.set_title("Risk evaluation, illustrative acceptability plan (green = acceptable)", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def importance(path, initial, controlled):
    fig, ax = plt.subplots(figsize=(8, 4.2))
    top = "H2_UNDER_DELIVERY"
    events = list(initial["bdds"][top].order)
    width = 0.38
    for k, (data, label) in enumerate(((initial, "initial"), (controlled, "with risk control"))):
        table = data["bdds"][top].importance(data["q_bar"])
        values = [table[e]["fussell_vesely"] for e in events]
        ax.barh(np.arange(len(events)) + (k - 0.5) * width, values, height=width, label=label)
    ax.set_yticks(np.arange(len(events)), events, fontsize=8)
    ax.set_xlabel("Fussell-Vesely importance for H2 (share of P(H2) involving the event)")
    ax.set_title("Which basic events drive under-delivery (life-average probabilities)", fontsize=9)
    ax.invert_yaxis()
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
