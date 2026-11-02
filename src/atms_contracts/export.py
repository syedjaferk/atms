"""Write contracts/events/*.schema.json from the models.

    python -m atms_contracts.export          write the files
    python -m atms_contracts.export --check  exit 1 if the files are out of date
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .models import EVENT_TYPES

OUT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "events"


def render() -> dict[str, str]:
    files: dict[str, str] = {}
    for event_type, cls in sorted(EVENT_TYPES.items()):
        schema = cls.model_json_schema()
        schema["$id"] = f"https://atms.local/events/{event_type}.schema.json"
        files[f"{event_type}.schema.json"] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    index = {"events": sorted(EVENT_TYPES), "schema_version": 1}
    files["index.json"] = json.dumps(index, indent=2) + "\n"
    return files


def main(argv: list[str]) -> int:
    files = render()
    if "--check" in argv:
        stale = [n for n, text in files.items() if not (OUT_DIR / n).exists() or (OUT_DIR / n).read_text() != text]
        extra = [p.name for p in OUT_DIR.glob("*.json") if p.name not in files]
        if stale or extra:
            print("out of date:", ", ".join(stale + extra))
            return 1
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (OUT_DIR / name).write_text(text)
    print(f"wrote {len(files)} files to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
