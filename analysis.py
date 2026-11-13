"""
analysis.py - Flag buildings whose heat / hot-water complaints in a target
heating season are anomalously high relative to their own history.

Model
-----
For building i, let x_it be its complaint-days in baseline season t (t = 1..k).
Assume x_it ~ Poisson(lambda_i), with building rates drawn from a citywide
Gamma(alpha, beta) prior. Then:

  * The prior is estimated from the baseline seasons by the method of moments
    (empirical Bayes).
  * Each building's posterior is lambda_i | x ~ Gamma(alpha + sum_t x_it, beta + k).
  * The posterior predictive for the target season is Negative Binomial with
    r = alpha + sum_t x_it and p = (beta + k) / (beta + k + 1).

The model pulls buildings with only a few complaints toward the citywide rate
(shrinkage), so a building going from 0 to 2 complaint-days is not treated as
a "100x increase". Uncertainty is reported as a 90% prediction interval and an
upper-tail p-value P(X >= observed). Because thousands of buildings are tested
at once, p-values are adjusted with Benjamini-Hochberg to control the false
discovery rate.

Usage:
    python analysis.py --db heat.db --target-season 2024 --baseline-seasons 3 --fdr 0.05
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

SQL_DIR = Path(__file__).parent / "sql"
log = logging.getLogger("analysis")


def load_counts(db_path: str | Path) -> pd.DataFrame:
    query = (SQL_DIR / "building_season_counts.sql").read_text()
    with sqlite3.connect(db_path) as con:
        return pd.read_sql_query(query, con)


def to_panel(counts: pd.DataFrame, seasons: list[int]) -> pd.DataFrame:
    """Building x season matrix of complaint-days, with explicit zeros."""
    panel = (counts[counts["season"].isin(seasons)]
             .pivot_table(index="bbl", columns="season", values="complaint_days",
                          aggfunc="sum", fill_value=0)
             .reindex(columns=seasons, fill_value=0))
    return panel


def fit_gamma_prior(baseline: pd.DataFrame) -> tuple[float, float]:
    """
    Method-of-moments Gamma(alpha, beta) prior for per-season rates.

    With m_i = mean of k Poisson(lambda_i) draws:
        E[m]   = alpha / beta
        Var[m] = alpha / beta^2 + E[m] / k
    so the between-building variance of lambda is Var[m] - E[m] / k.
    """
    k = baseline.shape[1]
    m = baseline.mean(axis=1)
    mean, var = m.mean(), m.var(ddof=1)
    var_lambda = var - mean / k
    if var_lambda <= 0:
        # No detectable extra-Poisson variation: use a very tight prior
        # (close to a single citywide rate).
        log.warning("No overdispersion detected; using a near-degenerate prior.")
        var_lambda = mean * 1e-3
    alpha = mean ** 2 / var_lambda
    beta = mean / var_lambda
    return alpha, beta


def benjamini_hochberg(p: np.ndarray) -> np.ndarray:
    """BH-adjusted q-values."""
    p = np.asarray(p, dtype=float)
    n = p.size
    order = np.argsort(p)
    ranked = p[order] * n / np.arange(1, n + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.clip(q, 0, 1)
    return out


def score_buildings(panel: pd.DataFrame, target: int, baseline_seasons: list[int],
                    interval: float = 0.90) -> tuple[pd.DataFrame, tuple[float, float]]:
    baseline = panel[baseline_seasons]
    k = len(baseline_seasons)
    alpha, beta = fit_gamma_prior(baseline)

    r = alpha + baseline.sum(axis=1).to_numpy()
    p = (beta + k) / (beta + k + 1)
    observed = panel[target].to_numpy()

    predictive = stats.nbinom(r, p)
    lo_q, hi_q = (1 - interval) / 2, 1 - (1 - interval) / 2
    result = pd.DataFrame({
        "bbl": panel.index,
        "baseline_total": baseline.sum(axis=1).to_numpy(),
        "observed": observed,
        "expected": predictive.mean(),
        "pi_low": predictive.ppf(lo_q),
        "pi_high": predictive.ppf(hi_q),
        # P(X >= observed) = sf(observed - 1)
        "p_value": predictive.sf(observed - 1),
    })
    result["ratio"] = result["observed"] / result["expected"]
    result["q_value"] = benjamini_hochberg(result["p_value"].to_numpy())
    return result.sort_values("p_value").reset_index(drop=True), (alpha, beta)


def plot(result: pd.DataFrame, fdr: float, target: int, out_path: Path) -> None:
    flagged = result["q_value"] < fdr
    fig, ax = plt.subplots(figsize=(7, 6))
    ax.scatter(result.loc[~flagged, "expected"], result.loc[~flagged, "observed"] + 0.5,
               s=6, alpha=0.25, color="#8a8a8a", label="Not flagged")
    ax.scatter(result.loc[flagged, "expected"], result.loc[flagged, "observed"] + 0.5,
               s=14, alpha=0.9, color="#c0392b", label=f"Flagged (FDR < {fdr:.0%})")
    lim = [result["expected"].min() * 0.8, max(result["expected"].max(), result["observed"].max()) * 1.2]
    ax.plot(lim, lim, color="black", lw=0.8, ls="--", label="Observed = expected")
    ax.set(xscale="log", yscale="log",
           xlabel="Expected complaint-days (posterior predictive mean)",
           ylabel="Observed complaint-days (+0.5 for log scale)",
           title=f"Heat / hot-water complaint-days by building, {target}-{target + 1} season")
    ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="heat.db")
    ap.add_argument("--target-season", type=int, required=True,
                    help="season start year, e.g. 2024 = Oct 2024 - May 2025")
    ap.add_argument("--baseline-seasons", type=int, default=3)
    ap.add_argument("--fdr", type=float, default=0.05)
    ap.add_argument("--out-dir", default="output")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)

    baseline_seasons = list(range(args.target_season - args.baseline_seasons, args.target_season))
    counts = load_counts(args.db)
    missing = set(baseline_seasons + [args.target_season]) - set(counts["season"])
    if missing:
        raise SystemExit(f"No data for season(s) {sorted(missing)} - widen the ETL date range.")

    panel = to_panel(counts, baseline_seasons + [args.target_season])
    result, (alpha, beta) = score_buildings(panel, args.target_season, baseline_seasons)
    boroughs = counts.groupby("bbl")["borough"].agg(lambda s: s.dropna().mode().iat[0] if s.notna().any() else None)
    result = result.merge(boroughs.rename("borough"), left_on="bbl", right_index=True, how="left")

    flagged = result[result["q_value"] < args.fdr]
    log.info("Prior: alpha=%.3f beta=%.3f (citywide mean %.2f complaint-days/season)",
             alpha, beta, alpha / beta)
    log.info("Buildings scored: %s | flagged at FDR %.0f%%: %s",
             f"{len(result):,}", args.fdr * 100, f"{len(flagged):,}")

    result.to_csv(out_dir / f"building_scores_{args.target_season}.csv", index=False)
    flagged.to_csv(out_dir / f"flagged_buildings_{args.target_season}.csv", index=False)
    plot(result, args.fdr, args.target_season, out_dir / f"observed_vs_expected_{args.target_season}.png")
    log.info("Wrote results to %s/", out_dir)


if __name__ == "__main__":
    main()
