"""Index-free literal search: complete source scan, bounded JSONL, exact excerpt offsets."""
import argparse
import bisect
import hashlib
import json
import sys
from pathlib import Path


def json_read(path):
    return json.loads(path.read_text('utf-8-sig'))


def safe_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Path outside the library workspace')
    return path


def workspace(args):
    if args.workspace:
        return args.workspace.expanduser().resolve()
    if (Path.cwd() / 'workspace.json').is_file():
        return Path.cwd().resolve()
    binding = Path(__file__).resolve().parents[1] / 'local-workspace.json'
    if not binding.is_file():
        raise ValueError('Specify --workspace or configure local-workspace.json')
    return Path(json_read(binding)['default_workspace']).expanduser().resolve()


def original_index(line, folded, position):
    """Map casefold expansions such as ß -> ss back to original characters."""
    if len(line) == len(folded):
        return position
    offset = 0
    for index, char in enumerate(line):
        offset += len(char.casefold())
        if offset > position:
            return index
    return len(line)


def refresh_excerpt(row):
    row['offset_end'] = row['offset_start'] + len(row['context'])
    row['line_end'] = row['line_start'] + row['context'][:-1].count('\n')
    row['next_offset'] = row['offset_end'] if row['offset_end'] < row['source_characters'] else None


def excerpt(source, path, raw, start, end, truncated=False):
    text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    line_start = text.count('\n', 0, start) + 1
    row = {
        'source_id': source['id'], 'title': source['title'], 'path': str(path),
        'source_sha256': hashlib.sha256(raw).hexdigest(),
        'line_start': line_start, 'column_start': start - text.rfind('\n', 0, start),
        'offset_start': start, 'source_characters': len(text),
        'context': text[start:end], 'truncated': truncated,
        'citation_ready': False,
    }
    refresh_excerpt(row)
    return row


def emit(rows, summary, budget):
    """Count the complete serialized JSONL, including escapes and newlines."""
    summary['output_budget_chars'] = budget
    summary['output_truncated'] = False

    def encode():
        if 'matches_shown' in summary:
            summary['matches_shown'] = len(rows)
            summary['limit_reached'] = summary['total_matches'] > len(rows)
        if 'shown_sources' in summary:
            summary['shown_sources'] = len(rows)
        return ''.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n'
                       for row in [*rows, summary])

    output = encode()
    while len(output) > budget:
        summary['output_truncated'] = True
        if len(rows) > 1:
            rows.pop()
        elif rows and len(rows[0].get('context', '')) > 32:
            row = rows[0]
            count = max(32, len(row['context']) - max(1, (len(output) - budget) // 2))
            # Keep the discovered match in a search excerpt, even with long lead-in text.
            if 'match_offset' in row and row['match_offset'] >= row['offset_start'] + count:
                shift = row['match_offset'] - row['offset_start']
                removed = row['context'][:shift]
                row['line_start'] += removed.count('\n')
                row['column_start'] = (len(removed) - removed.rfind('\n') if '\n' in removed
                                       else row['column_start'] + len(removed))
                row['offset_start'] += shift
                row['context'] = row['context'][shift:]
            row['context'] = row['context'][:count]
            row['truncated'] = True
            refresh_excerpt(row)
        else:
            raise ValueError('Budget cannot fit result metadata; narrow the request or increase --budget')
        output = encode()
    sys.stdout.write(output)


def run(args):
    root = workspace(args)
    manifest = json_read(root / 'workspace.json')
    sources = json_read(safe_path(root, manifest['catalog']))['sources']
    if args.source:
        sources = [source for source in sources if source['id'] == args.source]
    if args.title:
        needle = args.title.casefold()
        def title_matches(source):
            values = [source['title'], source['id']]
            for key in ('authors', 'requested_titles'):
                items = source.get(key) or []
                values.extend([items] if isinstance(items, str) else items)
            return any(needle in str(value).casefold() for value in values)
        sources = [source for source in sources if title_matches(source)]

    if args.read:
        if len(sources) != 1:
            raise ValueError('Read requires one known --source ID')
        source = sources[0]
        path = safe_path(root, source['text_path'])
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if args.expect_sha256 and digest != args.expect_sha256:
            raise ValueError('Source changed; search again before continuing')
        text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
        start = args.offset or 0
        if not 0 <= start < len(text):
            raise ValueError('Offset outside the current text')
        end = min(len(text), start + args.length)
        row = excerpt(source, path, raw, start, end, end < len(text))
        emit([row], {'mode': 'read', 'offset_unit': 'Unicode characters after newline normalization'}, args.budget)
        return

    if not args.query:
        emit(sources[:args.limit], {'mode': 'catalog', 'matched_sources': len(sources),
             'shown_sources': 0, 'note': 'Catalog entries do not prove complete text or a matching edition.'}, args.budget)
        return

    query = args.query.casefold()
    groups, failures = [], []
    scanned = total_matches = matched_sources = 0
    for source in sources:
        hits, count, offset = [], 0, 0
        try:
            path = safe_path(root, source['text_path'])
            before = path.stat()
            with path.open(encoding='utf-8-sig') as stream:
                for number, line in enumerate(stream, 1):
                    folded = line.casefold()
                    position = folded.find(query)
                    if position >= 0:
                        count += 1
                        if len(hits) < args.limit:
                            hits.append((number, offset + original_index(line, folded, position)))
                    offset += len(line)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('Source changed during scan')
        except (OSError, UnicodeError, ValueError):
            failures.append(source['id'])
            continue
        scanned += 1
        total_matches += count
        if count:
            matched_sources += 1
            groups.append((source, hits, after))

    # One match per source before extra matches; source ID makes ties independent of import order.
    groups.sort(key=lambda group: group[0]['id'])
    selected = []
    for index in range(max((len(group[1]) for group in groups), default=0)):
        for source, hits, stat in groups:
            if index < len(hits):
                selected.append((source, hits[index], stat))
                if len(selected) == args.limit:
                    break
        if len(selected) == args.limit:
            break

    rows = []
    for source, (number, match), scanned_stat in selected:
        try:
            path = safe_path(root, source['text_path'])
            current = path.stat()
            if (current.st_size, current.st_mtime_ns) != (scanned_stat.st_size, scanned_stat.st_mtime_ns):
                raise ValueError('Source changed after scan')
            raw = path.read_bytes()
            text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
            offsets = [0] + [pos + 1 for pos, char in enumerate(text) if char == '\n']
            lo = max(0, number - 1 - args.context)
            hi = min(len(offsets), number + args.context)
            context_start = offsets[lo]
            context_end = offsets[hi] if hi < len(offsets) else len(text)
            lead = min(80, args.snippet // 3)
            start = max(context_start, match - lead)
            end = min(context_end, start + args.snippet)
            row = excerpt(source, path, raw, start, end,
                          start > context_start or end < context_end)
            row.update(match_line=number, match_column=match - offsets[number - 1] + 1,
                       match_offset=match)
            rows.append(row)
        except (OSError, UnicodeError, ValueError):
            if source['id'] not in failures:
                failures.append(source['id'])

    emit(rows, {
        'mode': 'literal_search', 'method': 'casefold substring; source round-robin; no semantic ranking',
        'matches_shown': 0, 'total_matches': total_matches, 'match_unit': 'matching lines',
        'matched_sources': matched_sources, 'sources_scanned': scanned,
        'candidate_sources': len(sources), 'scan_complete': not failures,
        'failed_source_count': len(failures), 'failed_sources': [str(sid)[:80] for sid in failures[:5]],
        'failed_sources_truncated': len(failures) > 5,
    }, args.budget)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query', nargs='?', default='')
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--title', help='Filter by title, author or source ID')
    parser.add_argument('--source', help='Exact source ID')
    parser.add_argument('--limit', type=int, default=12)
    parser.add_argument('--context', type=int, default=3)
    parser.add_argument('--snippet', type=int, default=600)
    parser.add_argument('--budget', type=int, default=3600)
    parser.add_argument('--read', action='store_true', help='Read original text without an index')
    parser.add_argument('--offset', type=int)
    parser.add_argument('--length', type=int, default=1800)
    parser.add_argument('--expect-sha256', help='Reject continuation from a changed source')
    args = parser.parse_args()
    if args.limit < 1 or args.snippet < 1 or args.length < 1:
        parser.error('limit, snippet and length must be positive')
    if not 0 <= args.context <= 20 or not 1600 <= args.budget <= 30000:
        parser.error('context must be 0..20 and budget 1600..30000 characters')
    if args.read and (not args.source or args.query):
        parser.error('--read requires --source and no query')
    if (args.offset is not None or args.expect_sha256) and not args.read:
        parser.error('--offset and --expect-sha256 require --read')
    if args.offset is not None and args.offset < 0:
        parser.error('offset must be nonnegative')
    try:
        run(args)
    except (OSError, UnicodeError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)[:600]}, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    main()
