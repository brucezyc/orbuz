"""Drive one orbuz task against a real model and report each phase.

Credential never passes through argv or a shell: either the environment already has it, or
--key-file/--key-path read it in-process from a local config.

Usage:
    python3 run_task.py <contract.json> --state-dir /root/orbuz-state \
        --model deepseek-chat --base-url https://api.deepseek.com \
        [--key-env DEEPSEEK_API_KEY | --key-file /root/.orbuz/forge.yaml --key-path cheap.api_key] \
        [--runs 6] [--orbuz /root/orbuz]
"""
import argparse
import json
import os
import sys
from pathlib import Path

TERMINAL = ('accepted', 'rejected', 'hacking_suspected', 'failed', 'stalled', 'exhausted',
            'blocked', 'cancelled')


def read_env_file(path, name):
    """Read KEY=VALUE lines without letting the value through argv or a shell."""
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    raise SystemExit(f'{name} not found in {path}')


def credential(args):
    if args.env_file:
        return read_env_file(args.env_file, args.key_env)
    if args.key_file:
        import yaml
        value = yaml.safe_load(Path(args.key_file).read_text())
        for part in args.key_path.split('.'):
            value = value[part]
        if not isinstance(value, str) or not value:
            raise SystemExit('Credential path did not resolve to a string')
        return value
    value = os.environ.get(args.key_env)
    if not value:
        raise SystemExit(f'Absent credential environment variable: {args.key_env} '
                         '(or pass --key-file/--key-path)')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contract')
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--key-env', default='DEEPSEEK_API_KEY')
    parser.add_argument('--key-file')
    parser.add_argument('--key-path', default='cheap.api_key')
    parser.add_argument('--env-file', help='KEY=VALUE file to read --key-env from')
    parser.add_argument('--runs', type=int, default=6)
    parser.add_argument('--task', help='resume this task instead of creating a new one')
    parser.add_argument('--orbuz', default='/root/orbuz')
    args = parser.parse_args()

    os.environ['ORBUZ_KEY'] = credential(args)
    sys.path.insert(0, args.orbuz)
    from orbuz.runtime.engine import Runtime
    from orbuz.runtime.model import ChatModel

    rt = Runtime(Path(args.state_dir))
    spec = json.loads(Path(args.contract).read_text())
    task_id = args.task or rt.create(spec)
    print(json.dumps({'task': task_id, 'goal_chars': len(spec['goal']),
                      'writable': len(spec['writable']),
                      'visible_nodes': len(spec['acceptance']) - 6,
                      'hidden_nodes': len(spec['heldout']) - 6,
                      'max_calls': spec['max_calls'],
                      'steps_per_run': spec['limits'].get('max_steps')}), flush=True)

    model = ChatModel(args.model, args.base_url, 'ORBUZ_KEY')
    try:
        for index in range(args.runs):
            result = rt.run(task_id, model)
            print(json.dumps({'run': index + 1, 'status': result['status'],
                              'calls': result['calls'], 'steps': result['steps'],
                              'tokens': (result.get('budget') or {}).get('total_tokens'),
                              'reason': (result.get('error') or '')[:160]}, ensure_ascii=False),
                  flush=True)
            if result['status'] in TERMINAL:
                break
    finally:
        model.close()
        os.environ.pop('ORBUZ_KEY', None)

    final = rt.status(task_id)
    verified = rt.verify(task_id)
    suites = (final.get('evidence') or {}).get('suites') or {}
    print(json.dumps({
        'task': task_id,
        'status': final['status'],
        'verified': verified['status'],
        'visible_exit': (suites.get('visible') or {}).get('exit_code'),
        'heldout_exit': (suites.get('heldout') or {}).get('exit_code'),
        'heldout_staging': bool(final.get('evidence') and final['evidence'].get('heldout_staging')),
        'attempts': [{'status': a['status'], 'resumed': bool(a.get('resumed'))}
                     for a in final['attempts']],
        'journal_steps': len(rt.journal.steps(task_id)),
        'prefix_cache': final.get('cache'),
        'budget': {k: (final.get('budget') or {}).get(k) for k in
                   ('calls', 'total_tokens', 'cache_hits', 'cache_misses', 'cache_hit_ratio')},
        'pins': len(final.get('pins') or []),
        'workspace': final.get('workspace'),
    }, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
