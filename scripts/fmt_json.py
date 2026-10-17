"""Format .json (2 space indent) and .jsonl (one compact record per line, so it stays valid JSONL) in place.

Usage: uv run python scripts/fmt_json.py [--check] [paths...]
No paths: every .json/.jsonl under the repo, skipping generated and vendored dirs (logs/, .venv/, node_modules/, ...).
--check: change nothing, list files that would change, exit 1 if any.
"""

import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", ".idea", ".claude", ".cache", "logs", "node_modules", "cdk.out", ".cli"}


def fmt_json(text):
    return json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"


def fmt_jsonl(text):
    return "".join(
        json.dumps(json.loads(line), ensure_ascii=False) + "\n" for line in text.splitlines() if line.strip()
    )


def files(paths):
    if paths:
        return [Path(p) for p in paths]
    return sorted(
        p
        for p in ROOT.rglob("*")
        if p.suffix in (".json", ".jsonl") and p.is_file() and not SKIP & set(p.relative_to(ROOT).parts)
    )


def main(argv):
    check = "--check" in argv
    changed = []
    for p in files([a for a in argv if a != "--check"]):
        old = p.read_text()
        new = (fmt_jsonl if p.suffix == ".jsonl" else fmt_json)(old)
        if new != old:
            changed.append(p)
            if not check:
                p.write_text(new)
    for p in changed:
        print(("would reformat " if check else "reformatted ") + str(p.relative_to(ROOT) if p.is_absolute() else p))
    return 1 if check and changed else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        assert fmt_json('{"b":1,"a":[1,2]}') == '{\n  "b": 1,\n  "a": [\n    1,\n    2\n  ]\n}\n', "keeps key order"
        assert fmt_json('{"name":"Ελένη"}') == '{\n  "name": "Ελένη"\n}\n', "no ascii escaping"
        assert fmt_jsonl('{"a":1}\n\n{"b":  [1,2]}') == '{"a": 1}\n{"b": [1, 2]}\n', "one record per line"
        print("fmt_json: ok")
        sys.exit(0)
    sys.exit(main(sys.argv[1:]))
