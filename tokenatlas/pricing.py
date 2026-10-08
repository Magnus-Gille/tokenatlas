"""API-equivalent list-price costs for history observations (never what was actually paid).

Costs are computed at report time from a price table; a missing price or tariff dimension yields an unknown
(None) part, never a default or zero.  Reasoning tokens are a subset of output and are not priced separately.
"""
import json
import math
import re
from pathlib import Path

PACKAGED = Path(__file__).with_name('prices.json')
PRICE_KEYS = ('input', 'cache_write_5m', 'cache_write_1h', 'cache_write', 'cache_read', 'output')
_TOP_KEYS = ('schema', 'retrieved_on', 'unit', 'provider_aliases', 'local_providers', 'models')
_MODEL_KEYS = ('provider', 'model', 'currency', 'input', 'cache_read', 'output', 'free', 'source_url', 'retrieved_on')
_CLASSES = ('fresh_input', 'cache_read', 'cache_write', 'output')
_COVERED = ('priced', 'assumed', 'free')
_STANDARD_CLAUDE = 'speed not recorded; priced as standard'
_STANDARD_OTHER = 'service tier not recorded; priced as standard'


def _price(value):
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0)


def _check_prices(name, where, prices, keys):
    if not isinstance(prices, dict):
        raise ValueError(f'{name}: {where} must be an object')
    for key, value in prices.items():
        if key not in keys and key != 'above_input_tokens' and key != 'multiplier':
            raise ValueError(f'{name}: unknown key {where}.{key}')
        if not _price(value):
            raise ValueError(f'{name}: {where}.{key} must be a number >= 0 or null')


def load_prices(path=None):
    """The packaged table by default; ValueError naming the model on any schema violation."""
    table = json.loads(Path(path or PACKAGED).read_text())
    if not isinstance(table, dict) or table.get('schema') != 1:
        raise ValueError('price table: schema must be 1')
    for key in _TOP_KEYS:
        if key not in table:
            raise ValueError(f'price table: missing key {key}')
    if not isinstance(table['models'], list):
        raise ValueError('price table: models must be a list')
    for entry in table['models']:
        name = f"{entry.get('provider')}/{entry.get('model')}" if isinstance(entry, dict) else repr(entry)
        if not isinstance(entry, dict):
            raise ValueError(f'{name}: model entry must be an object')
        for key in _MODEL_KEYS:
            if key not in entry:
                raise ValueError(f'{name}: missing key {key}')
        _check_prices(name, 'prices', {k: entry[k] for k in PRICE_KEYS if k in entry}, PRICE_KEYS)
        if not isinstance(entry['free'], bool):
            raise ValueError(f'{name}: free must be a boolean')
        if entry.get('long_context') is not None:
            _check_prices(name, 'long_context', entry['long_context'], PRICE_KEYS)
            if not isinstance(entry['long_context'].get('above_input_tokens'), int):
                raise ValueError(f'{name}: long_context.above_input_tokens must be an integer')
        for label, modifier in (entry.get('modifiers') or {}).items():
            _check_prices(name, f'modifiers.{label}', modifier, PRICE_KEYS)
    return table


def _find(table, provider, model):
    provider = (table.get('provider_aliases') or {}).get(provider, provider)
    for entry in table.get('models', ()):
        if entry['provider'] == provider and (entry['model'] == model or model in (entry.get('aliases') or ())):
            return provider, entry
    return provider, None


def is_local_provider(provider, table):
    """Whether *provider* is local according to a price table, after aliases."""
    if not isinstance(provider, str):
        return False
    canonical = (table.get('provider_aliases') or {}).get(provider, provider)
    return canonical in (table.get('local_providers') or ())


def _finite_rate(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _reference_long_context(entry):
    """The standard input/output long-context rates needed by a no-cache reference."""
    long = entry.get('long_context')
    if long is None:
        return None
    if not isinstance(long, dict):
        return dict(above_input_tokens=None, input=None, output=None)
    threshold = long.get('above_input_tokens')
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        threshold = None
    return dict(above_input_tokens=threshold,
                input=long.get('input') if _finite_rate(long.get('input')) else None,
                output=long.get('output') if _finite_rate(long.get('output')) else None)


def reference_rate_card(table):
    """Compact public cards for non-free hosted USD models.

    These are for an explicit offline what-if: all input classes are charged at
    the standard input rate and output at the standard output rate. Missing
    long-context rates remain null so a request crossing that threshold stays
    unknown rather than silently falling back.
    """
    valid_date = lambda value: value if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else None
    retrieved = valid_date(table.get('retrieved_on'))
    cards = []
    for entry in table.get('models', ()):
        if not isinstance(entry, dict) or entry.get('free') or entry.get('currency') != 'USD' or is_local_provider(entry.get('provider'), table):
            continue
        if not _finite_rate(entry.get('input')) or not _finite_rate(entry.get('output')):
            continue
        cards.append(dict(provider=entry['provider'], model=entry['model'], input=entry['input'], output=entry['output'],
                          long_context=_reference_long_context(entry), retrieved_on=valid_date(entry.get('retrieved_on')) or retrieved))
    return cards


def _result(status, parts=None, cost=None, currency=None, assumptions=(), reason=None, ref=None):
    return {'cost': cost, 'currency': currency, 'status': status,
            'parts': parts or dict.fromkeys(('input', 'cache_write', 'cache_read', 'output')),
            'assumptions': list(assumptions), 'reason': reason, 'price_ref': ref}


def _times(count, price):
    """Tokens x price per million; zero tokens cost nothing whatever the price, unknown stays unknown."""
    if count is None:
        return None
    if count == 0:
        return 0.0
    return None if price is None else count * price / 1e6


def _rates(obs, table, long_context=True, modifiers=True):
    """(early result, None) when the observation is decided before any token maths, else (None, rates) with the selected
    prices, multiplier, assumptions and reasons; shared by price_observation and unit_prices. long_context=False never switches to the
    long-context prices (long_context='always' forces them, for pricing a group whose members all exceed the threshold) and modifiers=False never switches to a fast/priority/flex tier's (what-if costs for cost facts; the rest is unchanged)."""
    provider, model = obs.get('provider') or 'unknown', obs.get('model')
    provider = (table.get('provider_aliases') or {}).get(provider, provider)
    if provider in (table.get('local_providers') or ()):
        return _result('local', reason='local model'), None
    if not isinstance(model, str) or model in ('', 'unknown'):
        return _result('unpriced', reason=f'no model recorded for {provider}'), None
    provider, entry = _find(table, provider, model)
    if entry is None:
        return _result('unpriced', reason=f'no list price for {provider}/{model}'), None
    ref = {'provider': provider, 'model': entry['model'], 'source_url': entry.get('source_url'),
           'retrieved_on': entry.get('retrieved_on')}
    currency = entry.get('currency')
    if entry.get('free'):
        return _result('free', dict.fromkeys(('input', 'cache_write', 'cache_read', 'output'), 0.0), 0.0, currency, ref=ref), None
    tokens, raw = obs.get('tokens') or {}, obs.get('raw_usage') or {}
    claude = obs.get('harness') == 'claude'
    prices = {k: entry.get(k) for k in PRICE_KEYS}
    assumptions, reasons, multiplier = [], [], 1.0

    def unpriced(reason):
        return _result('unpriced', reason=reason, currency=currency, assumptions=assumptions, ref=ref), None

    # Long context replaces the whole price set once the request's total input exceeds the threshold.
    long = entry.get('long_context')
    context_unknown = long_applies = False
    modifier = None  # the speed/service-tier price set in effect, for cost facts
    if long:
        # fresh + cache read + cache write is the request's whole input in every harness (Codex's inclusive
        # input_tokens equals it); fall back to the raw inclusive count when a class is unknown.
        known = [tokens.get(k) for k in ('fresh_input', 'cache_read', 'cache_write')]
        total = raw.get('input_tokens') if None in known else sum(known)
        if total is None:
            context_unknown = True
            reasons.append('context size unknown')
        elif long_context == 'always' or (long_context and total > long['above_input_tokens']):
            long_applies = True
            prices = {k: long.get(k) for k in PRICE_KEYS}
    modifiers, modifiers_on = entry.get('modifiers') or {}, modifiers
    if claude:
        tariff = obs.get('tariff') or {}
        speed = tariff.get('speed')
        if speed == 'fast':
            if 'speed=fast' not in modifiers:
                return unpriced('fast mode price missing')
            if long_applies:
                return unpriced('fast mode with long context is not priced')
            modifier = 'speed=fast'
            if modifiers_on:
                prices = {k: modifiers['speed=fast'].get(k) for k in PRICE_KEYS}
        elif speed is None:
            assumptions.append(_STANDARD_CLAUDE)
        elif speed != 'standard':
            return unpriced(f'unknown speed {speed}')
        geo = tariff.get('inference_geo')
        if geo == 'us':
            if 'multiplier' not in (modifiers.get('inference_geo=us') or {}):
                return unpriced('inference_geo=us price missing')
            multiplier = modifiers['inference_geo=us']['multiplier']
        elif geo not in (None, 'not_available', 'global'):
            return unpriced(f'unknown inference_geo {geo}')
    tariff = obs.get('tariff') or {}
    if 'service_tier' in tariff:
        tier = tariff['service_tier']
        if tier not in (None, 'standard'):
            if f'service_tier={tier}' not in modifiers:
                return unpriced(f'unknown service_tier {tier}')
            if (claude and tariff.get('speed') == 'fast') or long_applies:
                return unpriced(f'service_tier {tier} with fast mode or long context is not priced')
            modifier = f'service_tier={tier}'
            if modifiers_on:
                prices = {k: modifiers[f'service_tier={tier}'].get(k) for k in PRICE_KEYS}
    elif not claude:
        assumptions.append(_STANDARD_OTHER)
    if context_unknown:
        prices = dict.fromkeys(PRICE_KEYS)

    return None, dict(prices=prices, multiplier=multiplier, assumptions=assumptions, reasons=reasons, claude=claude,
                      currency=currency, ref=ref, tokens=tokens, raw=raw, long=long_applies, has_long=bool(long), modifier=modifier)


def _parts(r):
    """(parts, hour): per-class costs from _rates, and the 1-hour cache-write tokens when the 5m/1h split is known (else 0)."""
    prices, tokens, raw, reasons = r['prices'], r['tokens'], r['raw'], r['reasons']
    write, hour = tokens.get('cache_write'), 0
    if r['claude']:
        split = raw.get('cache_creation') or {}
        five, hr = split.get('ephemeral_5m_input_tokens'), split.get('ephemeral_1h_input_tokens')
        if five is not None and hr is not None and write is not None and five + hr == write:
            pair = (_times(five, prices['cache_write_5m']), _times(hr, prices['cache_write_1h']))
            write_part, hour = (None if None in pair else sum(pair)), hr
        else:
            write_part = 0.0 if write == 0 else None
            if write:
                reasons.append('cache write TTL unknown')
    else:
        write_part = _times(write, prices['cache_write'])
    parts = {'input': _times(tokens.get('fresh_input'), prices['input']), 'cache_write': write_part,
             'cache_read': _times(tokens.get('cache_read'), prices['cache_read']),
             'output': _times(tokens.get('output'), prices['output'])}
    if r['multiplier'] != 1.0:
        parts = {k: None if v is None else v * r['multiplier'] for k, v in parts.items()}
    return parts, hour


def price_observation(obs, table, long_context=True, modifiers=True, tier=None):
    """tier, when a dict, receives {'long': bool, 'has_long': bool, 'modifier': label or None}: the long-context / speed / service-tier price
    set used, and whether the model has a long-context tier at all."""
    early, r = _rates(obs, table, long_context, modifiers)
    if early:
        return early
    if tier is not None:
        tier.update(long=r['long'], has_long=r['has_long'], modifier=r['modifier'])
    parts, _ = _parts(r)
    reasons, assumptions = r['reasons'], r['assumptions']
    missing = [k for k, v in parts.items() if v is None]
    if missing:
        if not reasons:
            reasons.append('unknown or unpriced token classes: ' + ', '.join(missing))
        return _result('partial', parts, None, r['currency'], assumptions, '; '.join(reasons), r['ref'])
    return _result('assumed' if assumptions else 'priced', parts, sum(parts.values()), r['currency'], assumptions, None, r['ref'])


def price_vector(obs, table):
    """(unit prices, cw1h) or (None, 0) when price_observation's cost is None. Unit prices are USD per token,
    [input, cache_write_5m, cache_write_1h, cache_read, output], multiplier applied; cost =
    fresh*in + (write-cw1h)*cw5m + cw1h*cw1h + read*cr + out*out, where cw1h is the 1-hour write tokens when the 5m/1h split is known."""
    early, r = _rates(obs, table)
    if early:
        return ([0.0] * 5, 0) if early['status'] == 'free' and early.get('currency') in (None, 'USD') else (None, 0)
    if r['currency'] != 'USD':return None, 0  # unit prices are USD: never mix currencies
    parts, hour = _parts(r)
    if None in parts.values():
        return None, 0
    p, m = r['prices'], r['multiplier']
    w5, w1 = (p['cache_write_5m'], p['cache_write_1h']) if r['claude'] else (p['cache_write'],) * 2
    # A price may be None only where its token count is zero (then it contributes nothing).
    return [(v or 0.0) * m / 1e6 for v in (p['input'], w5, w1, p['cache_read'], p['output'])], hour


def unit_prices(obs, table):
    return price_vector(obs, table)[0]


def summarize_costs(observations, table, key=lambda o: (o['provider'], o['model'])):
    """Per group: token totals, cost per currency, status counts, coverage and the price refs used."""
    groups = {}
    for obs in observations:
        result = price_observation(obs, table)
        group = groups.setdefault(key(obs), {
            'key': key(obs), 'observations': 0, 'tokens': dict.fromkeys(_CLASSES, 0), 'cost': {}, 'status': {},
            'coverage': None, 'unpriced_tokens': 0, 'price_refs': [], '_known': 0, '_covered': 0})
        group['observations'] += 1
        known = 0
        for name in _CLASSES:
            value = (obs.get('tokens') or {}).get(name)
            if value is not None:
                group['tokens'][name] += value
                known += value
        group['status'][result['status']] = group['status'].get(result['status'], 0) + 1
        group['_known'] += known
        if result['status'] in _COVERED:
            group['_covered'] += known
            group['cost'][result['currency']] = group['cost'].get(result['currency'], 0.0) + result['cost']
        else:
            group['unpriced_tokens'] += known
        if result['price_ref'] and result['price_ref'] not in group['price_refs']:
            group['price_refs'].append(result['price_ref'])
    for group in groups.values():
        known, covered = group.pop('_known'), group.pop('_covered')
        group['coverage'] = covered / known if known else None
    return list(groups.values())
