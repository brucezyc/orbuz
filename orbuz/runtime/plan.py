"""A plan is a claim that work splits into independent pieces, and the judge that checks it.

Several agents are only worth their cost when the pieces are genuinely independent: disjoint
writable sets, distinct evidence, and an acceptance that does not depend on another piece landing
first. "These are independent" is exactly the kind of claim a model makes cheaply and wrongly, so
the runtime decides it from the contract and the repository instead of believing the plan.

Decidable, therefore checked here:

* **disjoint writable** - two items writing one file cannot be judged apart
* **no duplicate items** - the same objective over the same evidence is one item bought twice,
  which is the shape a review fan-out degenerates into ("same input times N")
* **declared evidence exists** - every slice an item reads comes from the task's own catalog
* **no acceptance of one item names another item's output** - that item cannot be judged until
  the other lands, so it is a sequence step, not a parallel one
* **scope** - an item may only write files the task already declared writable

A plan that fails the judge is still a valid plan: it dispatches sequentially, and the reasons are
recorded on the contract as evidence rather than smuggled away.

    "slices": {"diff": ["sympy/parsing/sympy_parser.py"], "tests": ["sympy/parsing/tests"]},
    "plan": [{"id": "parser", "objective": "...", "writable": ["sympy/parsing/sympy_parser.py"],
              "acceptance": ["/usr/bin/python3", "-m", "pytest", "-q", "sympy/parsing/tests"],
              "slices": ["diff", "tests"]}]
"""
from __future__ import annotations

from .contract import relative, source_path

MAX_ITEMS = 12
MAX_SLICES = 64
ITEM_FIELDS = {'id', 'objective', 'writable', 'acceptance', 'slices'}


def _catalog(repo, spec):
    """The evidence a task offers. Items may only read from here, so a slice is a real path."""
    raw = spec.get('slices')
    if not isinstance(raw, dict) or not raw:
        raise ValueError('slices must be a nonempty mapping of slice name -> paths')
    if len(raw) > MAX_SLICES:
        raise ValueError('Too many slices')
    catalog = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 64:
            raise ValueError('Invalid slice name')
        if not isinstance(value, list) or not value or len(value) > 128:
            raise ValueError('Invalid slice paths: ' + str(name))
        paths = []
        for entry in value:
            item = relative(entry)
            if not (repo / item).exists():
                raise ValueError('Slice path does not exist: ' + item)
            paths.append(item)
        catalog[name] = paths
    return catalog


def _items(repo, spec, catalog):
    raw = spec.get('plan')
    if not isinstance(raw, list) or not raw:
        raise ValueError('plan must be a nonempty list of work items')
    if len(raw) > MAX_ITEMS:
        raise ValueError('Too many plan items')
    parsed, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - ITEM_FIELDS:
            raise ValueError('Invalid plan item fields')
        ident = entry.get('id')
        if not isinstance(ident, str) or not ident.strip() or len(ident) > 64 or ident in seen:
            raise ValueError('Plan item ids must be unique nonempty strings')
        seen.add(ident)
        objective = entry.get('objective')
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 2000:
            raise ValueError('Plan items need a nonempty objective')
        writable = entry.get('writable')
        if not isinstance(writable, list) or not writable:
            raise ValueError('Plan items must declare the files they write')
        writable = [relative(x) for x in writable]
        outside = [name for name in writable if name not in spec['writable']]
        if outside:
            raise ValueError('Plan item writes outside the task scope: ' + ', '.join(outside))
        for name in writable:
            if source_path(repo, name).is_dir():
                raise ValueError('Plan item writable entries must be exact files')
        argv = entry.get('acceptance')
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
            raise ValueError('Plan items need a nonempty acceptance argv')
        names = entry.get('slices')
        if not isinstance(names, list) or not names:
            raise ValueError('Plan items must declare the slices they read')
        for name in names:
            if name not in catalog:
                raise ValueError('Unknown slice: ' + str(name))
        parsed.append({'id': ident, 'objective': objective.strip(),
                       'writable': list(dict.fromkeys(writable)),
                       'acceptance': list(argv), 'slices': list(dict.fromkeys(names))})
    return parsed


def judge(items):
    """The independence decision. Everything here is decidable, so it is not a judgement call."""
    reasons = []
    writers = {}
    for item in items:
        for name in item['writable']:
            writers.setdefault(name, []).append(item['id'])
    for name, who in sorted(writers.items()):
        if len(who) > 1:
            reasons.append(f'writable overlap on {name}: {", ".join(sorted(who))}')
    seen = {}
    for item in items:
        key = (item['objective'], tuple(sorted(item['slices'])))
        if key in seen:
            reasons.append(f'duplicate item {item["id"]}: same objective over the same evidence as '
                           f'{seen[key]}')
        seen[key] = item['id']
    for item in items:
        for other in items:
            if other['id'] == item['id']:
                continue
            for name in other['writable']:
                if any(name in arg for arg in item['acceptance'] if not arg.startswith('-')):
                    reasons.append(f'acceptance of {item["id"]} names {other["id"]}\'s output '
                                   f'{name}: it cannot be judged before {other["id"]} lands')
    return {'mode': 'sequential' if reasons else 'parallel', 'reasons': reasons}


def attach(spec, repo):
    """Validate the plan and record how it may dispatch. No plan field means one agent, as before."""
    if 'plan' not in spec and 'slices' not in spec:
        return spec
    if 'slices' not in spec:
        raise ValueError('plan requires slices: the catalog a plan may draw evidence from')
    if 'plan' not in spec:
        raise ValueError('slices without a plan dispatch nothing')
    spec['slices'] = _catalog(repo, spec)
    if spec['plan'] == 'auto':
        # The items do not exist yet: a planner produces them at run time and the same rules
        # judge them then. Deferred is recorded rather than assumed to be parallel.
        spec['dispatch'] = {'mode': 'deferred',
                            'reasons': ['plan = auto: a planner agent produces the items at run time']}
        return spec
    spec['plan'] = _items(repo, spec, spec['slices'])
    spec['dispatch'] = judge(spec['plan'])
    return spec


def describe(spec):
    """What the plan says and what the judge decided, for a CLI or a report."""
    items = spec.get('plan') or []
    if not items:
        return {'items': 0, 'dispatch': {'mode': 'single', 'reasons': []}}
    return {'items': len(items), 'dispatch': spec.get('dispatch') or judge(items),
            'plan': [{'id': i['id'], 'writable': i['writable'], 'slices': i['slices']} for i in items]}
