"""Catalog-only edits must not leave stale citation metadata or pretend old cards were rechecked."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'skills/writing-companion/scripts/library_reader.py'
TEMPLATE = REPO / 'skills/writing-companion/assets/workspace-template'


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='writing-metadata-')
        self.root = Path(self.temp.name) / 'workspace'
        shutil.copytree(TEMPLATE, self.root)
        self.folder = self.root / 'library/sample'
        self.folder.mkdir()
        (self.folder / 'text.md').write_text('# 测试\n\n回想昨夜的雨声，窗台还湿着。\n', encoding='utf-8')
        (self.folder / 'record.md').write_text('# 来源\n\n原创测试短文；旧版本说明。\n', encoding='utf-8')
        self.source = {'id': 'sample', 'title': '旧题名', 'authors': ['旧作者'],
                       'coverage': ['full_text'], 'edition_notes': ['旧版'],
                       'text_path': 'library/sample/text.md', 'record_path': 'library/sample/record.md'}
        self.save_catalog()
        self.run_tool('build')
        self.initial = self.read_source()
        self.passage = self.run_tool('search', '回想')['results'][0]['passage']

    def tearDown(self):
        self.temp.cleanup()

    def save_catalog(self):
        write_json(self.root / 'catalog.json', {'schema_version': 1, 'sources': [self.source],
                                              'works': [], 'techniques': [], 'practice': []})

    def run_tool(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), '--workspace', str(self.root), *args],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def read_source(self):
        return self.run_tool('read', '--source', 'sample', '--start', '1', '--end', '3')

    def change_metadata(self):
        self.source.update(title='新 "题名"', authors=['新作者'], coverage=['excerpt'],
                           edition_notes=['更正后的节选版'], translator='单名译者',
                           translators=['译者甲', '译者乙'], edition='第二版', languages=['zh'])
        self.save_catalog()

    def save_card(self, snapshot=None, extra_evidence=None):
        (self.root / 'techniques/sample-card.md').write_text('# 阅读卡\n\n对原创雨声片段的分析。\n', encoding='utf-8')
        evidence = {'source': 'sample', 'sha256': self.initial['source_sha256'], 'lines': [1, 3]}
        if snapshot:
            evidence['metadata_sha256'] = snapshot
        write_json(self.root / 'techniques/reading-cards.json', {'schema_version': 1, 'cards': [{
            'id': 'sample-card', 'title': '雨声', 'kind': 'literature', 'tags': ['细节'],
            'summary': '动作与感受', 'path': 'techniques/sample-card.md',
            'evidence': [evidence, *(extra_evidence or [])],
        }]})

    def card(self):
        return self.run_tool('notes', '--id', 'sample-card')['results'][0]

    def test_read_uses_live_metadata_without_rebuild(self):
        self.change_metadata()
        current = self.run_tool('read', '--passage', self.passage)
        self.assertEqual(current['title'], self.source['title'])
        for key in ['authors', 'coverage', 'edition_notes', 'translator', 'translators', 'edition', 'languages']:
            self.assertEqual(current['metadata'][key], self.source[key])
        self.assertEqual(current['source_sha256'], self.initial['source_sha256'])
        self.assertNotEqual(current['metadata_sha256'], self.initial['metadata_sha256'])
        self.assertEqual(current['metadata_origin'], 'current_catalog')
        self.assertTrue(current['text_verified'])
        self.assertTrue(current['verified_current'])

    def test_title_author_and_translator_filters_use_live_values(self):
        self.change_metadata()
        for query in [self.source['title'], '新作者', '单名译者', '译者甲']:
            for args in [('search', '--title', query), ('search', '回想', '--title', query)]:
                rows = self.run_tool(*args)['results']
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['title'], self.source['title'])
        for old in ['旧题名', '旧作者']:
            self.assertEqual(self.run_tool('search', '--title', old)['results'], [])
            self.assertEqual(self.run_tool('search', '回想', '--title', old)['results'], [])

    def test_current_kind_and_record_path_without_reindex(self):
        path = self.root / 'techniques/retrieval-config.json'
        settings = json.loads(path.read_text('utf-8'))
        settings['guide_source_ids'] = ['sample']
        write_json(path, settings)
        for args in [('search', '--kind', 'guide'), ('search', '回想', '--kind', 'guide')]:
            self.assertEqual(self.run_tool(*args)['results'][0]['source'], 'sample')
        self.assertEqual(self.run_tool('search', '回想', '--kind', 'literature')['results'], [])
        (self.folder / 'new-record.md').write_text('# 更新后的来源记录\n', encoding='utf-8')
        self.source['record_path'] = 'library/sample/new-record.md'
        self.save_catalog()
        self.assertEqual(self.read_source()['record_path'], self.source['record_path'])

    def test_metadata_only_build_preserves_passages(self):
        with closing(sqlite3.connect(self.root / 'library/retrieval.sqlite3')) as db:
            before = db.execute('SELECT id,source,start,end,text FROM passages').fetchall()
        self.change_metadata()
        result = self.run_tool('build')
        self.assertEqual(result['changed'], 0)
        self.assertEqual(result['unchanged'], 1)
        with closing(sqlite3.connect(self.root / 'library/retrieval.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT id,source,start,end,text FROM passages').fetchall(), before)
            cached = json.loads(db.execute('SELECT meta FROM sources WHERE id=?', ('sample',)).fetchone()[0])
        self.assertEqual(cached['translators'], self.source['translators'])
        self.assertEqual(self.run_tool('read', '--passage', self.passage)['metadata']['coverage'], ['excerpt'])

    def test_legacy_card_does_not_gain_metadata_verification(self):
        self.save_card()
        self.change_metadata()
        for rebuild in [False, True]:
            if rebuild:
                self.run_tool('build')
            card = self.card()
            self.assertTrue(card['text_verified'])
            self.assertTrue(card['valid'])
            self.assertEqual(card['validation_scope'], 'text_only')
            self.assertEqual(card['metadata_status'], 'not_recorded')
            self.assertTrue(card['metadata_recheck_required'])
            self.assertFalse(card['citation_ready'])
        stored = json.loads((self.root / 'techniques/reading-cards.json').read_text('utf-8'))
        self.assertNotIn('metadata_sha256', stored['cards'][0]['evidence'][0])

    def test_card_snapshot_detects_change_and_rebuild_cannot_review_it(self):
        self.save_card(self.initial['metadata_sha256'])
        self.assertTrue(self.card()['valid'])
        self.assertEqual(self.card()['metadata_status'], 'match')
        self.change_metadata()
        for rebuild in [False, True]:
            if rebuild:
                self.run_tool('build')
            card = self.card()
            self.assertTrue(card['text_verified'])
            self.assertFalse(card['valid'])
            self.assertEqual(card['metadata_status'], 'changed')
            self.assertNotIn('对原创雨声片段的分析', card['content'])
        # A new snapshot is recorded only after the caller actually rechecks the source.
        self.save_card(self.read_source()['metadata_sha256'])
        self.assertTrue(self.card()['valid'])
        self.assertEqual(self.card()['metadata_status'], 'match')

    def test_text_changes_and_source_removal_still_reject_old_evidence(self):
        self.save_card(self.initial['metadata_sha256'])
        path = self.folder / 'text.md'
        path.write_text(path.read_text('utf-8') + '\n补充测试文字。\n', encoding='utf-8')
        self.assertIn('error', self.run_tool('read', '--passage', self.passage, ok=False))
        self.assertFalse(self.card()['text_verified'])
        self.assertFalse(self.card()['valid'])
        self.run_tool('build')
        self.assertIn('error', self.run_tool('read', '--passage', self.passage, ok=False))
        self.assertFalse(self.card()['valid'])
        write_json(self.root / 'catalog.json', {'sources': []})
        self.assertIn('error', self.read_source_error())
        self.assertEqual(self.run_tool('search', '回想')['results'], [])

    def read_source_error(self):
        return self.run_tool('read', '--source', 'sample', '--start', '1', '--end', '3', ok=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--script', type=Path)
    options, remaining = parser.parse_known_args()
    if options.script:
        SCRIPT = options.script.resolve()
    unittest.main(argv=[sys.argv[0], *remaining])
