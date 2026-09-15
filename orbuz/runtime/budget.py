"""Soft budget caps with an explicit ``capped`` outcome, plus prefix-cache discipline.

Two rules this module exists to enforce:

* **Exhausting the budget is not a failure.** It yields ``capped`` -- a resumable state
  carrying the degrade steps already taken -- never ``failed``.
* **Money is only counted when a price table is supplied.** Token usage is recorded as
  reported; no price is guessed, so ``usd`` stays ``None`` unless the caller provides
  rates. A soft cap expressed in dollars therefore cannot fire without prices.

``PrefixGuard`` watches the prompt prefix that must stay byte-stable for provider cache
hits. It spends no model calls: it hashes the serialized prefix and reports changes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

CAPPED = 'capped'
DEGRADE_ORDER = ('no_fanout', 'small_model', 'checkpoint_exit')


@dataclass
class Limits:
    """Soft limits only. Hard limits (max_calls / max_seconds) live in the contract and
    produce ``exhausted``; these produce ``capped`` because they are resumable."""
    max_steps: int | None = None
    max_tokens: int | None = None
    max_usd: float | None = None
    degrade: tuple = DEGRADE_ORDER

    def __post_init__(self):
        for name in ('max_steps', 'max_tokens'):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f'{name} must be a positive int or None')
        if self.max_usd is not None and (not isinstance(self.max_usd, (int, float)) or self.max_usd <= 0):
            raise ValueError('max_usd must be positive or None')
        if set(self.degrade) - set(DEGRADE_ORDER):
            raise ValueError('Unknown degrade step')


@dataclass
class Decision:
    action: str                    # 'continue' | 'degrade' | 'cap'
    reason: str = ''
    degrade_step: str | None = None
    taken: tuple = field(default_factory=tuple)


class Budget:
    """Cumulative usage with a soft ceiling. Degrade before capping."""

    def __init__(self, limits=None, prices=None):
        self.limits = limits or Limits()
        self.prices = prices
        self.calls = 0
        self.steps = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.usd = 0.0 if prices else None
        self._taken = []

    def observe(self, usage):
        """Record one API-reported usage block. Unknown fields are ignored, not invented."""
        usage = usage or {}
        self.prompt_tokens += int(usage.get('prompt_tokens') or 0)
        self.completion_tokens += int(usage.get('completion_tokens') or 0)
        hit = usage.get('prompt_cache_hit_tokens')
        miss = usage.get('prompt_cache_miss_tokens')
        if hit is None or miss is None:
            details = usage.get('prompt_tokens_details') or {}
            hit = details.get('cached_tokens', hit)
        if hit is not None:
            self.cache_hits += int(hit or 0)
        if miss is not None:
            self.cache_misses += int(miss or 0)
        if self.prices is not None:
            self.usd += (self.prompt_tokens / 1e6) * float(self.prices.get('input', 0)) \
                + (self.completion_tokens / 1e6) * float(self.prices.get('output', 0))

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_ratio(self):
        total = self.cache_hits + self.cache_misses
        return None if total == 0 else self.cache_hits / total

    def snapshot(self):
        return {'calls': self.calls, 'steps': self.steps, 'prompt_tokens': self.prompt_tokens,
                'completion_tokens': self.completion_tokens, 'total_tokens': self.total_tokens,
                'cache_hits': self.cache_hits, 'cache_misses': self.cache_misses,
                'cache_hit_ratio': self.cache_hit_ratio, 'usd': self.usd,
                'degrade_taken': list(self._taken)}

    def check(self):
        """Over a configured soft limit -> 'cap'. Past half of one -> next degrade step."""
        limits = self.limits
        exceeded = []
        if limits.max_steps is not None and self.steps >= limits.max_steps:
            exceeded.append(f'steps {self.steps}/{limits.max_steps}')
        if limits.max_tokens is not None and self.total_tokens >= limits.max_tokens:
            exceeded.append(f'tokens {self.total_tokens}/{limits.max_tokens}')
        if limits.max_usd is not None and self.usd is not None and self.usd >= limits.max_usd:
            exceeded.append(f'usd {self.usd:.4f}/{limits.max_usd}')
        if exceeded:
            return Decision('cap', 'soft budget reached: ' + ', '.join(exceeded))

        ratios = []
        if limits.max_steps is not None:
            ratios.append(self.steps / limits.max_steps)
        if limits.max_tokens is not None:
            ratios.append(self.total_tokens / limits.max_tokens)
        if limits.max_usd is not None and self.usd is not None:
            ratios.append(self.usd / limits.max_usd)
        if ratios and max(ratios) >= 0.5:
            remaining_steps = [s for s in limits.degrade if s not in self._taken]
            if remaining_steps:
                return Decision('degrade', f'usage at {max(ratios):.0%} of the soft budget',
                                degrade_step=remaining_steps[0])
        return Decision('continue')

    def take(self, degrade_step):
        """Mark a degrade step as applied. Each step is applied at most once."""
        if degrade_step not in self.limits.degrade:
            raise ValueError('Unknown degrade step')
        if degrade_step in self._taken:
            raise ValueError('Degrade step already applied')
        self._taken.append(degrade_step)
        return self._taken


class PrefixGuard:
    """Detect prompt-prefix changes that void provider cache hits. No model calls."""

    def __init__(self, watcher=None):
        self.watcher = watcher
        self.fingerprint = None
        self.busts = []
        self.checks = 0

    def observe(self, messages, tools):
        """Returns {'stable': bool, 'bust': int, 'fingerprint': str}."""
        payload = json.dumps({'messages': messages, 'tools': tools}, sort_keys=True,
                             ensure_ascii=False, default=str)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        self.checks += 1
        stable = self.fingerprint is None or fingerprint == self.fingerprint
        if not stable:
            self.busts.append(fingerprint)
            if self.watcher is not None:
                self.watcher(self.busts[-1], self.fingerprint, fingerprint)
        self.fingerprint = fingerprint
        return {'stable': stable, 'bust': len(self.busts), 'fingerprint': fingerprint}

    def snapshot(self):
        return {'checks': self.checks, 'busts': len(self.busts),
                'call_tokens_without_cache_hit': None}
