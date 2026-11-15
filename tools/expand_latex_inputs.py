#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flatten a LaTeX collection (a main document plus its local ``\\input`` /
``\\include`` files) into one indexable document with full provenance.

Resolution rules, deliberately simple and explicit:

* A relative path is interpreted relative to the directory of the file that
  contains the referencing directive (the referrer).
* The authorization root is given by ``--root``; a candidate whose canonical
  path (``os.path.realpath``, so symlinks are resolved first) is not inside the
  root is never read.  The failed reference is recorded as a warning and a
  placeholder instead.
* A bare name is completed with ``.tex`` then ``.latex`` when the exact name
  does not exist, mirroring ``pylatexenc.latex2text._inputlatexfile``.
* A file already on the current expansion chain is a circular reference: the
  recursion stops there with a cycle marker.  The same file reached through a
  different chain (a shared sub-file included twice) is expanded again, so
  shared files may appear more than once; only cycles abort.

A main document may be a plain ``.tex`` file or a Python source file.  Python
mains are scanned with :mod:`tokenize`; every string literal that carries an
``\\input`` / ``\\include`` directive is extracted as a main block, keeping its
exact line range in the source file.

Outputs (in ``--output-dir``):

* ``flattened.md``   - the expanded, human- and index-friendly document;
* ``expansion_map.json`` - machine-readable fragments, edges and warnings;
* ``warnings.log``   - one line per outside-root / missing / cycle event.
"""

import argparse
import ast
import json
import os
import re
import sys
import tokenize


DIRECTIVE_RE = re.compile(
    r'\\(input|include)(?![A-Za-z])\s*(?:\{([^{}]*)\}|(\S+))'
)

TEX_EXTENSIONS = ('.tex', '.latex')


class WarningSink:
    def __init__(self):
        self.entries = []

    def add(self, kind, message, **details):
        entry = {'kind': kind, 'message': message}
        entry.update(details)
        self.entries.append(entry)
        return entry


def extract_main_blocks(main_path, warnings):
    """Return the LaTeX blocks a main file contributes.

    A ``.tex`` (or other non-Python) main is one block covering the whole file.
    A ``.py`` main yields one block per string literal that contains an
    ``\\input`` / ``\\include`` directive, each tagged with its source lines.
    """
    with open(main_path, 'rb') as handle:
        if main_path.endswith('.py'):
            blocks = []
            for tok in tokenize.tokenize(handle.readline):
                if tok.type != tokenize.STRING:
                    continue
                try:
                    value = ast.literal_eval(tok.string)
                except (ValueError, SyntaxError):
                    continue
                if not isinstance(value, str) or not DIRECTIVE_RE.search(value):
                    continue
                blocks.append({
                    'text': value,
                    'start_line': tok.start[0],
                    'end_line': tok.end[0],
                })
            if not blocks:
                warnings.add(
                    'empty-main',
                    'no string literal with an \\input/\\include directive found',
                    referrer=main_path,
                )
            return blocks
        handle.seek(0)
        text = handle.read().decode('utf-8')
    return [{'text': text, 'start_line': 1,
             'end_line': text.count('\n') + 1}]


def resolve_target(referrer_real, target, root_real):
    """Resolve ``target`` relative to the referrer's directory.

    Returns ``(status, real_path)`` where status is ``ok``, ``outside`` or
    ``missing``.  Extension completion mirrors the library's
    ``read_latex_file``: exact name, then ``.tex``, then ``.latex``.
    """
    referrer_dir = os.path.dirname(referrer_real)
    candidate = os.path.realpath(os.path.join(referrer_dir, target))
    inside = candidate == root_real or candidate.startswith(
        root_real + os.sep
    )
    if not inside:
        return 'outside', candidate
    for option in (candidate, candidate + TEX_EXTENSIONS[0],
                   candidate + TEX_EXTENSIONS[1]):
        if os.path.isfile(option):
            return 'ok', os.path.realpath(option)
    return 'missing', candidate


class Expander:
    def __init__(self, root_real, warnings):
        self.root_real = root_real
        self.warnings = warnings
        self.fragments = []
        self.edges = []
        self._counter = 0

    def _new_fragment_id(self):
        self._counter += 1
        return 'f{0}'.format(self._counter)

    def expand_block(self, text, referrer_real, referrer_loc, chain,
                     parent_id=None, directive=None, raw_target=None):
        """Expand one text block and register a fragment for it."""
        fragment_id = self._new_fragment_id()
        fragment = {
            'id': fragment_id,
            'parent_id': parent_id,
            'role': 'main' if parent_id is None else 'included',
            'path': os.path.relpath(referrer_real, self.root_real),
            'line_range': referrer_loc,
            'directive': directive,
            'raw_target': raw_target,
            'chain': [os.path.relpath(p, self.root_real) for p in chain],
            'segments': [],
        }
        self.fragments.append(fragment)

        position = 0
        line_cursor = referrer_loc[0]
        for match in DIRECTIVE_RE.finditer(text):
            plain = text[position:match.start()]
            if plain:
                fragment['segments'].append({
                    'kind': 'text',
                    'text': plain,
                    'line_range': [line_cursor,
                                   line_cursor + plain.count('\n')],
                })
                line_cursor += plain.count('\n')
            target = (match.group(2) or match.group(3) or '').strip()
            directive_line = line_cursor
            fragment['segments'].append(
                self._expand_directive(
                    directive=match.group(0),
                    macro=match.group(1),
                    target=target,
                    referrer_real=referrer_real,
                    directive_line=directive_line,
                    chain=chain,
                    parent_id=fragment_id,
                )
            )
            line_cursor += match.group(0).count('\n')
            position = match.end()

        tail = text[position:]
        if tail:
            fragment['segments'].append({
                'kind': 'text',
                'text': tail,
                'line_range': [line_cursor,
                               line_cursor + tail.count('\n')],
            })
        return fragment

    def _expand_directive(self, directive, macro, target, referrer_real,
                          directive_line, chain, parent_id):
        referrer_rel = os.path.relpath(referrer_real, self.root_real)
        status, resolved = resolve_target(
            referrer_real, target, self.root_real
        )
        edge = {
            'from': referrer_rel,
            'from_line': directive_line,
            'directive': directive,
            'raw_target': target,
            'status': status,
            'resolved': os.path.relpath(resolved, self.root_real)
            if resolved.startswith(self.root_real) else resolved,
        }
        self.edges.append(edge)

        if status == 'outside':
            self.warnings.add(
                'outside-root',
                "reference '{0}' resolves outside the authorization root; "
                "not read".format(target),
                referrer=referrer_rel, line=directive_line,
                resolved=resolved,
            )
            return {'kind': 'placeholder', 'reason': 'outside-root',
                    'edge': edge}

        if status == 'missing':
            self.warnings.add(
                'missing-file',
                "reference '{0}' does not resolve to a readable file"
                .format(target),
                referrer=referrer_rel, line=directive_line,
                resolved=edge['resolved'],
            )
            return {'kind': 'placeholder', 'reason': 'missing-file',
                    'edge': edge}

        if resolved in chain:
            chain_rel = [os.path.relpath(p, self.root_real) for p in chain]
            self.warnings.add(
                'cycle',
                "circular reference to '{0}' aborted".format(target),
                referrer=referrer_rel, line=directive_line,
                resolved=edge['resolved'], chain=chain_rel,
            )
            return {'kind': 'placeholder', 'reason': 'cycle', 'edge': edge}

        with open(resolved, encoding='utf-8') as handle:
            included_text = handle.read()
        total_lines = included_text.count('\n') + 1
        child = self.expand_block(
            included_text,
            referrer_real=resolved,
            referrer_loc=[1, total_lines],
            chain=chain + [resolved],
            parent_id=parent_id,
            directive=directive,
            raw_target=target,
        )
        return {'kind': 'include', 'fragment_id': child['id'], 'edge': edge}


def render_markdown(root_real, main_paths, expander, warnings):
    lines = []
    lines.append('# 采集包展开正文（可索引）')
    lines.append('')
    lines.append('## 元信息与规则')
    lines.append('')
    lines.append('- 授权根（canonical）：`{0}`'.format(root_real))
    lines.append('- 主文文件：{0}'.format(
        ', '.join('`{0}`'.format(p) for p in main_paths)))
    lines.append('- 引用指令：`\\input{...}` / `\\include{...}`（含无花括号'
                 '的原始形式 `\\input file.tex`）')
    lines.append('- 相对路径按引用者所在目录解释；精确名不存在时依次补 '
                 '`.tex`、`.latex`')
    lines.append('- 解析路径经 `realpath` 规范化后必须位于授权根内，越界不读取')
    lines.append('- 同一展开链上重复进入同一文件即判定循环引用并中止；'
                 '不同链上的共用子文件允许重复展开')
    lines.append('- 行号约定：每个片段标注源文件行区间，片段内第 k 行对应'
                 '源文件第（起始行 + k - 1）行')
    lines.append('')

    lines.append('## 片段索引')
    lines.append('')
    lines.append('| 片段 | 角色 | 源文件 | 行区间 | 父片段 | 引用指令 |')
    lines.append('| --- | --- | --- | --- | --- | --- |')
    for fragment in expander.fragments:
        lines.append('| {id} | {role} | `{path}` | {a}–{b} | {parent} | '
                     '{directive} |'.format(
                         id=fragment['id'],
                         role=fragment['role'],
                         path=fragment['path'],
                         a=fragment['line_range'][0],
                         b=fragment['line_range'][1],
                         parent=fragment['parent_id'] or '—',
                         directive='`{0}`'.format(fragment['directive'])
                         if fragment['directive'] else '—'))
    lines.append('')

    lines.append('## 展开正文')
    lines.append('')
    root_fragments = [f for f in expander.fragments
                      if f['parent_id'] is None]
    for fragment in root_fragments:
        _render_fragment(fragment, expander, lines, heading_level=3)
        lines.append('')

    lines.append('## 引用边（解析结果）')
    lines.append('')
    lines.append('| 引用者 | 行 | 指令 | 原始目标 | 状态 | 解析结果 |')
    lines.append('| --- | --- | --- | --- | --- | --- |')
    for edge in expander.edges:
        lines.append('| `{frm}` | {line} | `{dir}` | `{target}` | {status} '
                     '| `{resolved}` |'.format(
                         frm=edge['from'], line=edge['from_line'],
                         dir=edge['directive'], target=edge['raw_target'],
                         status=edge['status'], resolved=edge['resolved']))
    lines.append('')

    lines.append('## 告警（越界 / 缺失 / 循环）')
    lines.append('')
    if not warnings.entries:
        lines.append('- 无')
    else:
        for entry in warnings.entries:
            lines.append('- **{0}**：{1}（`{2}:{3}`）'.format(
                entry['kind'], entry['message'],
                entry.get('referrer', '?'), entry.get('line', '?')))
    lines.append('')
    return '\n'.join(lines)


def _render_fragment(fragment, expander, lines, heading_level):
    hashes = '#' * heading_level
    title = '{0} 片段 {1} · `{2}`（第 {3}–{4} 行）'.format(
        hashes, fragment['id'], fragment['path'],
        fragment['line_range'][0], fragment['line_range'][1])
    lines.append(title)
    lines.append('')
    chain = ' > '.join('`{0}`'.format(p) for p in fragment['chain'])
    lines.append('- 角色：{0}'.format(fragment['role']))
    lines.append('- 展开链：{0}'.format(chain))
    if fragment['directive']:
        lines.append('- 经由指令：`{0}`（目标 `{1}`）'.format(
            fragment['directive'], fragment['raw_target']))
    lines.append('')
    for segment in fragment['segments']:
        if segment['kind'] == 'text':
            lines.append('源行 {0}–{1}：'.format(
                segment['line_range'][0], segment['line_range'][1]))
            lines.append('')
            lines.append('```latex')
            lines.append(segment['text'].rstrip('\n'))
            lines.append('```')
            lines.append('')
        elif segment['kind'] == 'include':
            child = next(f for f in expander.fragments
                         if f['id'] == segment['fragment_id'])
            _render_fragment(child, expander, lines, heading_level + 1)
            lines.append('')
        else:
            edge = segment['edge']
            lines.append('> **未展开占位（{0}）**：`{1}` → 解析为 '
                         '`{2}`，状态 `{0}`。'.format(
                             segment['reason'], edge['directive'],
                             edge['resolved'], edge['status']))
            lines.append('')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.
                                     RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.',
                        help='authorization root; references escaping it are '
                             'not read (default: current directory)')
    parser.add_argument('--main', action='append', required=True,
                        help='main document path (.tex or .py); repeatable')
    parser.add_argument('--output-dir', default='flatten_output',
                        help='where to write the flattened document '
                             '(default: flatten_output)')
    args = parser.parse_args(argv)

    root_real = os.path.realpath(args.root)
    warnings = WarningSink()
    expander = Expander(root_real, warnings)

    main_paths = []
    for main_arg in args.main:
        main_real = os.path.realpath(main_arg)
        if not (main_real == root_real
                or main_real.startswith(root_real + os.sep)):
            warnings.add('outside-root',
                         'main document is outside the authorization root; '
                         'skipped', referrer=main_arg)
            continue
        main_rel = os.path.relpath(main_real, root_real)
        main_paths.append(main_rel)
        for block in extract_main_blocks(main_real, warnings):
            expander.expand_block(
                text=block['text'],
                referrer_real=main_real,
                referrer_loc=[block['start_line'], block['end_line']],
                chain=[main_real],
            )

    os.makedirs(args.output_dir, exist_ok=True)
    markdown = render_markdown(root_real, main_paths, expander, warnings)
    md_path = os.path.join(args.output_dir, 'flattened.md')
    with open(md_path, 'w', encoding='utf-8') as handle:
        handle.write(markdown)

    map_path = os.path.join(args.output_dir, 'expansion_map.json')
    with open(map_path, 'w', encoding='utf-8') as handle:
        json.dump({
            'root': root_real,
            'mains': main_paths,
            'resolution': {
                'relative_to': 'directory of the referrer',
                'extensions': ['', '.tex', '.latex'],
                'root_boundary': 'realpath must stay within root',
                'cycle_policy': 'abort only when a file repeats within the '
                                'same expansion chain',
            },
            'fragments': expander.fragments,
            'edges': expander.edges,
            'warnings': warnings.entries,
        }, handle, ensure_ascii=False, indent=2)

    log_path = os.path.join(args.output_dir, 'warnings.log')
    with open(log_path, 'w', encoding='utf-8') as handle:
        for entry in warnings.entries:
            handle.write('{kind}: {message} [{details}]\n'.format(
                kind=entry['kind'], message=entry['message'],
                details=json.dumps(
                    {k: v for k, v in entry.items()
                     if k not in ('kind', 'message')},
                    ensure_ascii=False)))

    print('wrote {0} ({1} fragments, {2} edges, {3} warnings)'.format(
        md_path, len(expander.fragments), len(expander.edges),
        len(warnings.entries)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
