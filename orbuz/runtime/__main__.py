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
    plan_cmd = sub.add_parser('plan')
    plan_cmd.add_argument('task_id')
    plan_cmd.add_argument('--model', required=True)
    plan_cmd.add_argument('--base-url', required=True)
    plan_cmd.add_argument('--key-env', default='DEEPSEEK_API_KEY')
    dispatch = sub.add_parser('dispatch')
    dispatch.add_argument('task_id')
    dispatch.add_argument('--mode', choices=['parallel', 'sequential'])
    fanout = sub.add_parser('run-children')
    fanout.add_argument('task_id')
    fanout.add_argument('--model', required=True)
    fanout.add_argument('--base-url', required=True)
    fanout.add_argument('--key-env', default='DEEPSEEK_API_KEY')
    fanout.add_argument('--concurrency', type=int, default=6)
    vchildren = sub.add_parser('verify-children')
    vchildren.add_argument('task_id')
    vchildren.add_argument('--model')
    vchildren.add_argument('--base-url', default='https://api.deepseek.com')
    vchildren.add_argument('--key-env', default='DEEPSEEK_API_KEY')
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
        elif args.action == 'plan':
            model = ChatModel(args.model, args.base_url, args.key_env)
            result = rt.plan(args.task_id, model)
        elif args.action == 'dispatch':
            result = rt.dispatch(args.task_id, mode=args.mode)
        elif args.action == 'run-children':
            factory = lambda child: ChatModel(args.model, args.base_url, args.key_env)  # noqa: E731
            result = rt.run_children(args.task_id, factory, concurrency=args.concurrency)
        elif args.action == 'verify-children':
            factory = (lambda child: ChatModel(args.model, args.base_url, args.key_env)
                       if args.model else None)
            result = rt.verify_children(args.task_id, factory)
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
        failed = ((args.action in ('run', 'retry', 'verify') and result['status'] != 'accepted')
                  or (args.action == 'plan' and result.get('status') != 'judged')
                  or (args.action == 'run-children'
                      and (result.get('merge') or {}).get('status') != 'accepted')
                  or (args.action == 'verify-children' and result.get('unverified')))
        return 1 if failed else 0
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}))
        return 2
    finally:
        if model:
            model.close()


if __name__ == '__main__':
    raise SystemExit(main())
