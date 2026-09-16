"""Print what the review bench actually contains right now, as JSON, for the progress board.

The board re-probes every number it shows. This is that probe: it reads the case directories on
111 and reports the cases, the arms that have reports, the measured clock, and which cases have
both arms - the only ones a comparison can be drawn from. Nothing here is estimated.

  ssh 111 python3 /root/orbuz/bench/review/probe.py
"""
import json
import pathlib

ROOT = pathlib.Path('/root/bench/review')


def arm(path):
    data = json.loads(path.read_text())
    cost = data.get('cost') or {}
    merged = data.get('merged') or {}
    agents = data.get('agents') or {}
    broken = [n for n, r in agents.items() if str(r.get('status', '')).endswith('error')]
    return {'calls': cost.get('calls'), 'tokens': cost.get('tokens'), 'wall_s': cost.get('wall_clock_s'),
            'concurrency': cost.get('concurrency'), 'hit': (merged.get('score') or [None])[0],
            'findings': merged.get('findings'), 'agents': len(agents), 'invalid': bool(broken),
            'hitting_agents': len(data.get('hits') or [])}


def main():
    cases, defect_cases, controls, subtle = [], 0, 0, 0
    both_arms = []
    for meta_path in sorted(ROOT.glob('*/*/meta.json')):
        case = meta_path.parent
        if '_stale' in case.parts:
            continue
        meta = json.loads(meta_path.read_text())
        control = bool(meta.get('control'))
        controls += control
        defect_cases += (not control)
        subtle += bool(meta.get('subtle'))
        record = {'case': f"{meta['instance']}/{case.name}", 'control': control,
                  'subtle': meta.get('subtle'), 'kind': (meta.get('defect') or {}).get('kind'),
                  'line': (meta.get('defect') or {}).get('line'),
                  'truth': bool(meta.get('measured')), 'arms': {}}
        for report in sorted(case.glob('review-*.json')):
            name = report.name.replace('review-', '').replace('.json', '')
            record['arms'][name] = arm(report)
        for refutation in sorted(case.glob('refute-*.json')):
            data = json.loads(refutation.read_text())
            record['arms']['refute'] = {'findings_in': data['findings_in'],
                                        'refuted': data['refuted'], 'survived': data['survived'],
                                        'unverified': data['unverified'],
                                        'hit': data['survivors_score'][0]}
        fan = [a for k, a in record['arms'].items() if k.startswith('fanout')]
        base = [a for k, a in record['arms'].items() if k.startswith('baseline')]
        if fan and base:
            both_arms.append(record['case'])
        cases.append(record)

    print(json.dumps({
        'root': str(ROOT),
        'cases': len(cases), 'defect_cases': defect_cases, 'controls': controls, 'subtle': subtle,
        'reports': sum(len(c['arms']) for c in cases),
        'refutations_run': sum(1 for c in cases if 'refute' in c['arms']),
        'cases_with_both_arms': both_arms,
        'clocked': [{'case': c['case'], 'arm': k, 'wall_s': a['wall_s'], 'concurrency': a['concurrency']}
                    for c in cases for k, a in c['arms'].items() if a.get('wall_s')],
        'controls_detail': [{'case': c['case'], 'arms': {k: {'findings': a['findings'], 'hit': a['hit'],
                                                             'calls': a['calls'], 'tokens': a['tokens']}
                                                         for k, a in c['arms'].items()}}
                            for c in cases if c['control']],
        'defect_pairs': [{'case': c['case'], 'subtle': c['subtle'],
                          'fanout': next((a for k, a in c['arms'].items() if k.startswith('fanout')), None),
                          'baseline': next((a for k, a in c['arms'].items() if k.startswith('baseline')), None)}
                         for c in cases if not c['control'] and c['case'] in both_arms],
        'defect_incomplete': [c['case'] for c in cases if not c['control'] and c['case'] not in both_arms],
        'invalid_arms': [f"{c['case']}:{k}" for c in cases for k, a in c['arms'].items() if a.get('invalid')],
    }, indent=1))


main()
