"""Optional one-call validation against the bundled historical post fixtures."""
import argparse
import json
from pathlib import Path
import monitor as m


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path.cwd() / "outputs" / "tibo-monitor-validation")
    args = parser.parse_args()
    fixtures = json.loads((m.ROOT.parent / "references" / "validation-fixtures.json").read_text(encoding="utf-8"))
    posts, context = fixtures["posts"], fixtures["context"]
    results = m.analyze(posts, context, m.DEFAULT_DATA / "validation")
    by_id = {p["id"]: p for p in posts}
    checks = []
    for result in results:
        m.save_report(args.output_dir, by_id[result["post_id"]], result)
        expected = fixtures["expected_classes"][result["post_id"]]
        checks.append({"post_id": result["post_id"], "actual": result["reset_class"],
                       "expected": expected, "passed": result["reset_class"] in expected})
    report = {"verified_at_utc": m.utc_now(), "passed": all(c["passed"] for c in checks),
              "checks": checks, "analyses": results,
              "scope": "Historical classification only; not future discovery or notifications"}
    m.atomic_json(args.output_dir / "validation-result.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
