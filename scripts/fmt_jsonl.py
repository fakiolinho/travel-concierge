"""Format .jsonl in place: one compact record per line, so it stays valid JSONL. Prettier formats .json but has
no JSONL parser, hence this script.

Usage: uv run python scripts/fmt_jsonl.py [--check] [paths...]
No paths: every .jsonl under the repo, skipping generated and vendored dirs (logs/, .venv/, node_modules/, ...).
--check: change nothing, list files that would change, exit 1 if any.
"""

import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", ".idea", ".claude", ".cache", "logs", "node_modules", "cdk.out", ".cli"}


def fmt_jsonl(text):
    return "".join(
        json.dumps(json.loads(line), ensure_ascii=False) + "\n" for line in text.splitlines() if line.strip()
    )


def files(paths):
    if paths:
        return [Path(p) for p in paths]
    return sorted(p for p in ROOT.rglob("*.jsonl") if p.is_file() and not SKIP & set(p.relative_to(ROOT).parts))


def main(argv):
    check = "--check" in argv
    changed = []
    for p in files([a for a in argv if a != "--check"]):
        old = p.read_text()
        if (new := fmt_jsonl(old)) != old:
            changed.append(p)
            if not check:
                p.write_text(new)
    for p in changed:
        print(("would reformat " if check else "reformatted ") + str(p))
    return 1 if check and changed else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        assert fmt_jsonl('{"a":1}\n\n{"b":  [1,2]}') == '{"a": 1}\n{"b": [1, 2]}\n', "one record per line"
        assert fmt_jsonl('{"name":"Ελένη"}') == '{"name": "Ελένη"}\n', "no ascii escaping"
        print("fmt_jsonl: ok")
        sys.exit(0)
    sys.exit(main(sys.argv[1:]))
