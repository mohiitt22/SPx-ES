"""Price European and American options on a forward and invert observed premiums."""

from math import exp, log, sqrt

import numpy as np
from scipy.optimize import brentq
from scipy.special import ndtr
from scipy.stats import binom


def black76(forward, strike, t, rate, vol, side):
    discount = exp(-rate * t)
    if t <= 0 or vol <= 0:
        return discount * max(side * (forward - strike), 0.0)
    width = vol * sqrt(t)
    d1 = log(forward / strike) / width + width / 2
    d2 = d1 - width
    return discount * side * (forward * ndtr(side * d1) - strike * ndtr(side * d2))


def american_crr(forward, strike, t, rate, vol, side, steps=400):
    # Under the pricing measure the futures price has zero drift.
    european = black76(forward, strike, t, rate, vol, side)
    if rate <= 0:
        return float(european)
    intrinsic = max(side * (forward - strike), 0.0)
    if t <= 0 or vol <= 0:
        return intrinsic
    dt = t / steps
    shift = vol * sqrt(dt)
    probability = 1 / (1 + exp(shift))
    discount = exp(-rate * dt)
    up = exp(shift)
    counts = np.arange(steps + 1)
    nodes = forward * np.exp((2 * counts - steps) * shift)
    value = np.maximum(side * (nodes - strike), 0)
    european_tree = exp(-rate * t) * np.dot(binom.pmf(counts, steps, probability), value)
    for _ in range(steps):
        nodes = nodes[:-1] * up
        continuation = discount * (probability * value[1:] + (1 - probability) * value[:-1])
        value = np.maximum(continuation, side * (nodes - strike))
    # Apply the lattice's early-exercise premium to the exact European price.
    return float(max(intrinsic, european + max(0.0, value[0] - european_tree)))


def implied_vol(premium, forward, strike, t, rate, side, pricer=black76):
    if premium is None or premium < 0 or t <= 0:
        return None

    def residual(vol):
        return pricer(forward, strike, t, rate, vol, side) - premium

    lower = residual(0)
    if lower > 1e-8:
        return None
    if abs(lower) <= 1e-8:
        return 0.0
    for upper in (1.0, 2.0, 4.0, 8.0):
        if residual(upper) >= 0:
            return brentq(residual, 0, upper, xtol=1e-11)
    return None
