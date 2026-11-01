"""Observable boundary, budget, freshness and portable-workspace regression checks."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

READER = Path(os.environ.get('WRITING_READER', str(Path(__file__).resolve().parents[1] / 'skills/writing-companion/scripts/library_reader.py'))).resolve()


class ReaderContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='reader-context-')
        self.root = Path(self.temp.name)
        self.save('workspace.json', {'catalog': 'catalog.json'})
        self.text = ('# 卷首\n\n开场。\n\n## 第一章\n\n第一段铺垫。\n\n第二段目标词，接着否定：这不是事实。\n\n'
                     '第三段保留说话者。\n\n## 第二章\n\n另一个人说的无关内容。\n')
        self.add_text(self.text)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, name, value):
        (self.root / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def add_text(self, text):
        self.path = self.root / 'text.md'
        self.path.write_bytes(text.encode('utf-8'))
        self.save('catalog.json', {'sources': [{'id': 'sample', 'title': '原创边界测试',
                                               'text_path': 'text.md', 'record_path': 'record.md'}]})
        self.run_tool('build')

    def run_tool(self, *arguments, ok=True, budget=3600):
        result = subprocess.run([sys.executable, '-X', 'utf8', str(READER), *arguments,
                                 '--budget', str(budget)], cwd=self.root, capture_output=True)
        output = result.stdout.decode('utf-8')
        self.assertEqual(result.returncode == 0, ok, output + result.stderr.decode('utf-8'))
        self.assertLessEqual(len(output), budget)
        self.assertNotIn('\r\n', output)
        return json.loads(output)

    def read_focus(self, mode, focus=None, **options):
        focus = self.text.index('目标词') if focus is None else focus
        return self.run_tool('read', '--source', 'sample', '--focus-offset', str(focus),
                             '--context-mode', mode, '--context-chars', '0', **options)

    def exact(self, row):
        normalized = self.path.read_bytes().decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
        self.assertEqual(row['text'], normalized[row['offset_start']:row['offset_end']])
        self.assertEqual(row['source_sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(row['next_offset'], row['offset_end'] if row['offset_end'] < len(normalized) else None)
        self.assertEqual(row['line_start'], normalized.count('\n', 0, row['offset_start']) + 1)

    def test_paragraph_expands_complete_meaning(self):
        row = self.read_focus('paragraph')
        self.assertEqual(row['text'], '第二段目标词，接着否定：这不是事实。\n\n')
        self.assertTrue(row['context_complete'])
        self.assertFalse(row['output_truncated'])
        self.exact(row)

    def test_section_stays_inside_markdown_heading(self):
        row = self.read_focus('section')
        self.assertTrue(row['text'].startswith('## 第一章\n'))
        self.assertIn('第三段保留说话者', row['text'])
        self.assertNotIn('## 第二章', row['text'])
        self.assertNotIn('开场', row['text'])
        self.exact(row)

    def test_paragraph_recovers_qualifier_outside_fixed_radius(self):
        self.text = '# 原创\n\n目标词：' + ('说话者的猜测，' * 65) + '但叙述者随后明确否定，这不是事实。\n\n下一段。'
        self.add_text(self.text)
        focus = str(self.text.index('目标词'))
        fixed = self.run_tool('read', '--source', 'sample', '--focus-offset', focus,
                              '--context-mode', 'chars', '--context-chars', '240')
        paragraph = self.run_tool('read', '--source', 'sample', '--focus-offset', focus,
                                  '--context-mode', 'paragraph', '--context-chars', '240')
        self.assertNotIn('这不是事实', fixed['text'])
        self.assertIn('这不是事实', paragraph['text'])
        self.assertTrue(paragraph['context_complete'])
        self.exact(paragraph)

    def test_nested_section_includes_child_until_peer(self):
        self.text = '# 总章\n\n引子\n\n## 小节甲\n\n目标词\n\n### 子节\n\n延伸\n\n## 小节乙\n\n别处\n'
        self.add_text(self.text)
        row = self.read_focus('section')
        self.assertIn('### 子节', row['text'])
        self.assertNotIn('## 小节乙', row['text'])
        self.exact(row)

    def test_long_paragraph_preserves_focus_and_budget(self):
        self.text = '# 长段\n\n' + '前' * 12000 + '目标词这不是事实' + '后' * 12000 + '\n\n## 下章\n内容'
        self.add_text(self.text)
        row = self.read_focus('paragraph', budget=1600)
        self.assertIn('目标词这不是事实', row['text'])
        self.assertTrue(row['output_truncated'])
        self.assertFalse(row['context_complete'])
        self.assertTrue(row['context_omitted_before'])
        self.assertTrue(row['context_omitted_after'])
        self.exact(row)

    def test_long_section_keeps_focus_late_in_chapter(self):
        self.text = '# 卷\n\n## 第一章\n\n' + ('铺垫。\n\n' * 5000) + '目标词\n\n## 第二章\n别处'
        self.add_text(self.text)
        row = self.read_focus('section', budget=1600)
        self.assertIn('目标词', row['text'])
        self.assertTrue(row['context_omitted_before'])
        self.assertNotIn('第二章', row['text'])
        self.exact(row)

    def test_exact_continuation_reconstructs_unicode_crlf(self):
        original = '# 原创\r\n\r\n' + ('"\\\t🙂Straße\r\n' * 500)
        self.add_text(original)
        parts, offset = [], 0
        while offset is not None:
            row = self.run_tool('read', '--source', 'sample', '--offset', str(offset), budget=1600)
            self.exact(row)
            self.assertGreater(row['offset_end'], offset)
            parts.append(row['text'])
            offset = row['next_offset']
        self.assertEqual(''.join(parts), original.replace('\r\n', '\n'))

    def test_search_match_offset_uses_original_unicode(self):
        self.text = '# 测试\n\nStraße İSTANBUL 目标词\n'
        self.add_text(self.text)
        for word, raw in [('strasse', 'Straße'), ('i̇stanbul', 'İSTANBUL'), ('目标词', '目标词')]:
            hit = self.run_tool('search', word)['results'][0]
            self.assertEqual(hit['match_offset'], self.text.index(raw))
            row = self.run_tool('read', '--passage', hit['passage'], '--focus-offset', str(hit['match_offset']),
                                '--context-mode', 'paragraph')
            self.assertIn(raw, row['text'])
            self.exact(row)

    def test_strict_hash_rejects_changed_same_size_same_mtime(self):
        hit = self.run_tool('search', '目标词')['results'][0]
        previous = self.path.stat()
        self.path.write_bytes(self.text.replace('目标词', '替换词').encode('utf-8'))
        os.utime(self.path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        row = self.run_tool('read', '--passage', hit['passage'], '--context-mode', 'section', ok=False)
        self.assertIn('Source changed', row['error'])

    def test_invalid_focus_and_continuation_modes_are_rejected(self):
        for args in [('--focus-offset', '-1'), ('--focus-offset', '999999'),
                     ('--context-chars', '-1'), ('--offset', '0', '--context-mode', 'section'),
                     ('--offset', '0', '--focus-offset', '0')]:
            result = subprocess.run([sys.executable, str(READER), 'read', '--source', 'sample', *args],
                                    cwd=self.root, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_minimal_workspace_without_settings_builds_and_searches(self):
        self.assertTrue((self.root / 'library/retrieval.sqlite3').is_file())
        row = self.run_tool('search', '目标词')
        self.assertEqual(row['scope']['eligible_sources'], 1)
        self.assertTrue(row['results'])
        self.assertIn('Unknown concept', self.run_tool('search', '--concept', 'missing', ok=False)['error'])


if __name__ == '__main__':
    unittest.main()
