"""CLI for the evidence runtime; intentionally independent of legacy CLI."""
import argparse
import json
from pathlib import Path

from orbuz.runtime.engine import Runtime
from orbuz.runtime.model import ChatModel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True,
                        help='Trusted persistent state directory outside the target repo')
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('create')
    create.add_argument('contract', type=Path)
    for action in ('run', 'retry'):
        run = sub.add_parser(action)
        run.add_argument('task_id')
        run.add_argument('--model', required=True)
        run.add_argument('--base-url', required=True)
        run.add_argument('--key-env', default='DEEPSEEK_API_KEY')
    for action in ('status', 'verify', 'cancel', 'steps'):
        sub.add_parser(action).add_argument('task_id')
    pin = sub.add_parser('pin')
    pin.add_argument('task_id')
    pin.add_argument('text')
    args = parser.parse_args()
    model = None
    try:
        rt = Runtime(args.state_dir)
        if args.action == 'create':
            result = {'id': rt.create(json.loads(args.contract.read_text())), 'status': 'pending'}
        elif args.action in ('run', 'retry'):
            model = ChatModel(args.model, args.base_url, args.key_env)
            result = rt.run(args.task_id, model, retry=args.action == 'retry')
        elif args.action == 'cancel':
            rt.cancel(args.task_id)
            result = rt.status(args.task_id)
        elif args.action == 'verify':
            result = rt.verify(args.task_id)
        elif args.action == 'steps':
            result = {'id': args.task_id, 'steps': rt.journal.steps(args.task_id),
                      'resume_plan': rt.journal.resume_plan(args.task_id)}
        elif args.action == 'pin':
            result = {'id': args.task_id, 'pin': rt.pin(args.task_id, args.text)}
        else:
            result = rt.status(args.task_id)
            result['journal'] = rt.journal.steps(args.task_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.action in ('run', 'retry', 'verify') and result['status'] != 'accepted':
            return 1
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}))
        return 2
    finally:
        if model:
            model.close()


if __name__ == '__main__':
    raise SystemExit(main())
