"""
Tests run on synthetic data so they need no network access.

    pytest -q
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analysis  # noqa: E402
import etl  # noqa: E402


def raw_frame(rows):
    base = dict(complaint_type="HEAT/HOT WATER", descriptor="ENTIRE BUILDING",
                incident_address="1 MAIN ST", borough="BROOKLYN", incident_zip="11201",
                latitude="40.69", longitude="-73.99", status="Closed", closed_date=None)
    return pd.DataFrame([{**base, **r} for r in rows]).reindex(columns=etl.FIELDS)


def test_validate_drops_and_flags():
    df = raw_frame([
        dict(unique_key="1", created_date="2024-11-01T08:00:00", bbl="3001230045"),
        dict(unique_key="1", created_date="2024-11-01T08:00:00", bbl="3001230045"),   # duplicate
        dict(unique_key="2", created_date="not a date",          bbl="3001230045"),   # bad date
        dict(unique_key="3", created_date="2024-11-02T08:00:00", bbl="0"),            # bad BBL
        dict(unique_key="4", created_date="2024-11-03T08:00:00", bbl="3001230045",
             closed_date="2024-11-01T00:00:00"),                                      # closed < created
        dict(unique_key="5", created_date="2024-11-04T08:00:00", bbl="3001230045",
             latitude="0", longitude="0"),                                            # bad coords
        dict(unique_key="6", created_date="2030-01-01T08:00:00", bbl="3001230045"),   # out of window
    ])
    clean, report = etl.validate(df, "2024-10-01", "2025-06-01")
    r = report.set_index("check_name")["n_rows"]

    assert set(clean["unique_key"]) == {"1", "4", "5"}
    assert r["duplicate_unique_key"] == 1
    assert r["missing_created_date"] == 1
    assert r["invalid_or_missing_bbl"] == 1
    assert r["created_outside_window"] == 1
    assert clean.loc[clean.unique_key == "4", "closed_date"].isna().all()
    assert clean.loc[clean.unique_key == "5", "geo_flag"].item() == 1


def test_bbl_validation():
    s = pd.Series(["1000010001", "6000010001", "100001000", "1000000001", None, "3001230045.0"])
    assert etl.is_valid_bbl(s).tolist() == [True, False, False, False, False, True]


def test_bh_matches_known_values():
    p = np.array([0.01, 0.04, 0.03, 0.20])
    q = analysis.benjamini_hochberg(p)
    np.testing.assert_allclose(q, [0.04, 0.04 * 4 / 3, 0.04 * 4 / 3, 0.20])


def test_model_finds_planted_anomaly_and_controls_false_positives():
    rng = np.random.default_rng(0)
    n, seasons = 3_000, [2020, 2021, 2022, 2023]
    lam = rng.gamma(shape=1.5, scale=2.0, size=n)          # heterogeneous buildings
    data = rng.poisson(lam[:, None], size=(n, len(seasons)))
    panel = pd.DataFrame(data, columns=seasons, index=[f"3{i:09d}" for i in range(n)])
    planted = panel.index[:10]
    panel.loc[planted, 2023] = (lam[:10] * 6 + 15).round().astype(int)   # big spike

    result, _ = analysis.score_buildings(panel, 2023, seasons[:-1])
    flagged = set(result.loc[result.q_value < 0.05, "bbl"])

    assert set(planted) <= flagged                     # all planted spikes found
    assert len(flagged - set(planted)) / max(len(flagged), 1) < 0.10   # few false discoveries


def test_end_to_end_sqlite(tmp_path):
    rows = []
    key = 0
    for season in (2021, 2022, 2023, 2024):
        for bbl, n_days in (("1000010001", 2), ("2000020002", 3)):
            for d in range(n_days):
                key += 1
                rows.append(dict(unique_key=str(key), created_date=f"{season}-12-{d + 1:02d}T09:00:00", bbl=bbl))
                key += 1   # second call same day -> should count as one complaint-day
                rows.append(dict(unique_key=str(key), created_date=f"{season}-12-{d + 1:02d}T15:00:00", bbl=bbl))
    clean, report = etl.validate(raw_frame(rows), "2021-10-01", "2025-06-01")
    db = tmp_path / "t.db"
    etl.load(clean, report, db)

    counts = analysis.load_counts(db)
    assert (counts["raw_complaints"] == 2 * counts["complaint_days"]).all()
    assert set(counts["season"]) == {2021, 2022, 2023, 2024}
