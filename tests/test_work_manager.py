"""Version preservation and author-decision invariants; only synthetic temporary works."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(os.environ.get('WRITING_MANAGER', str(Path(__file__).resolve().parents[1] / 'skills/writing-companion/scripts/work_manager.py'))).resolve()
spec = importlib.util.spec_from_file_location('writing_work_manager', SCRIPT)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class Versions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='writing-versions-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'workspace'
        self.root.mkdir()
        (self.root / 'workspace.json').write_text('{"catalog":"catalog.json"}', encoding='utf-8')
        (self.root / 'catalog.json').write_text('{"sources":[],"works":[],"techniques":[],"practice":[]}', encoding='utf-8')
        self.original = Path(self.temp.name) / 'author.md'
        self.original.write_bytes(b'\xef\xbb\xbf# Door\r\n\r\nThe cup stayed.\r\n')
        self.candidate = Path(self.temp.name) / 'candidate.md'
        self.candidate.write_text('# Door\n\nThe cup stood by the window.\n', encoding='utf-8')

    def run_cli(self, action, *args, ok=True):
        p = subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPT), '--workspace', str(self.root), action, '--id', 'door', *map(str, args)], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(p.returncode == 0, ok, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def archive(self):
        return self.run_cli('archive', '--title', 'Door', '--text-file', self.original)

    def state(self):
        return self.run_cli('show')

    def test_adoption_reversal_preserves_every_version(self):
        self.archive()
        saved_original = self.root / 'works/door/original.md'
        self.assertEqual(saved_original.read_bytes(), self.original.read_bytes())
        first = self.state()
        self.run_cli('candidate', '--text-file', self.candidate, '--origin', 'assistant', '--expect-state', first['state_sha256'])
        self.assertEqual(self.state()['active_version'], 'original')
        self.assertFalse(self.state()['versions'][1]['adopted'])
        self.run_cli('adopt', '--version', 'v001', '--final')
        self.assertEqual(self.state()['status'], '定稿')
        self.run_cli('revert', '--version', 'original', '--reason', 'Author prefers the original')
        restored = self.state()
        self.assertEqual(restored['active_version'], 'original')
        self.assertEqual(restored['status'], '进行中')
        self.assertTrue(restored['versions'][1]['adopted'])  # Past adoption survives reversal.
        self.assertEqual(restored['decisions'][-1]['previous'], 'v001')
        self.assertEqual(saved_original.read_bytes(), self.original.read_bytes())
        self.assertEqual((self.root / 'works/door/versions/v001.md').read_bytes(), self.candidate.read_bytes())
        self.assertEqual(json.loads((self.root / 'catalog.json').read_text('utf-8'))['works'][0]['status'], '进行中')
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        handoff = self.run_cli('handoff')
        self.assertEqual(handoff['active_version'], 'original')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_unadopted_candidate_cannot_become_active_by_revert(self):
        self.archive()
        self.run_cli('candidate', '--text-file', self.candidate, '--origin', 'mixed')
        before = self.state()['state_sha256']
        self.run_cli('revert', '--version', 'v001', ok=False)
        self.assertEqual(self.state()['state_sha256'], before)

    def test_current_flags_follow_active_version_after_legacy_manual_fields(self):
        self.archive()
        self.run_cli('candidate', '--text-file', self.candidate, '--origin', 'assistant')
        record = self.root / 'works/door/record.json'
        state = json.loads(record.read_text('utf-8'))
        for version in state['versions']:
            version['currently_adopted'] = False
            version['adoption_status'] = 'pending'
        record.write_text(json.dumps(state), encoding='utf-8')
        self.run_cli('adopt', '--version', 'v001')
        self.assertEqual([v['id'] for v in self.state()['versions'] if v['currently_adopted']], ['v001'])
        self.run_cli('revert', '--version', 'original')
        final = self.state()
        self.assertEqual([v['id'] for v in final['versions'] if v['currently_adopted']], ['original'])
        self.assertTrue(final['versions'][1]['adopted'])
        self.assertEqual(final['versions'][1]['adoption_status'], 'inactive')

    def test_stale_decision_and_duplicate_archive_are_rejected(self):
        initial = self.archive()
        self.run_cli('candidate', '--text-file', self.candidate, '--origin', 'assistant')
        before = self.state()['state_sha256']
        self.run_cli('adopt', '--version', 'v001', '--expect-state', initial['state_sha256'], ok=False)
        self.run_cli('archive', '--title', 'Same ID', '--text-file', self.candidate, ok=False)
        self.assertEqual(self.state()['state_sha256'], before)
        self.assertEqual(len(json.loads((self.root / 'catalog.json').read_text('utf-8'))['works']), 1)

    def test_modified_text_blocks_further_decisions(self):
        self.archive()
        original = self.root / 'works/door/original.md'
        original.write_text('Changed outside the manager', encoding='utf-8')
        state_path = self.root / 'works/door/record.json'
        before = state_path.read_bytes()
        self.run_cli('show', ok=False)
        self.run_cli('candidate', '--text-file', self.candidate, '--origin', 'assistant', ok=False)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(original.read_text('utf-8'), 'Changed outside the manager')

    def test_outside_paths_in_records_cannot_be_read(self):
        self.archive()
        record = self.root / 'works/door/record.json'
        state = json.loads(record.read_text('utf-8'))
        state['versions'][0]['path'] = '../author.md'
        record.write_text(json.dumps(state), encoding='utf-8')
        self.run_cli('handoff', ok=False)
        self.assertEqual(self.original.read_bytes(), b'\xef\xbb\xbf# Door\r\n\r\nThe cup stayed.\r\n')

    def test_interrupted_metadata_save_keeps_recovery_evidence(self):
        self.archive()
        real_atomic = manager.atomic

        def fail_catalog(path, data):
            if path.name == 'catalog.json':
                raise OSError('Synthetic interruption')
            return real_atomic(path, data)

        class Args:
            workspace = self.root
            id = 'door'
            action = 'candidate'
            text_file = self.candidate
            origin = 'assistant'
            reason = 'test'
            expect_state = None

        with patch.object(manager, 'atomic', fail_catalog):
            with self.assertRaises(OSError):
                manager.execute(Args())
        folder = self.root / 'works/door'
        self.assertTrue((folder / 'pending-save.json').is_file())
        self.assertEqual((folder / 'original.md').read_bytes(), self.original.read_bytes())
        self.assertEqual((folder / 'versions/v001.md').read_bytes(), self.candidate.read_bytes())
        self.run_cli('adopt', '--version', 'v001', ok=False)
        self.assertFalse((self.root / '.work-manager.lock').exists())


if __name__ == '__main__':
    unittest.main()
