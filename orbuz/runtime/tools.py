"""Narrow source tools. Command execution only sees read-only source."""
from pathlib import Path
from orbuz.runtime.contract import source_path, relative, git
from orbuz.runtime.sandbox import execute


def schema(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': required, 'additionalProperties': False}}}


TEXT = {'type': 'string'}
TOOLS = [
    schema('list_files', 'List tracked source paths; .git is private.', {}, []),
    schema('read_file', 'Read source with offset/limit in characters; use offsets for large files.',
           {'path': TEXT, 'offset': {'type': 'integer'}, 'limit': {'type': 'integer'}}, ['path']),
    schema('read_log', 'Read a runtime log referenced by this task, including previous attempts. Character offset/limit for full-log retrieval.',
           {'path': TEXT, 'offset': {'type': 'integer'}, 'limit': {'type': 'integer'}}, ['path']),
    schema('write_file', 'Replace one explicitly writable source file; protected paths are denied.',
           {'path': TEXT, 'content': TEXT}, ['path', 'content']),
    schema('command', 'Execute argv in a networkless sandbox. Source is read-only; /tmp is scratch. Use /usr/bin/python3. No host paths or credentials.',
           {'argv': {'type': 'array', 'items': TEXT}}, ['argv']),
    schema('submit', 'Submit candidate to the fixed runtime acceptance check. Not a success claim.', {}, []),
    schema('blocked', 'Stop and explain a missing prerequisite; never fabricate success.', {'reason': TEXT}, ['reason']),
]


def dispatch(name, args, workspace, spec, log, cancel, remaining):
    expected = next((s['function']['parameters'] for s in TOOLS if s['function']['name'] == name), None)
    if expected is None or not isinstance(args, dict) or set(args) - set(expected['properties']) or set(expected['required']) - set(args):
        raise ValueError('Invalid tool or arguments')
    root = Path(workspace)
    if name == 'list_files':
        return {'files': git(root, 'ls-files').splitlines()[:2000]}
    if name in ('read_file', 'read_log'):
        if name == 'read_log':
            p = Path(args['path'])
            task_root = log.parent.parent
            if (not p.is_absolute() or p.is_symlink() or p.suffix != '.log'
                    or not p.resolve().is_relative_to(task_root.resolve())
                    or p.resolve().parent.parent != task_root.resolve()):
                raise ValueError('Log is outside this task')
        else:
            p = source_path(root, args['path'])
        offset, limit = args.get('offset', 0), args.get('limit', 6000)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12000:
            raise ValueError('Invalid read range')
        if p.stat().st_size > (4 * 1024 * 1024 if name == 'read_log' else 2_000_000):
            raise ValueError('File too large for source tool')
        text = p.read_text(errors='replace')
        return {'content': text[offset:offset + limit], 'total_chars': len(text),
                'path': args['path'], 'offset': offset}
    if name == 'write_file':
        path = relative(args['path'])
        if path not in spec['writable']:
            raise ValueError('File is outside writable scope')
        content = args['content']
        if not isinstance(content, str) or len(content.encode()) > 200_000:
            raise ValueError('Invalid or oversized content')
        p = source_path(root, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return {'written': path, 'bytes': p.stat().st_size}
    if name == 'command':
        argv = args['argv']
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or '\x00' in a for a in argv):
            raise ValueError('Expected command argv')
        return execute(root, argv, log, timeout=min(spec['timeout'], remaining), cancel=cancel)
    if name == 'blocked' and not isinstance(args['reason'], str):
        raise ValueError('Blocked reason must be text')
    return args
