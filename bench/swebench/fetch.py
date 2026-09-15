"""SWE-bench Verified access: cache the split once, then look instances up offline.

Usage:
    python3 fetch.py sympy__sympy-17630 [--cache /root/bench/data/verified.json] [--json out.json]
"""
import argparse
import json
import urllib.request
from pathlib import Path

API = ('https://datasets-server.huggingface.co/rows?dataset=SWE-bench%2FSWE-bench_Verified'
       '&config=default&split=test&offset={}&length=100')


def as_list(value):
    """FAIL_TO_PASS / PASS_TO_PASS come back as JSON strings or lists depending on path."""
    return json.loads(value) if isinstance(value, str) else list(value)


def split(cache):
    cache = Path(cache)
    if cache.exists():
        return json.loads(cache.read_text())
    rows, offset = [], 0
    while True:
        with urllib.request.urlopen(API.format(offset), timeout=60) as response:
            page = json.loads(response.read())
        got = page.get('rows') or []
        rows += [entry['row'] for entry in got]
        if len(got) < 100:
            break
        offset += 100
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows))
    return rows


def load(instance_id, cache):
    for row in split(cache):
        if row['instance_id'] == instance_id:
            return row
    raise SystemExit(f'Unknown instance: {instance_id}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('instance_id')
    parser.add_argument('--cache', default='/root/bench/data/verified.json')
    parser.add_argument('--json', help='write the whole instance row here')
    args = parser.parse_args()
    row = load(args.instance_id, args.cache)
    if args.json:
        Path(args.json).write_text(json.dumps(row, indent=2))
    print(json.dumps({k: row[k] for k in ('instance_id', 'repo', 'base_commit', 'difficulty',
                                          'version')}, indent=2))
    print('FAIL_TO_PASS (hidden):  ', as_list(row['FAIL_TO_PASS']))
    print('PASS_TO_PASS (visible): ', len(as_list(row['PASS_TO_PASS'])), 'tests')


if __name__ == '__main__':
    main()
