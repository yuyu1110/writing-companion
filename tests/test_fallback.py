"""Regression checks for complete discovery and bounded, index-free excerpts."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'skills/writing-companion/scripts/search_library.py'


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='writing-fallback-')
        self.root = Path(self.temp.name)
        self.sources = []
        (self.root / 'workspace.json').write_text('{"catalog":"catalog.json"}', encoding='utf-8')
        self.save_catalog()

    def tearDown(self):
        self.temp.cleanup()

    def save_catalog(self):
        (self.root / 'catalog.json').write_text(json.dumps({'sources': self.sources}, ensure_ascii=False), encoding='utf-8')

    def add(self, sid, text, **metadata):
        path = self.root / (sid + '.md')
        path.write_bytes(text.encode('utf-8'))
        self.sources.append({'id': sid, 'title': sid, 'text_path': path.name, **metadata})
        self.save_catalog()
        return path

    def run_tool(self, *args, ok=True, budget=3600, explicit=True):
        cmd = [sys.executable, str(SCRIPT)]
        if explicit:
            cmd += ['--workspace', str(self.root)]
        cmd += ['--budget', str(budget), *args]
        result = subprocess.run(cmd, cwd=self.root, capture_output=True)
        stdout = result.stdout.decode('utf-8')
        stderr = result.stderr.decode('utf-8', errors='replace')
        self.assertEqual(result.returncode == 0, ok, stdout + stderr)
        self.assertLessEqual(len(stdout), budget)
        self.assertNotIn('\r\n', stdout)
        self.assertTrue(stdout.endswith('\n'))
        return [json.loads(line) for line in stdout.splitlines()]

    def test_full_scan_source_diversity_and_order(self):
        self.add('a', '回想甲\n回想乙\n回想丙\n')
        self.add('b', '回想丁\n')
        self.add('c', '今天下雨。\n')
        rows = self.run_tool('回想', '--limit', '2', budget=6000)
        self.assertEqual([row['source_id'] for row in rows[:-1]], ['a', 'b'])
        self.assertEqual(rows[-1]['sources_scanned'], 3)
        self.assertEqual(rows[-1]['total_matches'], 4)
        self.assertEqual(rows[-1]['matched_sources'], 2)
        self.assertTrue(rows[-1]['scan_complete'])
        limited = self.run_tool('回想', '--limit', '1')
        self.assertEqual(limited[-1]['sources_scanned'], 3)
        self.assertEqual(limited[-1]['total_matches'], 4)
        self.sources.reverse()
        self.save_catalog()
        reversed_rows = self.run_tool('回想', '--limit', '2', budget=6000)
        self.assertEqual([row['source_id'] for row in reversed_rows[:-1]], ['a', 'b'])

    def test_casefold_offsets_point_to_original_text(self):
        text = '前 Straße 后\nİstanbul STRASSE🙂尾\n'
        self.add('unicode', text)
        for query, original in [('strasse', 'Straße'), ('i̇stanbul', 'İstanbul')]:
            rows = self.run_tool(query, '--context', '0', '--limit', '1')
            row = rows[0]
            self.assertEqual(row['match_offset'], text.index(original))
            self.assertEqual(row['context'], text[row['offset_start']:row['offset_end']])
            self.assertIn(original, row['context'])

    def test_long_line_and_complete_jsonl_budget(self):
        text = '前' * 25000 + '目标词' + '后' * 25000
        self.add('long', text)
        row, summary = self.run_tool('目标词', '--context', '20', '--limit', '1', budget=1800)
        self.assertIn('目标词', row['context'])
        self.assertTrue(row['truncated'])
        self.assertEqual(row['context'], text[row['offset_start']:row['offset_end']])
        self.assertEqual(row['next_offset'], row['offset_end'])
        self.assertEqual(summary['sources_scanned'], 1)
        tiny = self.run_tool('目标词', '--snippet', '1', '--limit', '1')
        self.assertEqual(tiny[0]['context'], '目')

    def test_budget_counts_escaped_content_and_metadata(self):
        text = 'needle ' + ('"\\\t\x01' * 5000)
        self.add('escaped', text, title='带有"引号"和\\路径的标题\n第二行')
        row, summary = self.run_tool('needle', '--snippet', '30000', budget=1600)
        self.assertIn('needle', row['context'])
        self.assertTrue(summary['output_truncated'])
        self.assertEqual(row['context'], text[row['offset_start']:row['offset_end']])

    def test_filters_and_empty_catalog(self):
        self.assertEqual(self.run_tool('不存在')[-1]['candidate_sources'], 0)
        self.add('a', '回想\n', title='A "quoted" path\\part',
                 authors=['Sample Author'], requested_titles=['Requested Name'])
        self.add('b', '回想\n')
        for flag, value in [('--source', 'a'), ('--title', 'sample author'),
                            ('--title', 'requested name'), ('--title', 'A "quoted"'),
                            ('--title', 'path\\part')]:
            rows = self.run_tool('回想', flag, value, explicit=False)
            self.assertEqual(rows[-1]['candidate_sources'], 1)
            self.assertEqual(rows[0]['source_id'], 'a')
        no_match = self.run_tool('回想', '--source', 'a', '--title', 'b')
        self.assertEqual(no_match[-1]['candidate_sources'], 0)
        self.assertEqual(self.run_tool('回想', '--title', '", "')[-1]['candidate_sources'], 0)
        catalog = self.run_tool('--limit', '1')
        self.assertEqual(catalog[-1]['matched_sources'], 2)
        self.assertEqual(catalog[-1]['shown_sources'], 1)

    def test_read_continuation_without_index_and_changed_source(self):
        original = ('第一行🙂\r\nİstanbul é\r\n' + '"\\\t\x01' * 100 + '\r\n结尾')
        normalized = original.replace('\r\n', '\n')
        path = self.add('pages', original)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        offset, parts = 0, []
        while offset is not None:
            row, summary = self.run_tool('--read', '--source', 'pages', '--offset', str(offset),
                                         '--length', '30000', '--expect-sha256', digest, budget=1600)
            self.assertEqual(row['offset_start'], offset)
            self.assertEqual(row['context'], normalized[row['offset_start']:row['offset_end']])
            parts.append(row['context'])
            self.assertGreater(row['offset_end'], offset)
            offset = row['next_offset']
        self.assertEqual(''.join(parts), normalized)
        self.assertFalse((self.root / 'library/retrieval.sqlite3').exists())
        path.write_bytes((original + '改动').encode('utf-8'))
        self.assertIn('changed', self.run_tool('--read', '--source', 'pages', '--expect-sha256', digest, ok=False)[0]['error'])

    def test_unreadable_source_does_not_hide_later_hit(self):
        path = self.add('missing', '回想\n')
        path.unlink()
        self.add('valid', '回想\n')
        rows = self.run_tool('回想', '--limit', '1')
        self.assertEqual(rows[0]['source_id'], 'valid')
        self.assertEqual(rows[-1]['sources_scanned'], 1)
        self.assertEqual(rows[-1]['candidate_sources'], 2)
        self.assertEqual(rows[-1]['failed_sources'], ['missing'])
        self.assertFalse(rows[-1]['scan_complete'])

    def test_argument_and_range_errors(self):
        self.add('a', '文字\n')
        for args in [('回想', '--limit', '0'), ('回想', '--context', '-1'),
                     ('--read', '--source', 'a', '--offset', '-1'),
                     ('--read', '--source', 'a', '--offset', '999'),
                     ('--read', '--source', 'unknown'), ('--offset', '1')]:
            result = subprocess.run([sys.executable, str(SCRIPT), '--workspace', str(self.root), *args],
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--script', type=Path)
    options, remaining = parser.parse_known_args()
    if options.script:
        SCRIPT = options.script.resolve()
    unittest.main(argv=[sys.argv[0], *remaining])
