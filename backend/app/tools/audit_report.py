"""Summarize a broker CSV without guessing manual/bot ownership."""
import argparse
import json
from pathlib import Path
import pandas as pd


def analyze(path, magic=990022):
    raw = Path(path).read_bytes()
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    df = pd.read_csv(path, encoding=encoding, sep=None, engine="python")
    df.columns = [str(c).strip().lower().replace("_", " ") for c in df.columns]
    money = [c for c in ("profit", "commission", "swap", "fee") if c in df]
    if "profit" not in money:
        raise ValueError("No Profit column found. Export a detailed English broker CSV")
    for col in money:
        values = df[col].fillna("0").astype(str).str.strip().str.replace("$", "", regex=False)
        if values.str.contains(r",\d{1,2}$", regex=True).any():
            raise ValueError("Decimal-comma monetary format is ambiguous; export decimal-point amounts")
        df[col] = pd.to_numeric(values.str.replace(",", "", regex=False), errors="raise").fillna(0)
    df["net"] = df[money].sum(axis=1)
    magic_col = next((c for c in ("magic", "magic number") if c in df), None)
    if magic_col:
        ids = pd.to_numeric(df[magic_col], errors="coerce")
        df["ownership"] = "other_or_unknown"
        df.loc[ids == 0, "ownership"] = "magic_zero_unattributed"
        df.loc[ids == magic, "ownership"] = "matching_magic"
    else:
        df["ownership"] = "unclassified"
    result = {"rows": len(df), "columns": list(df.columns), "groups": {},
              "warning": "Rows may be deals rather than complete positions. Match entry magic by position ID when exits use a different magic. Missing magic cannot establish bot ownership."}
    for name, group in df.groupby("ownership"):
        profits = group["net"]
        wins, losses = profits[profits > 0].sum(), -profits[profits < 0].sum()
        result["groups"][name] = {
            "rows": len(group), "net": round(float(profits.sum()), 2),
            "gross_profit": round(float(wins), 2), "gross_loss": round(float(losses), 2),
            "profit_factor": round(float(wins / losses), 4) if losses else None,
        }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv")
    parser.add_argument("--magic", type=int, default=990022)
    args = parser.parse_args()
    print(json.dumps(analyze(args.csv, args.magic), indent=2))


if __name__ == "__main__":
    main()
