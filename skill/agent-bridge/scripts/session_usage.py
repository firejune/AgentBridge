"""Offline per-session usage readers. Each provider owns one read_PROVIDER function."""
from datetime import datetime
import json
import math
from pathlib import Path

import sessions

FIELDS = ('model', 'input_tokens', 'cached_input_tokens', 'cache_write_tokens',
          'output_tokens', 'first_ts', 'last_ts', 'wall_seconds', 'active_seconds', 'usd', 'source')


def empty(provider, sid):
    return dict(provider=provider, id=sid, **dict.fromkeys(FIELDS), scope='session-alone',
                subagents=None, notes=['sub-agent usage excluded; subagents not collected'])


def count(value):
    return value if type(value) is int and value >= 0 else None


def read_codex(sid):
    """Read the final cumulative snapshot, never sum snapshots or reasoning twice."""
    path = Path(sessions.read_codex(sid, mode='record')['session']['path'])
    result = empty('codex', sid)
    result['source'] = str(path)
    timestamps, models, usage = [], set(), None
    meta, snapshots = {}, []
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError('record is not an object')
                stamp = row.get('timestamp')
                if stamp is not None:
                    parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                    if parsed.tzinfo is None:
                        raise ValueError('timestamp lacks timezone')
                    timestamps.append((stamp, parsed))
                payload = row.get('payload') or {}
                if row.get('type') == 'session_meta':
                    if payload.get('id') != sid:
                        raise ValueError('session identity mismatch')
                    meta = payload
                if row.get('type') == 'turn_context' and payload.get('model'):
                    models.add(payload['model'])
                    result['model'] = payload['model']
                if row.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                    info = payload.get('info')
                    if isinstance(info, dict):
                        usage = info.get('total_token_usage')
                        if isinstance(usage, dict):
                            snapshots.append((row.get('ordinal'), usage))
            except (ValueError, TypeError, AttributeError) as error:
                raise ValueError(f'{path}: record {number}: {error}') from error
    source = meta.get('source')
    subagent = source.get('subagent', {}) if isinstance(source, dict) else {}
    spawn = subagent.get('thread_spawn', {}) if isinstance(subagent, dict) else {}
    spawn = spawn if isinstance(spawn, dict) else {}
    parent = meta.get('forked_from_id') or meta.get('parent_thread_id') or spawn.get('parent_thread_id')
    baseline = {}
    if parent:
        boundary = meta.get('subagent_history_start_ordinal')
        if count(boundary) is not None and all(count(ordinal) is not None for ordinal, _ in snapshots):
            inherited = [value for ordinal, value in snapshots if ordinal < boundary]
            baseline = inherited[-1] if inherited else {}
            result['notes'].append('inherited cumulative prefix excluded using recorded subagent ordinal boundary')
            if meta.get('history_base'):
                baseline = None
        else:
            # Timestamp bursts are heuristics, not proof of ownership. Match the
            # exact cumulative prefix against the parent's own recorded history.
            try:
                parent_path = Path(sessions.read_codex(parent, mode='record')['session']['path'])
                parent_snapshots = []
                with parent_path.open(encoding='utf-8') as stream:
                    for line in stream:
                        row = json.loads(line)
                        payload = row.get('payload') or {}
                        if row.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                            info = payload.get('info')
                            total = info.get('total_token_usage') if isinstance(info, dict) else None
                            if isinstance(total, dict):
                                parent_snapshots.append(total)
                matched = 0
                for (_, child), ancestor in zip(snapshots, parent_snapshots):
                    if child != ancestor:
                        break
                    baseline = child
                    matched += 1
                if not matched:
                    baseline = None
                else:
                    result['notes'].append('inherited cumulative prefix subtracted after exact parent snapshot match')
            except (OSError, ValueError, TypeError, AttributeError):
                baseline = None
        if baseline is None:
            result['notes'].append('fork baseline cannot be determined from recorded boundary or parent prefix; tokens unavailable')
    if isinstance(usage, dict) and baseline is not None:
        for field, key in (('input_tokens', 'input_tokens'), ('cached_input_tokens', 'cached_input_tokens'),
                           ('cache_write_tokens', 'cache_write_input_tokens'), ('output_tokens', 'output_tokens')):
            total, inherited = count(usage.get(key)), count(baseline.get(key)) if baseline else 0
            if total is not None and inherited is not None and total >= inherited:
                result[field] = total - inherited
            else:
                result['notes'].append(f'{field} missing or cumulative counter decreased')
    for field in ('model', 'input_tokens', 'cached_input_tokens', 'cache_write_tokens', 'output_tokens'):
        if result[field] is None:
            result['notes'].append(f'{field} unavailable from record')
    result['notes'].append('input_tokens includes cached_input_tokens; cache_write_tokens is recorded separately')
    result['notes'].append('output_tokens includes reasoning as recorded by Codex; reasoning is not added again')
    if len(models) > 1:
        result['notes'].append('multiple models recorded; model is the last recorded model; usd unavailable')
    if timestamps:
        gaps = [(b[1]-a[1]).total_seconds() for a, b in zip(timestamps, timestamps[1:])]
        result['first_ts'], result['last_ts'] = timestamps[0][0], timestamps[-1][0]
        if all(gap >= 0 for gap in gaps):
            result['wall_seconds'] = (timestamps[-1][1]-timestamps[0][1]).total_seconds()
            result['active_seconds'] = sum(gap for gap in gaps if gap <= 600)
        else:
            result['notes'].append('timestamps out of order; durations unavailable')
    else:
        result['notes'].append('timestamps not recorded; durations unavailable')
    return result


CLAUDE_TOKENS = (('input_tokens', ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')),
                 ('cached_input_tokens', ('cache_read_input_tokens',)),
                 ('cache_write_tokens', ('cache_creation_input_tokens',)),
                 ('output_tokens', ('output_tokens',)))
CLAUDE_COUNTERS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')
CLAUDE_DIFFERING = 'lines of one response carry differing usage; per-counter maximum kept'


def claude_responses(path, skip_ids, notes, responses=None):
    """Unique responses of one transcript as {key: (model, counters)}, plus its timestamps.

    One response is written as one line per content block carrying the same usage, so lines are keyed
    on message.id + requestId (message.id alone when requestId is missing). A response whose message.id
    is in skip_ids (already counted in the parent transcript) is dropped. Pass `responses` to merge
    several transcripts into one deduplicated set.
    """
    responses, timestamps = {} if responses is None else responses, []
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError('record is not an object')
                stamp = row.get('timestamp')
                if stamp is not None:
                    parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                    if parsed.tzinfo is None:
                        raise ValueError('timestamp lacks timezone')
                    timestamps.append((stamp, parsed))
                message = row.get('message')
                if row.get('type') != 'assistant' or not isinstance(message, dict):
                    continue
                usage = message.get('usage')
                if not isinstance(usage, dict):
                    continue
                mid = message.get('id')
                if mid is not None and mid in skip_ids:
                    continue
                counters = {key: count(usage.get(key)) for key in CLAUDE_COUNTERS}
                earlier = None
                if mid is None:
                    key = (None, None, str(path), number)
                    if 'usage line without message.id counted per line' not in notes:
                        notes.append('usage line without message.id counted per line')
                elif row.get('requestId'):
                    key = (mid, row['requestId'])
                    earlier = responses.pop((mid, None), None)  # an earlier line of this response lacked requestId
                else:
                    key = next((k for k in responses if k[0] == mid), (mid, None))
                earlier = responses.get(key, earlier)
                if earlier is not None:
                    if earlier[1] != counters and CLAUDE_DIFFERING not in notes:
                        notes.append(CLAUDE_DIFFERING)
                    # Streamed block lines grow output_tokens; a later rewrite can carry zeros. The
                    # response's own figure is the largest any of its lines recorded, per counter.
                    counters = {k: None if earlier[1][k] is None or v is None else max(earlier[1][k], v)
                                for k, v in counters.items()}
                responses[key] = (message.get('model'), counters)
            except (ValueError, TypeError, AttributeError) as error:
                raise ValueError(f'{path}: record {number}: {error}') from error
    return responses, timestamps


def claude_totals(responses):
    """Normalised sums; a field is None when any response lacks one of its counters."""
    totals = {}
    for field, keys in CLAUDE_TOKENS:
        values = [usage[key] for _, usage in responses.values() for key in keys]
        totals[field] = None if any(v is None for v in values) else sum(values)
    return totals


def read_claude(sid):
    """Sum unique responses of the session transcript; sub-agent transcripts are reported apart."""
    import uuid
    try:
        sid = str(uuid.UUID(sid))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f'Claude session id is not a UUID: {sid}') from error
    root = sessions.claude_projects_dir()
    paths = sorted(root.glob(f'*/{sid}.jsonl'))
    if len(paths) != 1:
        found = 'no Claude transcript found' if not paths else f'{len(paths)} transcripts match'
        raise ValueError(f"{root / '*' / (sid + '.jsonl')}: {found}")
    path = paths[0]
    result = empty('claude', sid)
    result['notes'] = ['sub-agent usage excluded from the top-level figures; reported separately under subagents']
    result['source'] = str(path)
    try:
        responses, timestamps = claude_responses(path, (), result['notes'])
        main_ids = {key[0] for key in responses if key[0] is not None}
        subagent_files = sorted((path.parent / sid / 'subagents').glob('agent-*.jsonl'))
        side = {}
        for file in subagent_files:
            claude_responses(file, main_ids, result['notes'], side)
    except OSError as error:
        raise ValueError(f'{path}: transcript unreadable: {error}') from error
    result.update(claude_totals(responses))
    result['subagents'] = dict(claude_totals(side), count=len(subagent_files))
    models, skipped = [], set()
    for model, _ in responses.values():
        if not isinstance(model, str) or not model or model.startswith('<'):
            skipped.add(str(model))
        else:
            models.append(model)
    if skipped:
        result['notes'].append('placeholder model values ignored: ' + ', '.join(sorted(skipped)))
    if models:
        result['model'] = models[-1]
    for field in ('model', 'input_tokens', 'cached_input_tokens', 'cache_write_tokens', 'output_tokens'):
        if result[field] is None:
            result['notes'].append(f'{field} unavailable from record')
    result['notes'].append('responses deduplicated on message.id + requestId (message.id alone when requestId is missing)')
    result['notes'].append('input_tokens = input + cache_creation + cache_read; cached_input_tokens = cache_read; '
                           'cache_write_tokens = cache_creation; output_tokens = output')
    result['notes'].append('cache_write_tokens priced at the input rate; cache-write surcharge not applied')
    if len(set(models)) > 1:
        result['notes'].append('multiple models recorded; model is the last recorded model; usd unavailable')
    if timestamps:
        # Claude writes some records (attachments, queued prompts) after later ones, so file order is
        # not time order; durations are taken over the recorded instants in time order.
        ordered = sorted(timestamps, key=lambda stamp: stamp[1])
        if ordered != timestamps:
            result['notes'].append('timestamps not in file order; first_ts, last_ts and durations use time order')
        gaps = [(b[1]-a[1]).total_seconds() for a, b in zip(ordered, ordered[1:])]
        result['first_ts'], result['last_ts'] = ordered[0][0], ordered[-1][0]
        result['wall_seconds'] = (ordered[-1][1]-ordered[0][1]).total_seconds()
        result['active_seconds'] = sum(gap for gap in gaps if gap <= 600)
    else:
        result['notes'].append('timestamps not recorded; durations unavailable')
    return result


def read_agy(sid):
    """Read per-generation protobuf usage from a temporary copy of conversations/<sid>.db."""
    path = _agy_record_path(sid)
    result = empty('agy', str(path.stem))
    result['source'] = str(path)
    models, by_rid, anonymous_rows, differing_dups, failed = set(), {}, [], False, False
    # Copy .db with any -wal/-shm files into a temp dir: opening a WAL database
    # in place with mode=ro still creates/touches <sid>.db-shm in the live directory.
    with _agy_temp_copy(path) as copied:
        try:
            with _agy_connect(copied) as conn:
                has_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gen_metadata'"
                ).fetchone()
                rows = conn.execute('SELECT idx, data FROM gen_metadata ORDER BY idx').fetchall() if has_table else None
        except _agy_sqlite_error() as error:
            raise ValueError(f'{path}: {error}') from error
    if rows is None:
        result['notes'].append('gen_metadata table missing; tokens unavailable')
        failed = True
    else:
        for idx, data in rows:
            if not data:
                continue
            try:
                if not isinstance(data, (bytes, bytearray, memoryview)):
                    raise ValueError('row data is not bytes')
                top = _agy_fields(bytes(data))
                cm_bytes = _agy_submessage(top, 1)
                if not cm_bytes:
                    continue
                cm = _agy_fields(cm_bytes)
                model_bytes = _agy_scalar_bytes(cm, 19)
                if model_bytes:
                    model_name = model_bytes.decode('utf-8', errors='replace').strip()
                    if model_name:
                        models.add(model_name)
                        result['model'] = model_name
                usage_bytes = _agy_submessage(cm, 4)
                if usage_bytes is None:
                    continue
                usage_fields = _agy_fields(usage_bytes)
            except ValueError as error:
                result['notes'].append(f'gen_metadata row {idx} invalid protobuf ({error}); tokens unavailable')
                failed = True
                break
            nums = _agy_varints(usage_fields)
            if not any(nums.get(k, 0) for k in (2, 3, 4, 5, 9, 10)):
                continue
            if nums.get(9, 0) + nums.get(10, 0) != nums.get(3, 0):
                result['notes'].append(f'gen_metadata row {idx} failed output identity (#9 + #10 != #3); tokens unavailable')
                failed = True
                break
            row_counts = {k: nums.get(k, 0) for k in (2, 3, 4, 5)}
            rid = _agy_scalar_bytes(usage_fields, 11)
            if rid is not None:
                earlier = by_rid.get(rid)
                if earlier is not None:
                    if earlier != row_counts:
                        differing_dups = True
                    row_counts = {k: max(earlier[k], row_counts[k]) for k in (2, 3, 4, 5)}
                by_rid[rid] = row_counts
            else:
                anonymous_rows.append(row_counts)
    all_rows = [*by_rid.values(), *anonymous_rows]
    if differing_dups:
        result['notes'].append('duplicate response ids had differing counters; per-counter maximum kept')
    if not failed and all_rows:
        totals = {k: sum(row[k] for row in all_rows) for k in (2, 3, 4, 5)}
        result['input_tokens'] = totals[2] + totals[5] + totals[4]
        result['cached_input_tokens'] = totals[5]
        result['cache_write_tokens'] = totals[4]
        result['output_tokens'] = totals[3]
    for field in ('model', 'input_tokens', 'cached_input_tokens', 'cache_write_tokens', 'output_tokens'):
        if result[field] is None:
            result['notes'].append(f'{field} unavailable from record')
    result['notes'].append(
        'input_tokens is #2 + #5 + #4 (non-cached input + cached_input_tokens #5 + cache_write_tokens #4); '
        'output_tokens is #3 (#9 thinking + #10 response)'
    )
    result['notes'].append('whether retry usage is recorded in gen_metadata is unknown')
    if len(models) > 1:
        result['notes'].append('multiple models recorded; model is the last recorded model; usd unavailable')
    result['notes'].append('timestamps not recorded in gen_metadata; durations unavailable')
    return result


def _agy_record_path(sid):
    import os
    import uuid
    sid = str(uuid.UUID(sid))
    env = os.environ.get('AGY_HOME')
    roots = (Path(env).expanduser(),) if env else (
        Path.home() / '.gemini' / 'antigravity',
        Path.home() / '.gemini' / 'antigravity-ide',
        Path.home() / '.antigravity_cli',
    )
    candidates = [root / 'conversations' / f'{sid}.db' for root in roots]
    found = [path for path in candidates if path.exists()]
    if len(found) != 1:
        searched = str(candidates[0]) if len(candidates) == 1 else ', '.join(str(p) for p in candidates)
        raise ValueError(f'Expected one Antigravity record at {searched}; found {len(found)}')
    return found[0]


def _agy_temp_copy(path):
    from contextlib import contextmanager
    import shutil
    import tempfile

    @contextmanager
    def _ctx():
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / path.name
            shutil.copy2(path, dst)
            for suffix in ('-wal', '-shm'):
                sidecar = Path(f'{path}{suffix}')
                if sidecar.exists():
                    shutil.copy2(sidecar, Path(f'{dst}{suffix}'))
            yield dst

    return _ctx()


def _agy_connect(path):
    from contextlib import closing
    import sqlite3
    return closing(sqlite3.connect(path))


def _agy_sqlite_error():
    import sqlite3
    return sqlite3.DatabaseError


def _agy_varint(buf, index):
    result = shift = 0
    while index < len(buf) and shift < 64:
        byte = buf[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, index
        shift += 7
    raise ValueError('truncated or overflowed varint')


def _agy_fields(buf):
    import struct
    index, out = 0, []
    while index < len(buf):
        tag, index = _agy_varint(buf, index)
        field_num, wire_type = tag >> 3, tag & 7
        if field_num <= 0:
            raise ValueError('invalid field number')
        if wire_type == 0:
            value, index = _agy_varint(buf, index)
        elif wire_type == 2:
            length, index = _agy_varint(buf, index)
            if index + length > len(buf):
                raise ValueError('truncated length-delimited field')
            value = buf[index:index + length]
            index += length
        elif wire_type == 1:
            if index + 8 > len(buf):
                raise ValueError('truncated 64-bit field')
            value = struct.unpack_from('<Q', buf, index)[0]
            index += 8
        elif wire_type == 5:
            if index + 4 > len(buf):
                raise ValueError('truncated 32-bit field')
            value = struct.unpack_from('<I', buf, index)[0]
            index += 4
        else:
            raise ValueError(f'unsupported wire type {wire_type}')
        out.append((field_num, wire_type, value))
    return out


def _agy_submessage(fields, number):
    chunks = [value for fn, wt, value in fields if fn == number and wt == 2]
    return b''.join(chunks) if chunks else None


def _agy_scalar_bytes(fields, number):
    for fn, wt, value in reversed(fields):
        if fn == number and wt == 2:
            return value
    return None


def _agy_varints(fields):
    return {fn: value for fn, wt, value in fields if wt == 0}


def prices(path):
    """Strict Markdown table: model id | input | cached input | output, per million."""
    rows, header = {}, None
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if '|' not in line:
            continue
        cells = [cell.strip().strip('`').strip() for cell in line.strip().strip('|').split('|')]
        if [cell.lower() for cell in cells] == ['model id', 'input', 'cached input', 'output']:
            header = True
            continue
        if not header or all(set(cell) <= set('-: ') for cell in cells):
            continue
        if len(cells) != 4:
            raise ValueError(f'{path}: price table must have four columns')
        try:
            values = tuple(float(cell.removeprefix('$').replace(',', '')) for cell in cells[1:])
        except ValueError as error:
            raise ValueError(f'{path}: invalid price row') from error
        if not cells[0] or cells[0] in rows or not all(math.isfinite(v) and v >= 0 for v in values):
            raise ValueError(f'{path}: duplicate model or invalid price')
        rows[cells[0]] = values
    if header is None:
        raise ValueError(f'{path}: missing price table header: model id | input | cached input | output')
    return rows


def query(provider, sid, price_file=None):
    result = {'codex': read_codex, 'claude': read_claude, 'agy': read_agy}[provider](sid)
    if price_file:
        table = prices(price_file)
        rate = table.get(result['model'])
        tokens = [result[key] for key in ('input_tokens', 'cached_input_tokens', 'output_tokens')]
        if rate is None:
            result['notes'].append('no price row for recorded model')
        elif any(v is None for v in tokens) or tokens[1] > tokens[0]:
            result['notes'].append('usd unavailable: incomplete or inconsistent token counts')
        elif any('multiple models' in note for note in result['notes']):
            pass
        else:
            inp, cached, out = tokens
            result['usd'] = ((inp-cached)*rate[0]+cached*rate[1]+out*rate[2])/1_000_000
    else:
        result['notes'].append('usd requires --prices FILE')
    return result


def format_line(row):
    return ' '.join(f'{key}={json.dumps(value, ensure_ascii=False)}' for key, value in row.items())
