import decimal

import polars as pl


def test_decimals_and_small_types_become_the_usual_ones(act, tmp_path):
    pl.DataFrame(
        {
            "Price": pl.Series([decimal.Decimal("1.50"), None], dtype=pl.Decimal(10, 2)),
            "Count": pl.Series([decimal.Decimal("3"), decimal.Decimal("4")], dtype=pl.Decimal(10, 0)),
            "Small": pl.Series([1, 2], dtype=pl.Int32),
            "Rate": pl.Series([0.5, 0.25], dtype=pl.Float32),
            "Kind": pl.Series(["a", "b"], dtype=pl.Categorical),
        }
    ).write_parquet(tmp_path / "data.parquet")
    df = act("parquet.import", None, {"file": str(tmp_path / "data.parquet")})
    assert df.schema == {"Price": pl.Float64, "Count": pl.Int64, "Small": pl.Int64, "Rate": pl.Float64, "Kind": pl.String}
    assert df.to_dict(as_series=False) == {
        "Price": [1.5, None],
        "Count": [3, 4],
        "Small": [1, 2],
        "Rate": [0.5, 0.25],
        "Kind": ["a", "b"],
    }
