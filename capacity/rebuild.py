"""Re-render saved capacity evidence without fetching providers or publishing.

An explicit output directory prevents accidental replacement of the operational
store. The source run date and per-record observation times are preserved.
"""
import argparse
import json
from pathlib import Path

from capacity.diff import compute_diff
from capacity.render import render_confluence, render_slack
from capacity.schema import AvailabilityRecord


def rebuild(snapshot: Path, manifest_path: Path, output_dir: Path,
            previous_snapshot: Path = None):
    records = [AvailabilityRecord.from_dict(r) for r in json.loads(snapshot.read_text())]
    manifest = json.loads(manifest_path.read_text())
    previous = ([AvailabilityRecord.from_dict(r) for r in json.loads(previous_snapshot.read_text())]
                if previous_snapshot else [])
    changes = compute_diff(records, previous) if previous_snapshot else []
    parent, thread = render_slack(records, changes, manifest, previous)
    page = render_confluence(records, changes, manifest, previous)
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {'slack_message.txt': parent, 'slack_thread.txt': thread,
               'confluence_body.html': page}
    for name, text in outputs.items():
        (output_dir / name).write_text(text, encoding='utf-8')
    return {"run_date": manifest.get("run_date"), "records": len(records),
            "comparison": "saved previous snapshot" if previous_snapshot else "baseline only",
            "outputs": [str((output_dir / name).resolve()) for name in outputs]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--previous-snapshot', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(rebuild(args.snapshot, args.manifest, args.output_dir,
                             args.previous_snapshot), indent=2))


if __name__ == '__main__':
    main()
