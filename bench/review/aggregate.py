"""Read both arms of every review case and print the comparison that matters.

One number per case says nothing about whether a fan-out earns its cost. What this reads out:

* detection - did each arm name the injected defect (a control scores 0 only if it stayed quiet)
* cost - calls and tokens actually spent, which is where a fan-out usually loses
* redundancy - how many agents reported the defect that one or two agents already had
* per-agent attribution - who found it, who reported nothing, so a merged average cannot hide it

Usage: python3 bench/review/aggregate.py [--root /root/bench/review]
"""
import argparse
import json
from pathlib import Path


def arm(case, mode):
    path = case / f'review-{mode}.json'
    if not path.exists():
        return None
    report = json.loads(path.read_text())
    agents = report.get('agents') or {}
    hits = [n for n, r in agents.items() if (r.get('score') or (False,))[0]]
    reported = len(report.get('merged') or {})
    return {'calls': (report.get('cost') or {}).get('calls', 0),
            'tokens': (report.get('cost') or {}).get('tokens', 0),
            'agents': len(agents),
            'detected': bool((report.get('merged') or {}).get('score', [False])[0]),
            'reported': (report.get('merged') or {}).get('findings', reported),
            'hitting_agents': hits,
            'quiet_agents': [n for n, r in agents.items()
                             if r.get('status') not in ('contract_error',) and not
                             (r.get('score') or (False,))[0]],
            'also_quiet': [n for n, r in agents.items() if r.get('contract_error')],
            'statuses': {n: r.get('status') for n, r in agents.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='/root/bench/review')
    parser.add_argument('--out')
    args = parser.parse_args()

    rows = []
    for meta_path in sorted(Path(args.root).glob('*/*/meta.json')):
        case = meta_path.parent
        if '_stale' in case.parts:
            continue
        meta = json.loads(meta_path.read_text())
        row = {'case': f"{meta['instance']}/{case.name}", 'instance': meta['instance'],
               'kind': 'control' if meta.get('control') else
                       ('defect-subtle' if meta.get('subtle') else 'defect-loud'),
               'defect': (meta.get('defect') or {}).get('path')}
        for mode in ('fanout', 'baseline'):
            row[mode] = arm(case, mode)
        rows.append(row)

    header = f"{'case':44} {'kind':13} {'fan calls/tok':>16} {'fan det':>8} {'base calls/tok':>16} {'base det':>9}"
    print(header)
    print('-' * len(header))
    for row in rows:
        fan, base = row['fanout'] or {}, row['baseline'] or {}
        print(f"{row['case'][:44]:44} {row['kind']:13} "
              f"{str(fan.get('calls', '-')) + '/' + str(fan.get('tokens', '-')):>16} "
              f"{str(fan.get('detected', '-')):>8} "
              f"{str(base.get('calls', '-')) + '/' + str(base.get('tokens', '-')):>16} "
              f"{str(base.get('detected', '-')):>9}")

    both = [r for r in rows if r['fanout'] and r['baseline']]
    if both:
        defects = [r for r in both if r['kind'].startswith('defect')]
        controls = [r for r in both if r['kind'] == 'control']
        fan_tokens = sum(r['fanout']['tokens'] for r in both)
        base_tokens = sum(r['baseline']['tokens'] for r in both)
        fan_calls = sum(r['fanout']['calls'] for r in both)
        base_calls = sum(r['baseline']['calls'] for r in both)
        print()
        print(f"cases compared           {len(both)}  ({len(defects)} defects, {len(controls)} controls)")
        print(f"fan-out detected         {sum(r['fanout']['detected'] for r in defects)}/{len(defects)}"
              f"   baseline {sum(r['baseline']['detected'] for r in defects)}/{len(defects)}")
        print(f"fan-out quiet on control {sum(not r['fanout']['detected'] for r in controls)}/{len(controls)}"
              f"   baseline {sum(not r['baseline']['detected'] for r in controls)}/{len(controls)}")
        print(f"calls                   fan-out {fan_calls} vs baseline {base_calls}"
              f"  ({fan_calls / max(base_calls, 1):.1f}x)")
        print(f"tokens                  fan-out {fan_tokens} vs baseline {base_tokens}"
              f"  ({fan_tokens / max(base_tokens, 1):.1f}x)")
        if defects:
            redundant = [(r['case'], len(r['fanout']['hitting_agents'])) for r in defects]
            print('agents that found the same defect: ' +
                  ', '.join(f'{case.split("/")[-1]}={n}' for case, n in redundant))
        for row in defects:
            if row['fanout']:
                print(f"  {row['case']}: quiet={row['fanout']['quiet_agents']}"
                      f" hitting={row['fanout']['hitting_agents']}")
        for row in controls:
            fan = row['fanout'] or {}
            base = row['baseline'] or {}
            print(f"  {row['case']} (control): fan findings={fan.get('reported')} "
                  f"baseline findings={base.get('reported')}")

    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2))
        print(f"\nsaved {args.out}")


if __name__ == '__main__':
    main()
