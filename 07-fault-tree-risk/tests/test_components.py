import numpy as np
import pytest
from scipy import integrate

from fta_risk import (
    DriftFirstPassage, Exponential, PeriodicallyTested, RevealedWeibull, TwoStateExponential, Weibull,
)


def test_two_state_closed_form_and_monte_carlo():
    model = TwoStateExponential("x", 1e-3, 50.0)
    t = np.array([0.0, 100.0, 5000.0])
    lam, mu = 1e-3, 1 / 50
    assert np.allclose(model.unavailability(t), lam / (lam + mu) * (1 - np.exp(-(lam + mu) * t)))
    expected = integrate.quad(lambda s: float(model.failure_intensity(s)), 0, 20_000, limit=200)[0]
    assert model.expected_failures(20_000) == pytest.approx(expected, rel=1e-9)
    history = model.sample(np.random.default_rng(1), 20_000, 20_000)
    mean = history.failure_counts(20_000).mean()
    assert abs(mean - expected) < 4 * np.sqrt(expected / 20_000)
    assert abs(history.down_at(5000.0, 20_000).mean() - model.unavailability(5000.0)) < 4 * np.sqrt(0.02 / 20_000)


def test_periodic_test_general_formula_equals_memoryless_closed_form():
    general = PeriodicallyTested("x", Weibull(1.0, 1 / 3e-4), 100.0)
    exact = PeriodicallyTested("x", Exponential(3e-4), 100.0)
    t = np.linspace(0, 5000, 3001)
    assert np.allclose(general.unavailability(t), 1 - np.exp(-3e-4 * (t % 100.0)), atol=1e-12)
    assert np.allclose(general.failure_intensity(t), exact.failure_intensity(t), atol=1e-15)
    assert general.expected_failures(5000.0) == pytest.approx(exact.expected_failures(5000.0), rel=1e-12)


def test_periodic_test_restores_on_test_grid_only():
    model = PeriodicallyTested("x", Exponential(1e-2), 72.0)
    history = model.sample(np.random.default_rng(2), 2000, 2000.0)
    inside = history.end < 2000.0
    assert np.allclose(history.end[inside] / 72.0, np.round(history.end[inside] / 72.0))
    assert np.all(history.end > history.start)


def test_drift_cdf_matches_integrated_density_and_sampler():
    drift = DriftFirstPassage(2e-5, 0.2 / np.sqrt(1e3), 3.0)
    for t in (5_000.0, 20_000.0, 60_000.0):
        integral = integrate.quad(lambda s: float(drift.pdf(s)), 0, t, limit=200)[0]
        assert float(drift.cdf(t)) == pytest.approx(integral, rel=1e-6, abs=1e-12)
    samples = drift.sample(np.random.default_rng(3), 200_000)
    f = float(drift.cdf(60_000.0))
    assert abs(np.mean(samples <= 60_000.0) - f) < 4 * np.sqrt(f * (1 - f) / 200_000)


def test_drift_away_from_limit_is_defective():
    drift = DriftFirstPassage(-2e-5, 0.2 / np.sqrt(1e3), 3.0)
    p = np.exp(2 * -2e-5 * 3.0 / (0.04 / 1e3))
    assert drift.crossing_probability == pytest.approx(p)
    assert float(drift.cdf(1e12)) == pytest.approx(p, abs=1e-6)
    sample = drift.sample(np.random.default_rng(4), 200_000)
    assert abs(np.mean(np.isfinite(sample)) - p) < 4 * np.sqrt(p * (1 - p) / 200_000)


def test_drift_cdf_large_exponent_does_not_overflow():
    drift = DriftFirstPassage(1e-2, 1e-3, 5.0)  # 2va/sigma^2 = 1e5
    values = drift.cdf(np.array([1.0, 400.0, 500.0, 600.0, 1e6]))
    assert np.all(np.isfinite(values)) and np.all(np.diff(values) >= 0) and values[-1] == pytest.approx(1.0)


def test_weibull_renewal_shape_one_matches_two_state():
    a = RevealedWeibull("x", Weibull(1.0, 500.0), 10.0)
    b = TwoStateExponential("x", 1 / 500.0, 10.0)
    t = np.linspace(0, 10_000, 997)
    assert np.max(np.abs(a.unavailability(t) - b.unavailability(t))) < 1e-3 * b.steady_state_unavailability()
    assert a.expected_failures(10_000) == pytest.approx(b.expected_failures(10_000), rel=1e-3)


def test_weibull_renewal_matches_monte_carlo():
    model = RevealedWeibull("x", Weibull(3.0, 2000.0), 100.0)
    history = model.sample(np.random.default_rng(5), 40_000, 10_000.0)
    counts = history.failure_counts(40_000)
    assert abs(counts.mean() - model.expected_failures(10_000.0)) < 4 * counts.std() / np.sqrt(40_000)


@pytest.mark.parametrize("bad", [
    lambda: Exponential(-1.0),
    lambda: Weibull(0.0, 1.0),
    lambda: Weibull(2.0, -1.0),
    lambda: RevealedWeibull("x", Weibull(0.5, 100.0), 1.0),
    lambda: PeriodicallyTested("x", Exponential(1e-3), -5.0),
    lambda: DriftFirstPassage(1e-3, 1e-3, 0.0),
    lambda: DriftFirstPassage(np.inf, 1e-3, 1.0),
    lambda: TwoStateExponential("x", 1e-3, -1.0),
])
def test_invalid_component_parameters_rejected(bad):
    with pytest.raises(ValueError):
        bad()


def test_zero_rate_and_zero_repair_edge_cases():
    never = TwoStateExponential("x", 0.0, 10.0)
    assert never.expected_failures(1000.0) == 0.0
    assert not never.sample(np.random.default_rng(6), 100, 1000.0).traj.size
    instant = TwoStateExponential("x", 1e-2, 0.0)
    assert np.all(instant.unavailability(np.array([10.0, 100.0])) == 0.0)
    assert instant.expected_failures(1000.0) == pytest.approx(10.0)


def test_weibull_renewal_without_repair_uses_implicit_step():
    """Shape 1, no repair: a Poisson process, so the renewal density is exactly lambda (review item 8)."""
    model = RevealedWeibull("x", Weibull(1.0, 100.0), 0.0)
    t = np.linspace(0, 2000, 401)
    assert np.max(np.abs(model.failure_intensity(t) / 0.01 - 1.0)) < 1e-3  # explicit step was ~5e-3 off
    assert model.expected_failures(2000.0) == pytest.approx(20.0, rel=1e-3)


def test_component_models_are_immutable():
    """Cached curves are keyed by size only, so parameters must not change after creation (review item 8)."""
    import dataclasses
    model = PeriodicallyTested("x", Exponential(1e-3), 72.0)
    model.unavailability(np.array([10.0]))
    with pytest.raises(dataclasses.FrozenInstanceError):
        model.test_interval_h = 24.0
