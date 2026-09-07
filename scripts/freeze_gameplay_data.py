"""Freeze existing public local caches for gameplay evaluation; no network."""
import argparse
import json
from pathlib import Path

from src.evaluation.interactions import fingerprint

KEYS = ["anivia-augments", "anivia-build", "brand-augments", "brand-build",
        "global-champion-tiers", "tryndamere-build", "yunara-build", "khazix-build", "aurora-augments", "mordekaiser-augments", "gragas-augments", "ryze-augments", "zeri-augments"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, default=Path("logs/opgg_cache"))
    p.add_argument("--catalog", type=Path, default=Path("logs/name_catalog/catalog.json"))
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    records = {key: json.loads((args.cache / f"{key}.json").read_text(encoding="utf-8")) for key in KEYS}
    bundle = {"origin": "Frozen public OP.GG caches and public name catalog, not current match data",
              "records": records, "catalog": json.loads(args.catalog.read_text(encoding="utf-8"))}
    from src.lol_mcp.arammeta import DEFAULT_REVISION
    arammeta_cache = Path("logs/arammeta_cache") / DEFAULT_REVISION
    if arammeta_cache.exists():
        bundle["arammeta"] = {"revision": DEFAULT_REVISION, "resources": {
            path.relative_to(arammeta_cache).as_posix(): json.loads(path.read_text(encoding="utf-8"))
            for path in arammeta_cache.rglob("*.json")}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"data_sha256": fingerprint(bundle), "records": list(records)}))


if __name__ == "__main__":
    main()
