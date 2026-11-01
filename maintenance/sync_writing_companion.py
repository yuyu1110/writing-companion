"""Export only reviewed portable files; render GPT rules from the same canonical text.

Default is a read-only check. --write updates the allowlisted Skill package and the
complete GPT Markdown, never README/docs/changelog or a personal workspace.
Uses only the Python standard library, with no network or model calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
LOCAL_PROJECT = (ROOT / 'outputs/writing-system/writing-companion').is_dir()
CANONICAL = ROOT / ('outputs/writing-system/writing-companion' if LOCAL_PROJECT else 'skills/writing-companion')
PACKAGE = ROOT / ('outputs/writing-companion-github/skills/writing-companion' if LOCAL_PROJECT else 'dist/skills/writing-companion')
GPT = ROOT / ('outputs/writing-companion-chatgpt/写作伴侣-GPT对话完整版.md' if LOCAL_PROJECT else 'gpt/写作伴侣-GPT对话完整版.md')
MANIFEST = 'maintenance/export-manifest.json'
BLANK_ROOT = 'assets/workspace-template/'
BLANK_FILES = {
    'workspace.json', 'catalog.json', 'techniques/retrieval-config.json',
    'techniques/reading-cards.json', 'techniques/index.md', 'state/author.md',
    'state/session.md', 'state/library-policy.md', 'library/.gitkeep',
}
BOUNDARY = re.compile(
    r'<!-- export:gpt:([a-z-]+):start -->\s*\n(.*?)'
    r'\n<!-- export:gpt:\1:end -->', re.S)
ADAPTER = re.compile(
    r'<!-- gpt-adapter:([a-z-]+):start -->\s*\n(.*?)'
    r'\n<!-- gpt-adapter:\1:end -->', re.S)
MACHINE_PATH = re.compile(r'(?i)(?:\b[A-Z]:[\\/]|\\\\[^\s\\]+\\|/(?:Users|home)/)')
GPT_TOOL = re.compile(
    r'(?im)(?:\b(?:python\d*|powershell|cmd(?:\.exe)?)\b|'
    r'--[a-z][\w-]*|\b[\w-]+\.py\b|local-workspace(?:\.json)?|'
    r'library_reader|search_library|work_manager|export:gpt:|gpt-adapter:)')


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(data: bytes):
    return json.loads(data.decode('utf-8-sig'))


def portable_name(name: str) -> bool:
    """Deny data roots, bindings, traversal and executable/link surprises by default."""
    p = PurePosixPath(name)
    if not name or '\\' in name or p.is_absolute() or '..' in p.parts or ':' in name:
        return False
    if p.name == 'local-workspace.json' or p.suffix.lower() in {
        '.db', '.sqlite', '.sqlite3', '.epub', '.pdf', '.mobi', '.azw', '.pyc', '.pyo',
    }:
        return False
    if name.startswith(BLANK_ROOT):
        return name[len(BLANK_ROOT):] in BLANK_FILES
    if p.parts[0] in {'library', 'state', 'works', 'practice', 'techniques', 'private-workspace'}:
        return False
    return name in {'SKILL.md', 'agents/openai.yaml', 'local-workspace.example.json', MANIFEST} or (
        len(p.parts) == 2 and p.parts[0] == 'references' and p.suffix == '.md'
    ) or name in {
        'scripts/library_reader.py', 'scripts/search_library.py', 'scripts/work_manager.py',
    } or (
        len(p.parts) == 3 and p.parts[:2] in {('assets', 'record-templates'), ('assets', 'gpt')}
        and p.suffix == '.md'
    )


def safe_file(base: Path, name: str, *, must_exist=True) -> Path:
    if not portable_name(name):
        raise ValueError(f'Nonportable or private path rejected: {name}')
    base = base.resolve()
    path = base / PurePosixPath(name)
    if not path.resolve().is_relative_to(base):
        raise ValueError(f'Path escapes approved root: {name}')
    current = path
    while current != base:
        # Reject junctions as well as symbolic links, including links that stay inside.
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError(f'Linked export path rejected: {name}')
        current = current.parent
    if must_exist and not path.is_file():
        raise ValueError(f'Missing allowlisted file: {name}')
    return path


def load_source(source: Path):
    manifest = json_bytes(safe_file(source, MANIFEST).read_bytes())
    names = manifest['package_files']
    if manifest.get('schema_version') != 1 or not names or len(names) != len(set(names)):
        raise ValueError('Invalid or duplicate export manifest')
    payload = {name: safe_file(source, name).read_bytes() for name in names}
    for name, data in payload.items():
        text = data.decode('utf-8-sig')
        if MACHINE_PATH.search(text):
            raise ValueError(f'Machine path found in portable source: {name}')
    if MANIFEST not in payload or payload[MANIFEST] != safe_file(source, MANIFEST).read_bytes():
        raise ValueError('Manifest must export itself without a race')
    validate_blank(payload)
    for name, digest in manifest.get('blank_text_sha256', {}).items():
        if name not in {'state/author.md', 'state/session.md', 'state/library-policy.md', 'techniques/index.md'}:
            raise ValueError('Unexpected reviewed blank-text path')
        if sha(payload[BLANK_ROOT + name]) != digest:
            raise ValueError(f'Blank template text changed; review before updating its fingerprint: {name}')
    if len(manifest.get('blank_text_sha256', {})) != 4:
        raise ValueError('Reviewed blank-text fingerprints are required')
    return manifest, payload


def validate_blank(payload):
    expected = {BLANK_ROOT + name for name in BLANK_FILES}
    if not expected.issubset(payload):
        raise ValueError('Incomplete blank workspace template')
    catalog = json_bytes(payload[BLANK_ROOT + 'catalog.json'])
    if set(catalog) != {'schema_version', 'sources', 'works', 'techniques', 'practice'} or any(
        catalog[name] != [] for name in ('sources', 'works', 'techniques', 'practice')
    ):
        raise ValueError('Template catalog contains data or unknown fields')
    cards = json_bytes(payload[BLANK_ROOT + 'techniques/reading-cards.json'])
    if cards != {'schema_version': 1, 'cards': []}:
        raise ValueError('Template contains real reading cards')
    config = json_bytes(payload[BLANK_ROOT + 'techniques/retrieval-config.json'])
    if config.get('guide_source_ids') != [] or config.get('normalization_dictionary'):
        raise ValueError('Template contains personal source IDs or a dictionary binding')
    entry = json_bytes(payload[BLANK_ROOT + 'workspace.json'])
    allowed_entry_keys = {'schema_version', 'system_version', 'name', 'author_profile',
                          'session_state', 'catalog', 'library_policy', 'reading_cards', 'guidance_index'}
    if set(entry) - allowed_entry_keys or entry.get('name') != '我的写作工作室':
        raise ValueError('Template entry is not the reviewed generic workspace')
    for key in ('author_profile', 'session_state', 'catalog', 'library_policy', 'reading_cards', 'guidance_index'):
        if entry.get(key) not in BLANK_FILES:
            raise ValueError(f'Template entry points outside the blank template: {key}')
    for name, required in [('state/author.md', '尚未记录作者的长期偏好'),
                           ('state/session.md', '新工作区，尚无作品和辅导记录'),
                           ('techniques/index.md', '尚无阅读卡')]:
        if required not in payload[BLANK_ROOT + name].decode('utf-8-sig'):
            raise ValueError(f'Template has lost its explicit blank status: {name}')
    example = json_bytes(payload['local-workspace.example.json'])
    if example != {'default_workspace': '/absolute/path/to/my-writing-workspace'}:
        raise ValueError('Machine binding example contains a personal path')
    if payload[BLANK_ROOT + 'library/.gitkeep'].strip():
        raise ValueError('Blank library marker contains unexpected content')


def headings(text: str, extra: int) -> str:
    """Adjust real Markdown headings, preserving headings inside template code fences."""
    lines, fenced = [], False
    for line in text.splitlines():
        if line.startswith('```'):
            fenced = not fenced
        if not fenced:
            line = re.sub(r'^(#{1,6})(?= )', lambda m: '#' * min(6, len(m[1]) + extra), line)
        lines.append(line)
    return '\n'.join(lines)


def clean_links(text: str) -> str:
    # A single generated document cannot follow local Skill file links. Keep the
    # human heading or template name; preserve verified external configuration URLs.
    return re.sub(r'\[([^\]]+)\]\((?!https?://)[^)]+\)', r'“\1”', text)


def source_section(text: str, title: str) -> str:
    start = text.find(title + '\n')
    if start < 0:
        raise ValueError(f'Missing shared source section: {title}')
    end = text.find('\n## ', start + len(title))
    return text[start:end if end >= 0 else None].strip()


def render_gpt(manifest, payload):
    def source(name):
        if name not in payload:
            raise ValueError(f'GPT source is not in export allowlist: {name}')
        return payload[name].decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')

    adapter_text = source('assets/gpt/environment.md')
    matches = list(ADAPTER.finditer(adapter_text))
    adapters = {m[1]: m[2].strip() for m in matches}
    if len(matches) != len(adapters) or 'platform' not in adapters:
        raise ValueError('Invalid or duplicate GPT adapter sections')
    used = {'platform'}
    rendered_sources = ['SKILL.md'] + manifest['gpt_shared_rules']

    def portable(text):
        def adapt(match):
            key = match[1]
            if key not in adapters:
                raise ValueError(f'Unmapped environment boundary: {key}')
            if key in used:
                raise ValueError(f'Duplicate environment boundary: {key}')
            used.add(key)
            return adapters[key]
        result = BOUNDARY.sub(adapt, text)
        if 'export:gpt:' in result:
            raise ValueError('Malformed or nested environment boundary')
        return clean_links(result)

    skill = source('SKILL.md')
    core = source_section(skill, '## 核心约定')
    end_check = source_section(skill, '## 结束检查')
    sections = [
        '# 写作伴侣：GPT 对话完整版',
        f"版本：{manifest['gpt_version']} · 共同规则来源版本："
        f"{json_bytes(payload[BLANK_ROOT + 'workspace.json'])['system_version']} · "
        f"平台说明核验：{manifest['platform_guidance_verified']}\n\n"
        '由同一份写作伴侣核心规则与独立 GPT 环境适配层生成。无需安装 Skill 或运行本地程序。'
        '书籍正文、作者作品与历史阅读卡须另外提供；本文件只含规则、空白模板与原创示例。',
        source('assets/gpt/usage.md').strip(),
        '## GPT 环境适配\n\n' + adapters['platform'],
        '## 共享写作协作规则\n\n'
        '帮助作者完成作品并提高独立写作与判断能力。散文随笔为主，小说为辅；'
        '本次具体要求优先于默认方式。下列模块的业务规则直接来自维护源，'
        '只有明确标记的本地技术段改用上述环境能力。\n\n' + headings(portable(core), 1),
    ]
    for name in manifest['gpt_shared_rules']:
        sections.append(headings(portable(source(name).strip()), 2))
    sections.extend([
        headings(portable(end_check), 1),
        source('assets/gpt/markdown-templates.md').strip(),
        '## 素材、长篇与学习过程的记录表单\n\n按需要选用，不作为开始写作前的问卷。',
    ])
    for name in manifest['gpt_record_templates']:
        sections.append(headings(clean_links(source(name).strip()), 2))
    sections.append(source('assets/gpt/examples.md').strip())
    unused = set(adapters) - used
    if unused:
        raise ValueError('Unused GPT adapters; source boundaries may have drifted: ' + ', '.join(sorted(unused)))
    lines = [
        '## 维护来源与生成边界',
        '共享核心来自下列规则文件。GPT 环境段只适配资料访问、来源定位与保存能力；'
        '本地程序、索引、绑定及私人资料不随文件迁移。修改共享规则后必须重新生成，'
        '来源校验用于发现过期副本，不代替规则正文。',
        '| 共同来源 | SHA-256 |', '|---|---|',
    ]
    for name in rendered_sources:
        lines.append(f'| {name} | {sha(payload[name])} |')
    lines.extend([
        '', '表单与平台适配来自同一维护目录中的空白模板和环境说明，不读取私人工作区。',
        '平台说明依据所标日期核验的官方资料；界面入口与可用功能可能变化，'
        '仅在实际支持时使用，不保证每轮完整读取全部知识附件。',
    ])
    sections.append('\n'.join(lines))
    rendered = '\n\n---\n\n'.join(sections).strip() + '\n'
    validate_gpt(rendered)
    return rendered.encode('utf-8'), {'shared_rule_files': rendered_sources,
                                     'environment_boundaries': sorted(used - {'platform'})}


def validate_gpt(text):
    if MACHINE_PATH.search(text):
        raise ValueError('GPT export contains a machine path')
    match = GPT_TOOL.search(text)
    if match:
        raise ValueError(f'GPT export contains a local-tool dependency or unexpanded marker: {match[0]}')
    if re.search(r'\]\((?:\.\.?/|(?:references|assets|scripts)/)', text):
        raise ValueError('GPT export contains an unresolved local file link')
    # Cover the operational promises most easily lost when changing environment.
    required = ['默认由', '原创', '引用', '必要上下文', '样本观察', '独立运用',
                '撤回', '原稿', '没有文件工具时', '未写入外部存储', '接续包',
                '不能编造哈希', '没有附带书库', '长篇', '素材', '候选']
    for phrase in required:
        if phrase not in text:
            raise ValueError(f'GPT lost a required functional boundary: {phrase}')


def inspect_package(package: Path, names):
    if not package.exists():
        return []
    if package.is_symlink() or (hasattr(package, 'is_junction') and package.is_junction()):
        raise ValueError('Package target must not be a link or installation junction')
    extras = []
    compiled_modules = {PurePosixPath(name).stem for name in names
                        if PurePosixPath(name).parent == PurePosixPath('scripts') and name.endswith('.py')}
    for path in package.rglob('*'):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Linked path found in package target')
        relative = path.relative_to(package)
        generated_cache = (relative.parent.as_posix() == 'scripts/__pycache__' and any(
            re.fullmatch(re.escape(module) + r'\.cpython-\d+(?:\.opt-\d+)?\.pyc', path.name)
            for module in compiled_modules))
        if path.is_file() and relative.as_posix() not in names and not generated_cache:
            extras.append(relative.as_posix())
    if extras:
        raise ValueError('Unallowlisted package files require manual review (nothing removed): ' + ', '.join(extras))
    return extras


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync(*, source=CANONICAL, package=PACKAGE, gpt=GPT, write=False):
    source, package, gpt = source.resolve(), package.absolute(), gpt.absolute()
    if package.resolve().is_relative_to(source) or source.is_relative_to(package.resolve()):
        raise ValueError('Canonical and package roots must be separate')
    if gpt.resolve().is_relative_to(source) or gpt.resolve().is_relative_to(package.resolve()):
        raise ValueError('GPT output must not overwrite a shared source or package file')
    if gpt.is_symlink() or (hasattr(gpt, 'is_junction') and gpt.is_junction()):
        raise ValueError('GPT target must not be a link')
    manifest, payload = load_source(source)
    rendered, mapping = render_gpt(manifest, payload)
    inspect_package(package, set(payload))
    changed = [name for name, data in payload.items()
               if not (package / name).is_file() or (package / name).read_bytes() != data]
    gpt_changed = not gpt.is_file() or gpt.read_bytes() != rendered
    if write:
        # Finish all validations before mutation. Refuse a changing source snapshot.
        for name, data in payload.items():
            if safe_file(source, name).read_bytes() != data:
                raise ValueError(f'Canonical changed during export; retry after editing: {name}')
        for name in changed:
            atomic_write(safe_file(package, name, must_exist=False), payload[name])
        if gpt_changed:
            atomic_write(gpt, rendered)
        for name, data in payload.items():
            if safe_file(package, name).read_bytes() != data:
                raise ValueError(f'Package read-back differs: {name}')
        if gpt.read_bytes() != rendered:
            raise ValueError('GPT read-back differs')
    return {
        'mode': 'write' if write else 'check', 'in_sync': write or not (changed or gpt_changed),
        'package_file_count': len(payload), 'changed_package_files': changed,
        'gpt_changed': gpt_changed, 'gpt_sha256': sha(rendered),
        'gpt_bytes': len(rendered), 'mapping': mapping,
        'package_sha256': {name: sha(data) for name, data in sorted(payload.items())},
        'privacy': {'binding_exported': False, 'real_library_exported': False,
                    'real_state_exported': False, 'real_cards_exported': False,
                    'original_manuscripts_exported': False, 'blank_template_validated': True},
        'checks': {'byte_identical_core_and_assets': True if write else not changed,
                   'gpt_has_no_machine_paths_or_cli_dependency': True,
                   'gpt_includes_honest_no_file_tools_mode': True,
                   'gpt_is_rendered_from_shared_rules': True},
    }


def self_test():
    """Exercise private-data denial, portability validation and deterministic rendering."""
    manifest, payload = load_source(CANONICAL)
    rendered, _ = render_gpt(manifest, payload)
    assert render_gpt(manifest, payload)[0] == rendered
    blocked = ['local-workspace.json', 'library/private.md', 'state/author.md',
               'works/door/original.md', 'techniques/real-card.md', '../state/author.md',
               'assets/workspace-template/library/private.md', '/absolute/secret',
               'assets/gpt/../../state/author.md']
    assert all(not portable_name(name) for name in blocked)
    poisoned = dict(payload)
    poisoned[BLANK_ROOT + 'techniques/reading-cards.json'] = b'{"schema_version":1,"cards":[{"private":true}]}'
    try:
        validate_blank(poisoned)
        raise AssertionError('Private cards accepted')
    except ValueError:
        pass
    for bad in ('\npython scripts/run.py\n', '\nC:/Users/Private/book.md\n', '\n--workspace secret\n'):
        try:
            validate_gpt(rendered.decode('utf-8') + bad)
            raise AssertionError('Local dependency or machine path accepted')
        except ValueError:
            pass
    changed = dict(payload)
    changed['references/teaching.md'] += '\n共享教学更新演示哨兵。\n'.encode('utf-8')
    changed_gpt, _ = render_gpt(manifest, changed)
    assert '共享教学更新演示哨兵。' in changed_gpt.decode('utf-8')
    assert changed_gpt != rendered
    with tempfile.TemporaryDirectory(prefix='writing-export-check-') as temporary:
        fixture = Path(temporary) / 'canonical'
        package = Path(temporary) / 'package'
        gpt = Path(temporary) / 'complete.md'
        for name, data in payload.items():
            path = fixture / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        sentinel = 'PRIVATE_SENTINEL_DO_NOT_EXPORT_83A5'
        for name in ['local-workspace.json', 'state/author.md', 'library/real.md',
                     'works/private/original.md', 'techniques/real-card.md', 'assets/gpt/unlisted.md']:
            path = fixture / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sentinel, encoding='utf-8')
        result = sync(source=fixture, package=package, gpt=gpt, write=True)
        assert result['in_sync']
        assert sync(source=fixture, package=package, gpt=gpt)['in_sync']
        for path in [gpt] + [p for p in package.rglob('*') if p.is_file()]:
            assert sentinel.encode('utf-8') not in path.read_bytes(), 'Private sentinel was exported'
        assert len([p for p in package.rglob('*') if p.is_file()]) == len(payload)
        cache = package / 'scripts/__pycache__/work_manager.cpython-312.pyc'
        cache.parent.mkdir()
        cache.write_bytes(b'generated test cache, never exported')
        assert inspect_package(package, set(payload)) == []
        target_rule = package / 'references/teaching.md'
        target_rule.write_bytes(target_rule.read_bytes() + b'\nDRIFT_SENTINEL\n')
        assert not sync(source=fixture, package=package, gpt=gpt)['in_sync']
        sync(source=fixture, package=package, gpt=gpt, write=True)
        (package / 'local-workspace.json').write_text('{"private":true}', encoding='utf-8')
        try:
            inspect_package(package, set(payload))
            raise AssertionError('Private target binding accepted')
        except ValueError:
            pass
        assert (package / 'local-workspace.json').exists(), 'Audit must not delete private files'
    return {'self_test': 'passed', 'checks': ['deterministic_render', 'private_roots_denied',
            'real_cards_rejected', 'machine_paths_rejected', 'cli_dependencies_rejected',
            'shared_rule_change_propagates_into_gpt_body', 'full_export_excludes_private_sentinels',
            'export_then_check_is_in_sync', 'detects_and_repairs_package_drift',
            'target_private_binding_blocks_without_deletion', 'generated_python_cache_does_not_block_sync']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--write', action='store_true', help='Export reviewed files; preserve unrelated publication docs')
    mode.add_argument('--check', action='store_true', help='Read-only synchronization and privacy check (default)')
    mode.add_argument('--self-test', action='store_true', help='Run isolated safety and shared-source propagation checks')
    parser.add_argument('--report', type=Path, help='Optional JSON report inside this project workspace')
    args = parser.parse_args()
    try:
        report = self_test() if args.self_test else sync(write=args.write)
        if args.report:
            report_path = args.report.resolve()
            if not report_path.is_relative_to(ROOT / 'work') or report_path.suffix != '.json':
                raise ValueError('Report must be a JSON file inside this project work directory')
            atomic_write(report_path, (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get('in_sync', True) else 1
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({'error': str(error), 'writes_authorized': args.write}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
