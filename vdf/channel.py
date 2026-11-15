"""
Resolution of channel configuration VDF files with conditional keys.

Channel configuration files use Valve KeyValues conditions (``[...]``) to
guard platform specific values, and often repeat keys (e.g. ``installdir``)
once per platform. ``vdf.parse`` silently drops ``[...]`` conditions and,
with the default ``dict`` mapper, collapses duplicate keys (last one wins),
which makes it unsuitable for such files. This module parses them faithfully
and resolves them against a local condition environment, producing a
JSON-able manifest in which every effective value is annotated with its
source (line number and condition).

Condition syntax convention
---------------------------

- A condition is a boolean expression in square brackets, placed on the same
  line after a key-value pair or a section key::

      "installdir"  "win/path"    [$WINDOWS && $64BIT]
      "mac"         [$OSX]
      {
          "installdir"  "mac/path"
      }

  Conditions on sections apply to every entry inside the section.
- Grammar: ``$NAME`` (the ``$`` is optional, names are case-insensitive),
  ``!`` (not), ``&&`` (and), ``||`` (or) and parentheses.
- Evaluation is three-valued: a symbol that is missing from the environment
  is *unknown*, not false. Unknowns propagate through ``!``/``&&``/``||``
  using Kleene logic.

Resolution rules
----------------

- Entries whose condition chain evaluates to false are excluded.
- Entries with unknown conditions are kept as *candidates* in the manifest;
  they are never treated as true.
- Unconditional entries form the base tier; entries with a true condition
  form the override tier, which takes precedence.
- If several entries of the same tier are effective but have different
  values, the key is reported as a *conflict* and left unresolved. The
  resolver never silently picks the last value in file order.
"""

import argparse
import json
import re
import struct
import sys
from collections import OrderedDict, namedtuple

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

from vdf import _unescape, strip_bom, string_type


class ConditionSyntaxError(ValueError):
    """Raised when a condition expression cannot be parsed"""


class ConflictError(ValueError):
    """Raised in strict mode when same-tier effective entries disagree"""

    def __init__(self, conflicts):
        self.conflicts = conflicts
        super(ConflictError, self).__init__(
            "conflicting values for: %s" % ", ".join(sorted(conflicts)))


class Entry(namedtuple('Entry', ('path', 'key', 'value', 'conditions', 'lineno'))):
    """
    A single key-value entry from a channel configuration file.

    ``path`` is a tuple of enclosing section names, ``conditions`` is a tuple
    of raw condition expressions (with brackets, outermost first) inherited
    from enclosing sections plus the entry's own, and ``lineno`` is the line
    on which the entry starts.
    """

    @property
    def full_path(self):
        return self.path + (self.key,)


# ---------------------------------------------------------------------------
# condition expressions
# ---------------------------------------------------------------------------

_re_cond_token = re.compile(r"""
    [ \t]*(?:
        (?P<not>!)
      | (?P<and>&&)
      | (?P<or>\|\|)
      | (?P<lpar>\()
      | (?P<rpar>\))
      | (?P<sym>\$?[A-Za-z0-9_]+)
    )
""", re.VERBOSE)


def _tokenize_condition(text):
    tokens = []
    pos = 0

    while pos < len(text):
        match = _re_cond_token.match(text, pos)
        if not match:
            raise ConditionSyntaxError(
                "invalid token in condition %r at offset %d" % (text, pos))
        pos = match.end()

        for kind in ('not', 'and', 'or', 'lpar', 'rpar'):
            if match.group(kind):
                tokens.append((kind,))
                break
        else:
            tokens.append(('sym', match.group('sym').lstrip('$').upper()))

    return tokens


def _parse_or(tokens, pos):
    node, pos = _parse_and(tokens, pos)
    while pos < len(tokens) and tokens[pos] == ('or',):
        rhs, pos = _parse_and(tokens, pos + 1)
        node = ('or', node, rhs)
    return node, pos


def _parse_and(tokens, pos):
    node, pos = _parse_unary(tokens, pos)
    while pos < len(tokens) and tokens[pos] == ('and',):
        rhs, pos = _parse_unary(tokens, pos + 1)
        node = ('and', node, rhs)
    return node, pos


def _parse_unary(tokens, pos):
    if pos >= len(tokens):
        raise ConditionSyntaxError("unexpected end of condition")

    token = tokens[pos]

    if token == ('not',):
        node, pos = _parse_unary(tokens, pos + 1)
        return ('not', node), pos

    if token == ('lpar',):
        node, pos = _parse_or(tokens, pos + 1)
        if pos >= len(tokens) or tokens[pos] != ('rpar',):
            raise ConditionSyntaxError("missing closing parenthesis")
        return node, pos + 1

    if token[0] == 'sym':
        return token, pos + 1

    raise ConditionSyntaxError("unexpected token %r in condition" % (token,))


def _parse_condition(text):
    tokens = _tokenize_condition(text.strip())

    if not tokens:
        raise ConditionSyntaxError("empty condition")

    node, pos = _parse_or(tokens, 0)

    if pos != len(tokens):
        raise ConditionSyntaxError("unexpected trailing tokens in condition %r" % text)

    return node


def _eval_ast(node, env):
    kind = node[0]

    if kind == 'sym':
        value = env.get(node[1])
        return None if value is None else bool(value)

    if kind == 'not':
        value = _eval_ast(node[1], env)
        return None if value is None else not value

    left = _eval_ast(node[1], env)
    right = _eval_ast(node[2], env)

    if kind == 'and':
        if left is False or right is False:
            return False
        if left is None or right is None:
            return None
        return True

    # kind == 'or'
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


_BOOL_STRINGS = {
    '1': True, 'true': True, 'yes': True, 'on': True,
    '0': False, 'false': False, 'no': False, 'off': False,
}


def normalize_env(env):
    """
    Normalize a condition environment: strip leading ``$`` from symbol names,
    uppercase them, and coerce values to bool. String values must look like
    a boolean (``1/0``, ``true/false``, ``yes/no``, ``on/off``).
    """
    normalized = {}

    for name, value in (env or {}).items():
        name = name.lstrip('$').upper()

        if isinstance(value, string_type):
            try:
                value = _BOOL_STRINGS[value.lower()]
            except KeyError:
                raise ValueError("cannot interpret %r as a boolean for symbol %s"
                                 % (value, name))

        normalized[name] = bool(value)

    return normalized


def evaluate_condition(condition, env):
    """
    Evaluate a condition expression against ``env``.

    ``condition`` may be given with or without the surrounding brackets.
    Returns ``True``, ``False``, or ``None`` when the expression depends on
    symbols that are not present in the environment (unknown, not false).
    """
    text = condition.strip()

    if text.startswith('[') and text.endswith(']'):
        text = text[1:-1]

    return _eval_ast(_parse_condition(text), normalize_env(env))


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

_re_keyvalue = re.compile(r"""
    ^[ \t]*
    (?:"(?P<qkey>(?:\\.|[^\\"])*)"|(?P<key>[^\s"{}\[\]/][^\s"{}\[\]]*))
    (?:[ \t]+(?:"(?P<qval>(?:\\.|[^\\"])*)"|(?P<val>[^\s"{}\[\]]+)))?
    [ \t]*(?:\[(?P<cond>[^\[\]]*)\])?
    [ \t]*(?P<obrace>{)?
    [ \t]*(?://.*)?$
""", re.VERBOSE)


def parse_channel(fp, escaped=True):
    """
    Parse a channel configuration file into a list of ``Entry``.

    ``fp`` may be a string or a file-like object. Unlike ``vdf.parse`` this
    preserves duplicate keys, keeps ``[...]`` conditions and records the line
    number of every entry.
    """
    if isinstance(fp, string_type):
        fp = StringIO(fp)
    if not hasattr(fp, 'readline'):
        raise TypeError("Expected fp to be a str or a file-like object, got %s"
                        % type(fp))

    entries = []
    stack = []  # (section key, condition or None)
    expect_bracket = False

    lines = enumerate(fp, 1)

    for lineno, line in lines:
        if lineno == 1:
            line = strip_bom(line)

        line = line.lstrip()

        # skip empty and comment lines
        if line == "" or line[0] == '/':
            continue

        # one level deeper
        if line[0] == "{":
            expect_bracket = False
            continue

        if expect_bracket:
            raise SyntaxError("vdf.channel.parse_channel: expected opening bracket",
                              (getattr(fp, 'name', '<channel>'), lineno, 1, line))

        # one level back
        if line[0] == "}":
            if stack:
                stack.pop()
                continue

            raise SyntaxError("vdf.channel.parse_channel: one too many closing parenthesis",
                              (getattr(fp, 'name', '<channel>'), lineno, 0, line))

        # parse keyvalue pairs; a quoted key or value may span multiple lines
        while True:
            match = _re_keyvalue.match(line)

            if match:
                break

            try:
                line += next(lines)[1]
            except StopIteration:
                raise SyntaxError("vdf.channel.parse_channel: unexpected EOF (open quote?)",
                                  (getattr(fp, 'name', '<channel>'), lineno, 0, line))

        key = match.group('key') if match.group('qkey') is None else match.group('qkey')
        value = match.group('qval') if match.group('val') is None else match.group('val')
        condition = match.group('cond')

        if escaped:
            key = _unescape(key)
            if value is not None:
                value = _unescape(value)

        conditions = tuple(cond for _, cond in stack if cond is not None)
        if condition is not None:
            conditions += ('[%s]' % condition,)

        if value is None:
            # section key, descend one level deeper
            stack.append((key, '[%s]' % condition if condition is not None else None))

            if match.group('obrace') is None:
                expect_bracket = True
        else:
            entries.append(Entry(
                path=tuple(k for k, _ in stack),
                key=key,
                value=value,
                conditions=conditions,
                lineno=lineno,
            ))

    if stack:
        raise SyntaxError("vdf.channel.parse_channel: unclosed parenthesis or quotes (EOF)",
                          (getattr(fp, 'name', '<channel>'), lineno, 0, ''))

    return entries


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

def _entry_status(entry, env):
    """True (effective), False (excluded) or None (unknown) for an entry"""
    unknown = False

    for condition in entry.conditions:
        result = _eval_ast(_parse_condition(condition[1:-1]), env)

        if result is False:
            return False
        if result is None:
            unknown = True

    return None if unknown else True


def _entry_tier(entry):
    """Conditional entries override unconditional ones"""
    return 1 if entry.conditions else 0


def _entry_source(entry):
    return OrderedDict((
        ('line', entry.lineno),
        ('condition', ' && '.join(entry.conditions) if entry.conditions else None),
    ))


def _entry_choice(entry):
    return OrderedDict((
        ('value', entry.value),
        ('line', entry.lineno),
        ('condition', ' && '.join(entry.conditions) if entry.conditions else None),
    ))


def _path_record(path, entry):
    return OrderedDict((
        ('path', path),
        ('value', entry.value),
        ('line', entry.lineno),
        ('condition', ' && '.join(entry.conditions) if entry.conditions else None),
    ))


def resolve(entries, env=None, strict=False):
    """
    Resolve parsed ``Entry`` objects against the condition environment ``env``
    and return a manifest as an ``OrderedDict`` with the keys:

    - ``environment``: the normalized environment that was used
    - ``resolved``: ``{path: {"value": ..., "sources": [{"line", "condition"}]}}``
    - ``conflicts``: ``{path: {"choices": [{"value", "line", "condition"}]}}``
      for keys where same-tier effective entries disagree; these keys are
      deliberately left out of ``resolved`` (never last-one-wins)
    - ``candidates``: entries whose condition is unknown in ``env``; kept for
      review, never treated as true
    - ``excluded``: entries whose condition evaluated to false

    When ``strict`` is true a ``ConflictError`` is raised instead of
    returning a manifest containing conflicts.
    """
    env = normalize_env(env)

    groups = OrderedDict()
    for entry in entries:
        groups.setdefault('.'.join(entry.full_path), []).append(entry)

    resolved = OrderedDict()
    conflicts = OrderedDict()
    candidates = []
    excluded = []

    for path, group in groups.items():
        effective = []

        for entry in group:
            status = _entry_status(entry, env)

            if status is True:
                effective.append(entry)
            elif status is None:
                candidates.append(_path_record(path, entry))
            else:
                excluded.append(_path_record(path, entry))

        if not effective:
            continue

        top_tier = max(_entry_tier(entry) for entry in effective)
        top = [entry for entry in effective if _entry_tier(entry) == top_tier]

        values = []
        for entry in top:
            if entry.value not in values:
                values.append(entry.value)

        if len(values) == 1:
            resolved[path] = OrderedDict((
                ('value', values[0]),
                ('sources', [_entry_source(entry) for entry in top]),
            ))
        else:
            conflicts[path] = OrderedDict((
                ('choices', [_entry_choice(entry) for entry in top]),
            ))

    if strict and conflicts:
        raise ConflictError(conflicts)

    return OrderedDict((
        ('environment', OrderedDict(sorted(env.items()))),
        ('resolved', resolved),
        ('conflicts', conflicts),
        ('candidates', candidates),
        ('excluded', excluded),
    ))


def build_manifest(text, env=None, strict=False):
    """Parse ``text`` and resolve it against ``env`` in one step"""
    return resolve(parse_channel(text), env=env, strict=strict)


# ---------------------------------------------------------------------------
# local environment
# ---------------------------------------------------------------------------

def local_environment():
    """
    Derive a condition environment from the local machine.

    Defines the platform symbols ``WINDOWS``/``WIN32``/``WIN64``, ``LINUX``/
    ``LINUX64``, ``OSX``/``OSX64``, ``POSIX`` and ``64BIT``. Symbols that
    cannot be determined locally (e.g. ``STEAMDECK``, ``ARMV7``) are left
    undefined, so conditions using them evaluate to unknown and their entries
    are kept as candidates.
    """
    env = {
        'WINDOWS': False, 'WIN32': False, 'WIN64': False,
        'LINUX': False, 'LINUX64': False,
        'OSX': False, 'OSX64': False,
        'POSIX': False,
    }

    is_64bit = struct.calcsize("P") == 8 or sys.maxsize > 2 ** 32

    if sys.platform.startswith('win'):
        env['WINDOWS'] = env['WIN32'] = True
        env['WIN64'] = is_64bit
    elif sys.platform.startswith('linux'):
        env['LINUX'] = env['POSIX'] = True
        env['LINUX64'] = is_64bit
    elif sys.platform == 'darwin':
        env['OSX'] = env['POSIX'] = True
        env['OSX64'] = is_64bit

    env['64BIT'] = is_64bit

    return env


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='vdf-channel',
        description="Resolve a channel configuration VDF with platform "
                    "conditions into a JSON manifest")

    parser.add_argument('infile', nargs='?', type=argparse.FileType('r'),
                        default=sys.stdin, help="channel config VDF (default: stdin)")
    parser.add_argument('outfile', nargs='?', type=argparse.FileType('w'),
                        default=sys.stdout, help="manifest JSON (default: stdout)")
    parser.add_argument('--env', action='append', default=[], metavar='NAME[=BOOL]',
                        help="define a condition symbol; repeatable. "
                             "Bare NAME means true, e.g. --env WINDOWS --env STEAMDECK=0")
    parser.add_argument('--local-env', action='store_true',
                        help="seed the environment from the local machine")
    parser.add_argument('--strict', action='store_true',
                        help="fail (exit 2, no manifest) when conflicts are detected")
    parser.add_argument('-p', '--pretty', action='store_true',
                        help="pretty-print the manifest JSON")

    args = parser.parse_args(argv)

    env = {}
    if args.local_env:
        env.update(local_environment())

    for item in args.env:
        name, _, value = item.partition('=')
        try:
            env[name] = _BOOL_STRINGS[value.lower()] if value else True
        except KeyError:
            parser.error("invalid boolean in --env %r" % item)

    try:
        manifest = build_manifest(args.infile.read(), env=env, strict=args.strict)
    except ConflictError as error:
        sys.stderr.write("vdf-channel: %s\n" % error)
        return 2

    json.dump(manifest, args.outfile,
              indent=2 if args.pretty else None, ensure_ascii=False)
    args.outfile.write('\n')

    if manifest['conflicts']:
        sys.stderr.write("vdf-channel: unresolved conflicts for: %s\n"
                         % ", ".join(sorted(manifest['conflicts'])))
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
