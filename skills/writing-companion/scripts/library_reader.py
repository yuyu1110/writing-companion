"""Bounded local book retrieval. FTS candidates are leads, not semantic conclusions."""
import argparse
import bisect
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

VERSION = 1
WINDOW = 1800
OVERLAP = 180
CJK = re.compile(r'[\u3400-\u9fff\U00020000-\U0002fa1f]+|[^\W_]+', re.UNICODE)
METADATA_FIELDS = ('authors', 'coverage', 'edition_notes', 'translator', 'translators',
                   'edition', 'languages')


def json_read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def safe_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Path outside the library workspace')
    return path


def root_for(args):
    if args.workspace:
        return args.workspace.resolve()
    if (Path.cwd() / 'workspace.json').is_file():
        return Path.cwd().resolve()
    binding_path = Path(__file__).resolve().parents[1] / 'local-workspace.json'
    if not binding_path.is_file():
        raise ValueError('Specify --workspace, run from a workspace, or configure local-workspace.json')
    binding = json_read(binding_path)
    return Path(binding['default_workspace']).expanduser().resolve()


def retrieval_settings(root):
    path = root / 'techniques/retrieval-config.json'
    return json_read(path) if path.is_file() else {}


def norm(text, mapping):
    return text.translate(mapping).casefold()


def tokens(text, mapping):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\]\([^)]*\)', ']', text)
    out = []
    for part in CJK.findall(norm(text, mapping)):
        if '\u3400' <= part[0] <= '\u9fff' or ord(part[0]) >= 0x20000:
            if len(part) == 1:
                out.append(part)
            else:
                out.extend(part[i:i+2] for i in range(len(part)-1))
        else:
            out.append(part)
    return out


def phrase(term, mapping):
    parts = tokens(term, mapping)
    return '"' + ' '.join(parts).replace('"', '""') + '"' if parts else ''


def bounded(payload, budget):
    """Bound serialized characters, not pretend this is an exact model token count."""
    payload['output_budget_chars'] = budget
    payload['output_truncated'] = payload.get('output_truncated', False)
    encode = lambda: json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    while len(encode()) + 1 > budget:
        payload['output_truncated'] = True
        results = payload.get('results', [])
        if len(results) > 1:
            results.pop()
            continue
        target = results[0] if results else payload
        key = next((k for k in ('text', 'snippet', 'content') if len(target.get(k, '')) > 80), None)
        if key:
            target[key] = target[key][:max(80, len(target[key]) - (len(encode()) - budget) - 20)]
            target['truncated'] = True
            if key == 'text' and 'offset_start' in target:
                target['next_offset'] = target['offset_start'] + len(target[key])
            continue
        if results:
            results.pop()
            continue
        raise ValueError('Budget too small for result metadata; use at least 1600 characters')
    print(encode())


def connect(root, write=False):
    path = root / 'library' / 'retrieval.sqlite3'
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    if not write and not path.exists():
        raise ValueError('Index missing: run library_reader.py build')
    db = sqlite3.connect(path if write else path.as_uri() + '?mode=ro', uri=not write)
    db.row_factory = sqlite3.Row
    if write:
        db.executescript('''
          CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY, title TEXT, path TEXT, record_path TEXT,
            kind TEXT, size INTEGER, mtime INTEGER, sha256 TEXT, meta TEXT);
          CREATE TABLE IF NOT EXISTS passages (
            id INTEGER PRIMARY KEY, source TEXT, start INTEGER, end INTEGER,
            line_start INTEGER, line_end INTEGER, heading TEXT, text TEXT);
          CREATE INDEX IF NOT EXISTS by_source ON passages(source);
          CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(tokens, content='');
        ''')
    return db


def config(db, key):
    row = db.execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def mapping_for(db):
    return {int(k): v for k, v in (config(db, 'mapping') or {}).items()}


def source_manifest(root):
    manifest = json_read(root / 'workspace.json')
    return json_read(safe_path(root, manifest['catalog']))['sources']


def source_metadata(source):
    return {key: source.get(key) for key in METADATA_FIELDS}


def metadata_sha256(source):
    """Fingerprint catalog citation fields, not the contents of record.md or publication truth."""
    payload = {key: source.get(key) for key in ('id', 'title', 'text_path', 'record_path')}
    payload.update(source_metadata(source))
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def title_matches(source, query, mapping):
    values = [source.get('title', ''), source['id']]
    for key in ('authors', 'translator', 'translators'):
        items = source.get(key) or []
        values.extend([items] if isinstance(items, str) else items)
    return any(norm(str(value), mapping).find(norm(query, mapping)) >= 0 for value in values)


def build(root, args):
    started = time.perf_counter()
    sources = source_manifest(root)
    settings = retrieval_settings(root)
    db = connect(root, True)
    previous = config(db, 'version')
    if previous not in (None, VERSION):
        raise ValueError('Index schema changed; migrate the derived index before building')
    mapping = mapping_for(db)
    if previous is None:
        dictionary_path = settings.get('normalization_dictionary')
        if dictionary_path:
            dictionary = safe_path(root, dictionary_path)
            for line in dictionary.read_text('utf-8').splitlines():
                pair = line.split('\t')
                if len(pair) == 2 and len(pair[0]) == 1:
                    mapping[ord(pair[0])] = pair[1].split()[0]
        db.execute('INSERT OR REPLACE INTO config VALUES (?,?)', ('mapping', json.dumps(mapping)))
        db.execute('INSERT OR REPLACE INTO config VALUES (?,?)', ('version', json.dumps(VERSION)))
    guide_ids = set(settings.get('guide_source_ids', []))
    active = {s['id'] for s in sources}
    def remove(sid):
        for p in db.execute('SELECT id,text FROM passages WHERE source=?', (sid,)).fetchall():
            db.execute("INSERT INTO fts(fts,rowid,tokens) VALUES ('delete',?,?)", (p['id'], ' '.join(tokens(p['text'], mapping))))
        db.execute('DELETE FROM passages WHERE source=?', (sid,))
        db.execute('DELETE FROM sources WHERE id=?', (sid,))
    for old in db.execute('SELECT id FROM sources').fetchall():
        if old['id'] not in active:
            remove(old['id'])
    changed = skipped = 0
    for s in sources:
        if args.source and s['id'] not in args.source:
            continue
        path = safe_path(root, s['text_path'])
        stat = path.stat()
        old = db.execute('SELECT * FROM sources WHERE id=?', (s['id'],)).fetchone()
        kind = 'guide' if s['id'] in guide_ids else 'literature'
        meta = json.dumps(source_metadata(s), ensure_ascii=False)
        if old and not args.force and old['size'] == stat.st_size and old['mtime'] == stat.st_mtime_ns and old['path'] == s['text_path']:
            db.execute('UPDATE sources SET title=?,record_path=?,kind=?,meta=? WHERE id=?', (s['title'],s['record_path'],kind,meta,s['id']))
            skipped += 1
            continue
        raw = path.read_bytes()
        text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
        digest = hashlib.sha256(raw).hexdigest()
        if old:
            remove(s['id'])
        db.execute('INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?)', (s['id'],s['title'],s['text_path'],s['record_path'],kind,stat.st_size,stat.st_mtime_ns,digest,meta))
        line_offsets = [0] + [m.end() for m in re.finditer('\n', text)]
        headings = [(m.start(), m.group().strip()[:160]) for m in re.finditer(r'^(?:#{1,6}\s+|\*\*\s+).+', text, re.M)]
        heading_offsets = [h[0] for h in headings]
        start = 0
        while start < len(text):
            end = min(len(text), start + WINDOW)
            if end < len(text):
                boundary = text.rfind('\n\n', start + WINDOW // 2, end)
                if boundary != -1:
                    end = boundary + 2
            part = text[start:end]
            heading_idx = bisect.bisect_right(heading_offsets, start) - 1
            heading = headings[heading_idx][1] if heading_idx >= 0 else ''
            row = db.execute('INSERT INTO passages(source,start,end,line_start,line_end,heading,text) VALUES (?,?,?,?,?,?,?)',
                             (s['id'],start,end,bisect.bisect_right(line_offsets,start),bisect.bisect_right(line_offsets,max(start,end-1)),heading,part))
            db.execute('INSERT INTO fts(rowid,tokens) VALUES (?,?)', (row.lastrowid,' '.join(tokens(part,mapping))))
            if end == len(text):
                break
            start = max(start + 1, end - OVERLAP)
        changed += 1
        db.commit()
        if changed % 40 == 0:
            print(json.dumps({'progress_sources': changed, 'skipped': skipped}), file=sys.stderr, flush=True)
    db.commit()
    result = {'changed':changed,'unchanged':skipped,'sources':db.execute('SELECT count(*) FROM sources').fetchone()[0],
              'passages':db.execute('SELECT count(*) FROM passages').fetchone()[0], 'seconds':round(time.perf_counter()-started,3),
              'normalization':'traditional-character fold, not full phrase translation' if mapping else 'casefold only'}
    db.close()
    bounded(result,args.budget)


def fresh_source(root, source, strict=False):
    try:
        path = safe_path(root, source['path'])
        stat = path.stat()
        if stat.st_size != source['size'] or stat.st_mtime_ns != source['mtime']:
            return False
        return not strict or hashlib.sha256(path.read_bytes()).hexdigest() == source['sha256']
    except OSError:
        return False


def search(root, args):
    db = connect(root)
    mapping = mapping_for(db)
    groups = [g.split('|') for g in args.group]
    if args.query:
        groups.append([args.query])
    settings = retrieval_settings(root)
    for concept in args.concept:
        if concept not in settings.get('concepts', {}):
            raise ValueError('Unknown concept; inspect retrieval-config.json concept keys')
        groups.append(settings['concepts'][concept]['terms'])
    if any(not any(phrase(t, mapping) for t in group) for group in groups):
        raise ValueError('Each group needs at least one searchable term')
    # A one-character CJK token is only indexed when standalone. Reject misleading partial coverage.
    if any(len(norm(t.strip(), mapping)) == 1 and '\u3400' <= norm(t.strip(),mapping) <= '\u9fff' for g in groups for t in g):
        raise ValueError('Use a phrase of at least two Chinese characters, or select a known scene card')
    live = {s['id']: s for s in source_manifest(root)}
    clauses, values = [], []
    if args.source:
        clauses.append('s.id IN (' + ','.join('?' for _ in args.source) + ')'); values.extend(args.source)
    if args.title:
        # Filter current catalog values even when only bibliographic metadata changed.
        matches = [sid for sid, source in live.items() if title_matches(source, args.title, mapping)]
        clauses.append('s.id IN (' + ','.join('?' for _ in matches) + ')'); values.extend(matches)
    if args.kind != 'all':
        guide_ids = set(settings.get('guide_source_ids', []))
        matches = [sid for sid in live if (sid in guide_ids) == (args.kind == 'guide')]
        clauses.append('s.id IN (' + ','.join('?' for _ in matches) + ')'); values.extend(matches)
    where = (' AND ' + ' AND '.join(clauses)) if clauses else ''
    indexed = {s['id'] for s in db.execute('SELECT id FROM sources')}
    missing = sorted(set(live)-indexed)
    eligible = [s['id'] for s in db.execute('SELECT s.id FROM sources s WHERE 1=1'+where,values) if s['id'] in live]
    scope = {'mode':'selected_sources' if args.source or args.title else 'library_wide',
             'kind':args.kind,'eligible_sources':len(eligible),'source_filters':args.source,'title_filter':args.title}
    if not groups:
        rows = db.execute('SELECT s.* FROM sources s WHERE 1=1'+where, values).fetchall()
        rows = sorted((s for s in rows if s['id'] in live), key=lambda s: live[s['id']]['title'])
        guide_ids = set(settings.get('guide_source_ids', []))
        results = [{'source':s['id'],'title':live[s['id']]['title'],
                    'kind':'guide' if s['id'] in guide_ids else 'literature',
                    'path':live[s['id']]['text_path'],
                    'fresh':live[s['id']]['text_path'] == s['path'] and fresh_source(root,s)} for s in rows[:args.limit]]
        bounded({'mode':'titles','scope':scope,'results':results,'unindexed_count':len(missing),'note':'literature means non-guide, not a verified fiction classification'}, args.budget)
        return
    expression = ' AND '.join('('+' OR '.join(phrase(t,mapping) for t in g if phrase(t,mapping))+')' for g in groups)
    # Pool is capped; never imply an exhaustive similarity search.
    sql = '''SELECT p.*,s.title,s.path,s.size,s.mtime,s.sha256,s.kind,s.record_path,bm25(fts) AS rank
             FROM fts JOIN passages p ON p.id=fts.rowid JOIN sources s ON s.id=p.source
             WHERE fts MATCH ?''' + where + ' ORDER BY rank LIMIT ?'
    rows = db.execute(sql, [expression]+values+[args.pool]).fetchall()
    results, counts, seen, stale = [], {}, {}, set()
    terms = [norm(t,mapping) for g in groups for t in g]
    for row in rows:
        sid = row['source']
        if sid not in live or live[sid]['text_path'] != row['path']:
            stale.add(sid); continue
        if not fresh_source(root,row):
            stale.add(sid); continue
        if counts.get(sid,0) >= args.per_source:
            continue
        if any(max(a,row['start']) < min(b,row['end']) for a,b in seen.get(sid,[])):
            continue
        raw = row['text']; folded = norm(raw,mapping)
        positions = [folded.find(t) for t in terms if folded.find(t) >= 0]
        matched = [t for t in terms if t in folded]
        # Keep the original character offset even when Unicode casefold expands a character.
        first_match = min(positions or [0])
        original_match = 0
        if first_match:
            consumed = 0
            for original_match, character in enumerate(raw):
                width = len(norm(character, mapping))
                if consumed + width > first_match:
                    break
                consumed += width
        offset = max(0,original_match-60)
        snippet = raw[offset:offset+args.snippet]
        results.append({'passage':f"{sid}:{row['start']}:{row['sha256'][:12]}",'source':sid,'title':live[sid]['title'],'heading':row['heading'],
                        'lines':[row['line_start'],row['line_end']], 'matched':matched,
                        'match_offset':row['start'] + original_match,
                        'snippet':snippet,'truncated':True,'citation_ready':False})
        counts[sid] = counts.get(sid,0)+1
        seen.setdefault(sid,[]).append((row['start'],row['end']))
        if len(results) >= args.limit:
            break
    bounded({'mode':'ranked_candidates','scope':scope,'results':results,'candidate_pool':len(rows),'pool_limit':args.pool,
             'stale_sources':sorted(stale),'unindexed_count':len(missing),'method':'Chinese bigrams / word FTS + explicit concept groups; not semantic embeddings',
             'next':'read --passage ID to verify context; broaden groups if no useful match'},args.budget)


def context_range(text, start, end, mode, focus):
    """Expand at Markdown paragraph/section boundaries without changing index offsets."""
    if mode == 'chars':
        return start, end
    headings = list(re.finditer(r'^(#{1,6})[ \t]+[^\n]+', text, re.M))
    prior = [h for h in headings if h.start() <= focus]
    heading = prior[-1] if prior else None
    section_start = heading.start() if heading else 0
    section_end = len(text)
    for following in headings:
        if following.start() > focus and (not heading or len(following[1]) <= len(heading[1])):
            section_end = following.start()
            break
    if mode == 'section':
        return section_start, section_end
    start, end = max(start, section_start), min(end, section_end)
    breaks = list(re.finditer(r'\n[ \t]*\n', text, re.M))
    before = [b.end() for b in breaks if section_start <= b.end() <= start]
    after = [b.end() for b in breaks if end <= b.end() <= section_end]
    return (before[-1] if before else section_start), (after[0] if after else section_end)


def bounded_read(payload, text, offsets, start, end, budget, focus=None):
    """Fit complete serialized output, keeping the selected focus when context is too long."""
    desired_start, desired_end = start, end
    length = end - start
    payload.update(requested_offset_start=start, requested_offset_end=end,
                   output_budget_chars=budget, output_truncated=False)
    while True:
        if focus is not None and length < desired_end - desired_start:
            start = max(desired_start, min(focus - length // 2, desired_end - length))
        end = start + length
        line = bisect.bisect_right(offsets, start)
        complete = start == desired_start and end == desired_end
        payload.update(offset_start=start, offset_end=end, line_start=line,
                       column_start=start-offsets[line-1]+1,
                       line_end=bisect.bisect_right(offsets, end-1),
                       line_end_requested=bisect.bisect_right(offsets, desired_end-1),
                       text=text[start:end], next_offset=end if end < len(text) else None,
                       context_complete=complete, context_omitted_before=start > desired_start,
                       context_omitted_after=end < desired_end, output_truncated=not complete)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        excess = len(encoded) + 1 - budget
        if excess <= 0:
            print(encoded)
            return
        if length == 1:
            raise ValueError('Budget too small for source metadata and context; increase --budget')
        reduced = length - max(20, excess)
        length = reduced if reduced > 0 else max(1, length // 2)


def read(root, args):
    db = connect(root)
    passage = None
    expected_hash = None
    if args.passage:
        parts=args.passage.rsplit(':',2)
        if len(parts)!=3 or not parts[1].isdigit():
            raise ValueError('Use the complete passage identifier returned by search')
        passage=db.execute('SELECT * FROM passages WHERE source=? AND start=?',(parts[0],int(parts[1]))).fetchone()
        expected_hash=parts[2]
        if passage is None: raise ValueError('Passage no longer exists; search again')
    sid = passage['source'] if passage else args.source
    source = db.execute('SELECT * FROM sources WHERE id=?',(sid,)).fetchone()
    if not source:
        raise ValueError('Unknown source or passage')
    if expected_hash and not source['sha256'].startswith(expected_hash):
        raise ValueError('Passage identifier is from another file version; search again')
    live = {s['id']:s for s in source_manifest(root)}
    if sid not in live or live[sid]['text_path'] != source['path'] or not fresh_source(root,source,True):
        raise ValueError('Source changed, moved or removed; run build before quoting')
    text = safe_path(root,source['path']).read_text('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    offsets = [0] + [m.end() for m in re.finditer('\n',text)]
    focus = args.focus_offset
    if focus is not None and (not 0 <= focus < len(text) or
                              passage is not None and not passage['start'] <= focus < passage['end']):
        raise ValueError('focus-offset must be inside the selected source/passage')
    if args.offset is not None:
        start = args.offset; end = min(len(text),start+args.budget)
    elif focus is not None:
        start=max(0,focus-args.context_chars); end=min(len(text),focus+args.context_chars+1)
    elif passage:
        start=max(0,passage['start']-args.context_chars); end=min(len(text),passage['end']+args.context_chars)
    else:
        if args.start is None or args.end is None or not 1 <= args.start <= args.end <= len(offsets):
            raise ValueError('Use read --source ID --start LINE --end LINE')
        start=offsets[args.start-1]; end=offsets[args.end] if args.end < len(offsets) else len(text)
    if not 0 <= start < end <= len(text):
        raise ValueError('Invalid or empty range')
    boundary_focus = focus if focus is not None else passage['start'] if passage else start
    start, end = context_range(text, start, end, args.context_mode, boundary_focus)
    current = live[sid]
    payload = {'source':sid,'title':current['title'],'path':str(safe_path(root,source['path'])),
             'record_path':current['record_path'],'metadata':source_metadata(current),
             'metadata_origin':'current_catalog','metadata_sha256':metadata_sha256(current),
             'source_sha256':source['sha256'],'verified_current':True,'text_verified':True,
             'context_mode':args.context_mode,'focus_offset':focus,
             'note':'Text hash verified; catalog metadata is not publication verification. Offsets count normalized Unicode characters. Read the source record and omitted context before citing.'}
    bounded_read(payload, text, offsets, start, end, args.budget, focus)


def notes(root,args):
    manifest=json_read(root/'workspace.json')
    cards=json_read(safe_path(root,manifest['reading_cards']))['cards']
    db=connect(root); mapping=mapping_for(db)
    live={s['id']:s for s in source_manifest(root)}
    query_terms=set(tokens(args.query,mapping))
    matches=[]
    for card in cards:
        if args.kind!='all' and card['kind']!=args.kind: continue
        if args.id and card['id']!=args.id: continue
        score=len(query_terms & set(tokens(card['title']+' '+' '.join(card['tags'])+' '+card['summary'],mapping)))
        if args.query and not score: continue
        text_verified=bool(card['evidence'])
        metadata_states=[]
        for evidence in card['evidence']:
            source=db.execute('SELECT * FROM sources WHERE id=?',(evidence['source'],)).fetchone()
            if not source or evidence['source'] not in live or live[evidence['source']]['text_path']!=source['path'] or not fresh_source(root,source,True) or evidence['sha256']!=source['sha256']:
                text_verified=False
            snapshot=evidence.get('metadata_sha256')
            metadata_states.append('not_recorded' if not snapshot else
                                   'match' if evidence['source'] in live and snapshot == metadata_sha256(live[evidence['source']]) else 'changed')
        metadata_status=('changed' if 'changed' in metadata_states else
                         'not_recorded' if not metadata_states or 'not_recorded' in metadata_states else 'match')
        valid=text_verified and metadata_status != 'changed'
        row={'id':card['id'],'title':card['title'],'summary':card['summary'],'kind':card['kind'],'valid':valid,'path':card['path'],
             'text_verified':text_verified,'metadata_status':metadata_status,
             'validation_scope':'text_only' if metadata_status == 'not_recorded' else 'text_and_catalog',
             'metadata_recheck_required':metadata_status != 'match','citation_ready':False}
        if args.id:
            row['evidence']=card['evidence']
            row['content']=safe_path(root,card['path']).read_text('utf-8') if valid else 'Text or catalog evidence changed; revisit the source before reusing this card.'
        matches.append((score,row))
    matches.sort(key=lambda x:-x[0])
    bounded({'mode':'reading_cards','results':[row for _,row in matches[:args.limit]],
             'coverage':'Only previously verified cards; no match does not mean no similar literature exists'},args.budget)


def main():
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path)
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ['build','search','read','notes']:
        p=commands.add_parser(name)
        p.add_argument('--budget',type=int,default=2400 if name!='read' else 3600)
        if name=='build':
            p.add_argument('--source',action='append',default=[]);p.add_argument('--force',action='store_true')
        elif name=='search':
            p.add_argument('query',nargs='?',default='');p.add_argument('--group',action='append',default=[])
            p.add_argument('--concept',action='append',default=[]);p.add_argument('--source',action='append',default=[])
            p.add_argument('--title');p.add_argument('--kind',choices=['all','guide','literature'],default='all')
            p.add_argument('--limit',type=int,default=4);p.add_argument('--pool',type=int,default=120)
            p.add_argument('--per-source',type=int,default=2);p.add_argument('--snippet',type=int,default=180)
        elif name=='read':
            p.add_argument('--passage');p.add_argument('--source');p.add_argument('--start',type=int);p.add_argument('--end',type=int)
            p.add_argument('--offset',type=int);p.add_argument('--context-chars',type=int,default=240)
            p.add_argument('--context-mode',choices=['chars','paragraph','section'],default='chars')
            p.add_argument('--focus-offset',type=int)
        else:
            p.add_argument('query',nargs='?',default='');p.add_argument('--id')
            p.add_argument('--kind',choices=['all','guide','fiction','literature'],default='all');p.add_argument('--limit',type=int,default=4)
    args=parser.parse_args()
    if args.budget < 1600 or args.budget > 30000: parser.error('budget must be 1600..30000 characters')
    for key in ['limit','pool','per_source','snippet']:
        if hasattr(args,key) and getattr(args,key)<1: parser.error(key+' must be positive')
    if args.command == 'read':
        if args.context_chars < 0: parser.error('context-chars must be nonnegative')
        if args.offset is not None and (args.focus_offset is not None or args.context_mode != 'chars'):
            parser.error('offset continuation requires chars mode and no focus-offset')
    try:
        globals()[args.command](root_for(args),args)
    except (ValueError,OSError,sqlite3.Error,KeyError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False));sys.exit(1)


if __name__=='__main__': main()
