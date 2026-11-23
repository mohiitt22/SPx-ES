"""Regression checks for the pricing convention, expiry matching and coverage rules."""

import unittest
from datetime import datetime
from math import erfc, exp, log, sqrt
from statistics import median
from io import BytesIO
from pathlib import Path

from comparison import catalog, compare, interpolate, surface
from inputs import Inputs, es_settlement, read_futures, spx_settlement
from model import american_crr, black76, implied_vol


def reference_price(f, k, t, r, vol, side):
    width = vol * sqrt(t)
    x = log(f / k) / width
    n = lambda z: erfc(-z / sqrt(2)) / 2
    return exp(-r * t) * (f * n(x + width / 2) - k * n(x - width / 2)
                         if side == 1 else k * n(-x + width / 2) - f * n(-x - width / 2))


def reference_iv(price, f, k, t, r, side):
    lo, hi = 0, 2
    for _ in range(70):
        m = (lo + hi) * .5
        if reference_price(f, k, t, r, m, side) > price:
            hi = m
        else:
            lo = m
    return (lo + hi) / 2


class Calculations(unittest.TestCase):
    def test_black_and_iv_recover_independent_reference(self):
        for f, k, t, r, sigma in [(7471.25, 7475, 57.25 / 365, .0429, .177),
                                  (7471.25, 6200, 57.25 / 365, .0429, .32),
                                  (100, 100, 1 / 365, -.005, .20)]:
            for side in (1, -1):
                price = reference_price(f, k, t, r, sigma, side)
                self.assertAlmostEqual(black76(f, k, t, r, sigma, side), price, places=9)
                self.assertAlmostEqual(implied_vol(price, f, k, t, r, side), sigma, places=8)
            call = black76(f, k, t, r, sigma, 1)
            put = black76(f, k, t, r, sigma, -1)
            self.assertAlmostEqual(call - put, exp(-r * t) * (f - k), places=9)
        self.assertIsNone(implied_vol(5, 120, 100, .2, .04, 1))

    def test_american_early_exercise_and_convergence(self):
        f, strike, t, rate, vol, side = 100, 105, .45, .07, .24, -1
        american = american_crr(f, strike, t, rate, vol, side)
        self.assertGreater(american, black76(f, strike, t, rate, vol, side))
        self.assertGreaterEqual(american, 5)
        refined = american_crr(f, strike, t, rate, vol, side, steps=1600)
        self.assertAlmostEqual(american, refined, delta=.005)
        self.assertAlmostEqual(implied_vol(american, f, strike, t, rate, side, american_crr), vol, places=8)
        self.assertAlmostEqual(american_crr(f, strike, t, 0, vol, side), black76(f, strike, t, 0, vol, side))

    def test_surface_and_mapping(self):
        f, t, r, vol = 100, .15, .04, .2
        quotes = {k: {"call_price": reference_price(f, k, t, r, vol, 1),
                      "put_price": reference_price(f, k, t, r, vol, -1),
                      "call_iv": None, "put_iv": None, "underlying": 99}
                  for k in range(95, 107)}
        sf = surface(quotes, t, r)
        self.assertAlmostEqual(sf["forward"], f, places=10)
        self.assertAlmostEqual(sf["parity_mad"], 0, places=10)
        nodes = sf["nodes"]
        xs = [x["k"] for x in nodes]
        self.assertAlmostEqual(interpolate(log(102.5 / f), nodes, xs, .02)[0], vol, places=8)
        self.assertIsNone(interpolate(log(108 / f), nodes, xs, .02)[0])
        self.assertIsNone(interpolate(log(102.5 / f), nodes, xs, .0001)[0])

    def test_settlement_uses_pm_match_and_preserves_am(self):
        raw = "2027-01-14T18:00:00"
        pm, _ = spx_settlement(("OWSNP3EF27", raw))
        am, _ = spx_settlement(("OMAMSNPF27", raw))
        es, style, _ = es_settlement(("EW3F7", "XCME:F:ES:202703", "", "EW3", "202701"))
        self.assertEqual(pm, datetime.fromisoformat("2027-01-15T21:00:00+00:00"))
        self.assertEqual(pm, es)
        self.assertEqual(style, "european")
        self.assertEqual((es - am).total_seconds(), 6.5 * 3600)


class Sample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spx = Path.home() / "Desktop/2026-11-19.parquet"
        es = Path(__file__).resolve().parents[1] / "hour/2026-11-19.csv"
        if not (spx.is_file() and es.is_file()):
            raise unittest.SkipTest("Original sample inputs are unavailable.")
        cls.data = Inputs.load(spx, es)
        cls.sample = compare(cls.data, {})

    def test_sample_reconciles_to_source_and_independent_reprice(self):
        result = self.sample
        info = catalog(self.data, {})
        self.assertEqual(result["spx"]["code"], "OWSNP3EF27")
        self.assertEqual(info["es"][0]["dte"], 57)
        self.assertEqual(result["overlap"], 6)
        status = result["status_counts"]
        self.assertEqual(status, {"Outside SPX strike coverage": 358})
        rows = [q for h in result["hours"] for q in h["rows"]]
        self.assertEqual(len(rows), 600)
        overlaps = [q for q in rows if q["mapped_iv"] is not None]
        self.assertEqual(len(overlaps), 242)
        self.assertEqual(sum(q["within"] for q in overlaps), 217)

        rate = result["rate"]
        for h in result["hours"]:
            spx_f = h["surface"]["forward"]
            es_f = h["es_forward"]
            t = h["es_dte"] / 365
            nodes = {n["strike"]: n for n in h["surface"]["nodes"]}
            for q in h["rows"]:
                side = 1 if q["side"] == "Call" else -1
                observed = (q["bid"] + q["ask"]) / 2
                self.assertAlmostEqual(q["es_iv"], reference_iv(observed, es_f, q["strike"], t, rate, side), places=9)
                if q["mapped_iv"] is None:
                    continue
                a, b = (nodes[q[x]] for x in ("bracket_low", "bracket_high"))
                target = q["strike"] * spx_f / es_f
                self.assertTrue(a["strike"] <= target <= b["strike"])
                weight = log(target / a["strike"]) / log(b["strike"] / a["strike"])
                def node_iv(n):
                    return reference_iv(n["premium"], spx_f, n["strike"], t, rate, 1 if n["side"] == "call" else -1)
                iv = sqrt((1 - weight) * node_iv(a) ** 2 + weight * node_iv(b) ** 2)
                self.assertAlmostEqual(q["mapped_iv"], iv, places=9)
                self.assertAlmostEqual(q["model_price"], reference_price(es_f, q["strike"], t, rate, iv, side), places=7)
        self.assertAlmostEqual(result["summary"]["median_gap"], median(q["es_iv"] - q["mapped_iv"] for q in overlaps))

    def test_futures_upload_replaces_proxy_and_flags_missing_prices(self):
        copied = Inputs(**vars(self.data))
        copied.futures = read_futures(BytesIO(
            b"timestamp,futures_contract,price\n2026-11-19T09:00:00-06:00,XCME:F:ES:202703,7470.75\n"))
        result = compare(copied, {})
        self.assertTrue(result["using_actual"])
        self.assertEqual(result["hours"][0]["es_forward"], 7470.75)
        self.assertEqual(result["hours"][0]["es_proxy"], 7471.25)
        self.assertEqual(result["status_counts"]["Missing actual futures price"], 500)

    def test_timezone_changes_matches(self):
        result = compare(self.data, {"spx_zone": "UTC"})
        self.assertEqual(result["overlap"], 2)
        self.assertEqual(result["status_counts"]["No SPX snapshot at this time"], 400)


if __name__ == "__main__":
    unittest.main()
