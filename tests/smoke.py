"""Offline behavioral smoke checks with synthetic original text and a temporary workspace."""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / 'skills/writing-companion'
READER = SKILL / 'scripts/library_reader.py'
TEMPLATE = SKILL / 'assets/workspace-template'


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def run(root, *args, ok=True, explicit=True):
    cmd = [sys.executable, str(READER)]
    if explicit:
        cmd += ['--workspace', str(root)]
    result = subprocess.run(cmd + list(args), cwd=root, capture_output=True, text=True, encoding='utf-8')
    assert (result.returncode == 0) == ok, result.stdout + result.stderr
    return json.loads(result.stdout)


def check():
    with tempfile.TemporaryDirectory(prefix='writing-companion-test-') as temp:
        root = Path(temp) / 'workspace'
        shutil.copytree(TEMPLATE, root)
        empty = run(root, 'build', explicit=False)
        assert empty['sources'] == 0
        assert run(root, 'search', '回想')['results'] == []
        assert run(root, 'notes')['results'] == []

        texts = {
            'sample-a': '# 门口\n\n父亲没有说起那个秘密。我回想昨夜，雨水还留在门边。\n',
            'sample-b': '# 写作札记\n\n回想一个具体动作，再判断它怎样呈现人物的心情。\n',
        }
        sources = []
        for sid, content in texts.items():
            folder = root / 'library' / sid
            folder.mkdir()
            (folder / 'text.md').write_text(content, encoding='utf-8')
            (folder / 'record.md').write_text('# 测试来源\n\n专门编写的原创测试短文，不是文学引文。\n', encoding='utf-8')
            sources.append({'id': sid, 'title': sid, 'authors': ['测试作者'],
                            'text_path': f'library/{sid}/text.md', 'record_path': f'library/{sid}/record.md',
                            'coverage': '测试短文全文', 'edition_notes': '原创测试用'})
        write_json(root / 'catalog.json', {'schema_version': 1, 'sources': sources, 'works': [], 'techniques': [], 'practice': []})
        config_path = root / 'techniques/retrieval-config.json'
        config = json.loads(config_path.read_text('utf-8'))
        config['guide_source_ids'] = ['sample-b']
        write_json(config_path, config)
        assert run(root, 'build')['sources'] == 2
        all_hits = run(root, 'search', '回想')
        assert {h['source'] for h in all_hits['results']} == {'sample-a', 'sample-b'}
        assert all_hits['scope']['mode'] == 'library_wide'
        assert all_hits['scope']['eligible_sources'] == 2
        assert all(not h['citation_ready'] for h in all_hits['results'])
        literature = run(root, 'search', '回想', '--kind', 'literature')
        assert [h['source'] for h in literature['results']] == ['sample-a']
        assert [h['source'] for h in run(root, 'search', '回想', '--kind', 'guide')['results']] == ['sample-b']
        assert run(root, 'search', '--concept', 'family', '--concept', 'concealment')['results'][0]['source'] == 'sample-a'
        fallback = subprocess.run([sys.executable, str(SKILL / 'scripts/search_library.py'), '回想', '--source', 'sample-a'],
                                  cwd=root, capture_output=True, text=True, encoding='utf-8')
        assert fallback.returncode == 0, fallback.stderr
        fallback_rows = [json.loads(line) for line in fallback.stdout.splitlines()]
        assert fallback_rows[0]['source_id'] == 'sample-a' and fallback_rows[0]['match_line'] == 3
        assert 'error' in run(root, 'search', '雨', ok=False)
        passage = literature['results'][0]['passage']
        quote = run(root, 'read', '--passage', passage)
        assert quote['verified_current'] and quote['text'] == texts['sample-a']
        assert quote['line_start'] == 1
        assert quote['source_sha256'] == hashlib.sha256((root / 'library/sample-a/text.md').read_bytes()).hexdigest()
        assert run(root, 'read', '--source', 'sample-a', '--start', '3', '--end', '3')['text'] == texts['sample-a'].splitlines(keepends=True)[2]

        card_path = root / 'techniques/sample-card.md'
        card_path.write_text('# 测试阅读卡\n\n已核验测试短文中的动作。\n', encoding='utf-8')
        write_json(root / 'techniques/reading-cards.json', {'schema_version': 1, 'cards': [{
            'id': 'sample-card', 'title': '动作', 'kind': 'literature', 'tags': ['动作'], 'summary': '动作测试',
            'path': 'techniques/sample-card.md', 'evidence': [{'source': 'sample-a', 'sha256': quote['source_sha256'], 'lines': [1, 3]}]
        }]})
        assert run(root, 'notes', '--id', 'sample-card')['results'][0]['valid']
        target = root / 'library/sample-a/text.md'
        target.write_text(texts['sample-a'] + '\n又一阵雨落下。\n', encoding='utf-8')
        stale = run(root, 'search', '回想')
        assert 'sample-a' in stale['stale_sources']
        assert not run(root, 'notes', '--id', 'sample-card')['results'][0]['valid']
        assert 'error' in run(root, 'read', '--passage', passage, ok=False)
        assert run(root, 'build')['changed'] == 1
        assert not run(root, 'search', '回想')['stale_sources']
        assert 'error' in run(root, 'read', '--passage', passage, ok=False)

        # Mapping is optional and is read from the workspace, never a developer's machine.
        mapped = Path(temp) / 'mapped'
        shutil.copytree(TEMPLATE, mapped)
        (mapped / 'characters.txt').write_text('憶\t忆\n', encoding='utf-8')
        settings = json.loads((mapped / 'techniques/retrieval-config.json').read_text('utf-8'))
        settings['normalization_dictionary'] = 'characters.txt'
        write_json(mapped / 'techniques/retrieval-config.json', settings)
        folder = mapped / 'library/sample'
        folder.mkdir()
        (folder / 'text.md').write_text('# 测试\n\n回憶雨声。\n', encoding='utf-8')
        (folder / 'record.md').write_text('原创字形测试', encoding='utf-8')
        write_json(mapped / 'catalog.json', {'sources': [{'id': 'sample', 'title': '映射测试', 'text_path': 'library/sample/text.md', 'record_path': 'library/sample/record.md'}]})
        assert 'traditional' in run(mapped, 'build')['normalization']
        hit = run(mapped, 'search', '回忆')['results'][0]
        assert '回憶' in run(mapped, 'read', '--passage', hit['passage'])['text']
    print('PASS: empty workspace, full-scope retrieval, kind filters, concepts, verified lines, stale sources/cards, rebuild and optional normalization')


if __name__ == '__main__':
    check()
