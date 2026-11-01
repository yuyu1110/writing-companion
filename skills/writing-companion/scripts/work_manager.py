"""Opt-in manuscript versions. Files are immutable; author decisions remain explicit."""
import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(path.read_text('utf-8-sig'))


def within(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Path outside the specified workspace')
    return path


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def input_bytes(path):
    data = path.read_bytes()
    data.decode('utf-8-sig')
    if not data.strip():
        raise ValueError('An empty manuscript cannot be saved as a version')
    return data


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def verify_files(root, state):
    for version in state['versions']:
        path = within(root, version['path'])
        if not path.is_file() or digest(path.read_bytes()) != version['sha256']:
            raise ValueError('Version missing or changed: ' + version['id'])


def current_adoption(state):
    """Current adoption derives from the active version; past adoption is history."""
    if state['active_version'] not in {v['id'] for v in state['versions']}:
        raise ValueError('Active version is missing from the version record')
    for version in state['versions']:
        active = version['id'] == state['active_version']
        version['currently_adopted'] = active
        version['adoption_status'] = 'adopted' if active else ('inactive' if version['adopted'] else 'pending')


def record_text(state):
    rows = ['# ' + state['title'], '', 'ID：' + state['id'], '文体：' + state['genre'],
            '活动版本：' + state['active_version'], '作品状态：' + state['status'],
            '', '机器记录：record.json；正文版本保持独立且不覆盖。', '', '## 版本', '']
    for v in state['versions']:
        rows.append(f"- {v['id']}：{v['path']}；来源 {v['origin']}；曾采纳 {v['adopted']}；SHA-256 {v['sha256']}")
    rows += ['', '## 决定历史', '']
    for event in state['decisions']:
        rows.append(f"- {event['at']}：{event['action']} {event['version']}；{event.get('reason', '')}")
    return ('\n'.join(rows) + '\n').encode('utf-8')


def commit(root, state, old_state_hash, catalog, catalog_hash):
    """Record is authoritative. A pending marker makes interrupted metadata visible."""
    current_adoption(state)
    folder = within(root, 'works/' + state['id'])
    record = folder / 'record.json'
    current_hash = digest(record.read_bytes()) if record.exists() else None
    if current_hash != old_state_hash:
        raise ValueError('Work state changed; read it again before applying a decision')
    catalog_path = within(root, 'catalog.json')
    if digest(catalog_path.read_bytes()) != catalog_hash:
        raise ValueError('Catalog changed; read it again before saving')
    pending = folder / 'pending-save.json'
    atomic(pending, encode({'state': state, 'catalog': catalog, 'prepared_at': now()}))
    atomic(record, encode(state))
    atomic(folder / 'record.md', record_text(state))
    atomic(catalog_path, encode(catalog))
    if load(record) != state or load(catalog_path) != catalog:
        raise ValueError('Saved records could not be verified; inspect pending-save.json')
    pending.unlink()


def execute(args):
    root = args.workspace.resolve()
    if not (root / 'workspace.json').is_file():
        raise ValueError('Specify an existing writing workspace')
    manifest = load(root / 'workspace.json')
    if manifest.get('catalog', 'catalog.json') != 'catalog.json':
        raise ValueError('This optional helper supports catalog.json; use normal file tools for another layout')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', args.id):
        raise ValueError('Use a stable lowercase ID of at most 64 characters')
    folder = within(root, 'works/' + args.id)
    state_path = folder / 'record.json'
    if (folder / 'pending-save.json').exists():
        raise ValueError('Incomplete save: inspect pending-save.json and existing files before continuing')
    if args.action in ('show', 'handoff'):
        state = load(state_path)
        verify_files(root, state)
        current_adoption(state)
        state['state_sha256'] = digest(state_path.read_bytes())
        state['text_verified'] = True
        if args.action == 'handoff':
            state['handoff_note'] = 'Restore the active version from its file; candidates and past adoption are not current adoption. No cross-chat memory is implied.'
        return state

    lock = within(root, '.work-manager.lock')
    try:
        stream = lock.open('x', encoding='utf-8')
    except FileExistsError:
        raise ValueError('Another save or interrupted save exists; inspect .work-manager.lock')
    try:
        stream.write(str(os.getpid()))
        stream.close()
        catalog_path = within(root, 'catalog.json')
        catalog_bytes = catalog_path.read_bytes()
        catalog = json.loads(catalog_bytes.decode('utf-8-sig'))
        if not isinstance(catalog.get('works'), list):
            raise ValueError('Catalog must have a works array')
        old_hash = None
        if args.action == 'archive':
            if folder.exists() or any(w['id'] == args.id for w in catalog['works']):
                raise ValueError('Work already exists; do not overwrite or merge it')
            data = input_bytes(args.text_file)
            folder.mkdir(parents=True)
            original = folder / 'original.md'
            with original.open('xb') as f:
                f.write(data)
            state = {'schema_version': 1, 'id': args.id, 'title': args.title,
                     'genre': args.genre, 'active_version': 'original', 'status': '进行中',
                     'versions': [{'id': 'original', 'path': str(original.relative_to(root)).replace('\\', '/'),
                                   'origin': 'author', 'sha256': digest(data), 'adopted': True}],
                     'decisions': [{'at': now(), 'action': 'archive', 'version': 'original', 'reason': args.reason}]}
            catalog['works'].append({'id': args.id, 'title': args.title,
                                     'record_path': f'works/{args.id}/record.md', 'status': '进行中'})
        else:
            state_bytes = state_path.read_bytes()
            old_hash = digest(state_bytes)
            if args.expect_state and args.expect_state != old_hash:
                raise ValueError('State fingerprint differs; do not apply a stale author decision')
            state = json.loads(state_bytes.decode('utf-8-sig'))
            verify_files(root, state)
            if args.action == 'candidate':
                data = input_bytes(args.text_file)
                n = 1
                while any(v['id'] == f'v{n:03}' for v in state['versions']):
                    n += 1
                vid = f'v{n:03}'
                path = within(root, f'works/{args.id}/versions/{vid}.md')
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('xb') as f:
                    f.write(data)
                state['versions'].append({'id': vid, 'path': str(path.relative_to(root)).replace('\\', '/'),
                                          'origin': args.origin, 'sha256': digest(data), 'adopted': False})
                state['decisions'].append({'at': now(), 'action': 'candidate', 'version': vid, 'reason': args.reason})
            else:
                version = next((v for v in state['versions'] if v['id'] == args.version), None)
                if not version:
                    raise ValueError('Unknown version')
                if args.action == 'revert' and not version['adopted']:
                    raise ValueError('Cannot revert to an unadopted candidate; use an explicit adopt decision')
                if args.action == 'adopt':
                    version['adopted'] = True
                previous = state['active_version']
                state['active_version'] = version['id']
                state['status'] = '定稿' if args.action == 'adopt' and args.final else '进行中'
                state['decisions'].append({'at': now(), 'action': args.action, 'version': version['id'],
                                          'previous': previous, 'reason': args.reason})
        for work in catalog['works']:
            if work['id'] == args.id:
                work['status'] = state['status']
        commit(root, state, old_hash, catalog, digest(catalog_bytes))
        return {'saved': True, 'id': args.id, 'active_version': state['active_version'],
                'status': state['status'], 'record_path': f'works/{args.id}/record.md',
                'state_sha256': digest(state_path.read_bytes()),
                'session_updated': False}
    finally:
        stream.close()
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    subs = parser.add_subparsers(dest='action', required=True)
    for action in ('archive', 'candidate', 'adopt', 'revert', 'show', 'handoff'):
        p = subs.add_parser(action)
        p.add_argument('--id', required=True)
        if action in ('archive', 'candidate'):
            p.add_argument('--text-file', type=Path, required=True)
        if action == 'archive':
            p.add_argument('--title', required=True)
            p.add_argument('--genre', default='散文')
        if action == 'candidate':
            p.add_argument('--origin', choices=('author', 'assistant', 'mixed'), required=True)
        if action in ('candidate', 'adopt', 'revert'):
            p.add_argument('--expect-state')
        if action in ('adopt', 'revert'):
            p.add_argument('--version', required=True)
        if action == 'adopt':
            p.add_argument('--final', action='store_true')
        if action not in ('show', 'handoff'):
            p.add_argument('--reason', default='')
    try:
        print(json.dumps(execute(parser.parse_args()), ensure_ascii=False))
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({'error': str(exc), 'saved': False,
                          'note': 'Existing manuscripts are retained; inspect files after a failed save.'}, ensure_ascii=False))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
