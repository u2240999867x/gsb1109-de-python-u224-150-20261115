r"""
Estimate the fully expanded character count of a piece of LaTeX-like text
under a set of text-substitution macro definitions.

The macros supported here are pure text substitutions, in the spirit of
``\newcommand`` definitions that only rearrange text and parameters:

* A definition occupies one line and looks like::

      \name[n]{replacement body}

  ``\name`` is a backslash followed by letters.  ``[n]`` is optional; when
  present, ``n`` is a digit 1..9 giving the number of parameters (zero when
  absent).  Braces in the body must be balanced.
* In a body, ``#1`` ... ``#9`` refer to the corresponding call argument.
  A parameter contributes its argument text every time it appears: an
  argument referenced twice is counted twice, an argument never referenced
  contributes nothing.
* Braces are pure grouping syntax.  They delimit arguments and groups but do
  not themselves contribute characters to the expansion.
* Macro calls in the text may use braced arguments (``\m{ab}{c}``) or
  single-token undelimited arguments (``\m ab`` for a two-argument macro).
* A macro that has no definition is *unsupported*.  It is never expanded or
  dropped: its control word (backslash plus name) is kept verbatim, counted as
  literal characters, and reported separately.  Braced groups following it are
  ordinary groups and are processed independently, since the macro's arity is
  unknown.
* Definitions may refer to one another, including forward references.
* Recursion is never unrolled to a fixed depth and no fabricated decimal is
  ever printed for it.  The macro call graph (which names a body invokes) is
  analyzed first: every cycle reachable from the analyzed text is reported as
  an ordered list of macro names, and such inputs have no finite character
  count.  When the reachable graph is acyclic the expansion is guaranteed to
  terminate and its count is exact; macro calls supplied through parameters
  are fixed call-site data and reset the recursion guard when substituted, so
  ``\a{\a{x}}`` is finite nesting rather than recursion.  Termination of
  arbitrary substitution systems with parameter forwarding is undecidable in
  general, so an evaluation cap additionally guards the rare acyclic-graph
  case that cannot be exhausted within budget; it too yields ``None`` rather
  than a guessed total, together with a ``truncated`` marker.

Input is a single string split into a definition section and a text section.
Definition lines come first; blank lines and lines starting with ``#`` are
ignored there.  The first line that does not start with a backslash ends the
definition section; a line consisting solely of ``---`` is accepted as an
explicit separator and is not part of the text.  Every remaining line is the
text to analyze.

Example::

    \greet[1]{Hello #1!}
    \twice[1]{#1 #1}
    ---
    \twice{\greet{world}}

The expansion is ``Hello world! Hello world!`` (25 characters); the argument
of ``\twice`` is substituted twice, so ``\greet`` is expanded twice as well.
"""


class MacroSyntaxError(ValueError):
    """Raised when a definition or the text cannot be parsed."""

    def __init__(self, message, position=None, line=None):
        self.position = position
        self.line = line
        where = ""
        if line is not None:
            where = "line {}: ".format(line)
        elif position is not None:
            where = "position {}: ".format(position)
        super().__init__(where + message)


class CyclicMacroError(Exception):
    """Raised by :func:`expand_text` when expansion reaches a recursive cycle."""

    def __init__(self, cycle):
        self.cycle = list(cycle)
        super().__init__(
            "unbounded recursive expansion, reachable cycle: "
            + " -> ".join(self.cycle)
        )


class MacroDef(object):
    """A single macro definition: ``\name[n]{body}``."""

    __slots__ = ("name", "num_args", "body", "body_tokens")

    def __init__(self, name, num_args, body, body_tokens=None):
        self.name = name
        self.num_args = num_args
        self.body = body
        self.body_tokens = body_tokens

    def __repr__(self):
        return "MacroDef(name={!r}, num_args={!r}, body={!r})".format(
            self.name, self.num_args, self.body
        )


class TextToken(object):
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return "TextToken({!r})".format(self.text)


class GroupToken(object):
    """A braced group; the braces themselves carry no characters."""

    __slots__ = ("inner", "raw")

    def __init__(self, inner, raw):
        self.inner = inner
        self.raw = raw

    def __repr__(self):
        return "GroupToken({!r})".format(self.inner)


class ParamToken(object):
    """A ``#k`` parameter reference inside a definition body."""

    __slots__ = ("index",)

    def __init__(self, index):
        self.index = index

    def __repr__(self):
        return "ParamToken({})".format(self.index)


class MacroToken(object):
    """A macro call together with the already-parsed argument token lists."""

    __slots__ = ("name", "arguments", "raw_control_word")

    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments
        self.raw_control_word = "\\" + name

    def __repr__(self):
        return "MacroToken(name={!r}, arguments={!r})".format(
            self.name, self.arguments
        )


def _read_braced(source, open_pos, line=None):
    """Return the inner string and the index just past the matching ``}``."""
    depth = 0
    for pos in range(open_pos, len(source)):
        char = source[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[open_pos + 1:pos], pos + 1
    raise MacroSyntaxError("unbalanced opening brace", position=open_pos, line=line)


#: Maximum brace/macro nesting accepted while parsing.  The tokenizer is
#: recursive, so this stays comfortably below Python's own recursion limit;
#: exceeding it raises a clean :class:`MacroSyntaxError` rather than an
#: interpreter-level ``RecursionError``.  Evaluation itself runs on an
#: explicit heap stack and supports much deeper *expanded* nesting.
_MAX_PARSE_NESTING = 400


def _tokenize(source, arity_of, line=None, allow_params=False, depth=0):
    """Tokenize definition bodies or analyzed text.

    ``arity_of(name)`` returns the declared parameter count for defined macros
    and ``None`` for unsupported ones (which therefore consume no arguments).
    ``allow_params`` is set only inside a definition body, where ``#k`` denotes
    a parameter; elsewhere ``#`` is an ordinary literal character.
    """
    if depth > _MAX_PARSE_NESTING:
        raise MacroSyntaxError(
            "input nesting exceeds {} levels".format(_MAX_PARSE_NESTING), line=line
        )
    tokens = []
    length = len(source)
    pos = 0

    def push_text(text):
        if not text:
            return
        if tokens and isinstance(tokens[-1], TextToken):
            tokens[-1].text += text
        else:
            tokens.append(TextToken(text))

    while pos < length:
        char = source[pos]
        if char == "{":
            inner, after = _read_braced(source, pos, line=line)
            tokens.append(
                GroupToken(
                    _tokenize(
                        inner,
                        arity_of,
                        line=line,
                        allow_params=allow_params,
                        depth=depth + 1,
                    ),
                    source[pos:after],
                )
            )
            pos = after
        elif char == "}":
            raise MacroSyntaxError("unmatched closing brace", position=pos, line=line)
        elif char == "#":
            if allow_params and pos + 1 < length and source[pos + 1] == "#":
                # A doubled hash in a definition body denotes one literal '#'.
                push_text("#")
                pos += 2
            elif (
                allow_params
                and pos + 1 < length
                and source[pos + 1].isdigit()
                and source[pos + 1] != "0"
            ):
                tokens.append(ParamToken(int(source[pos + 1])))
                pos += 2
            else:
                push_text("#")
                pos += 1
        elif char == "\\":
            macro, pos = _parse_macro(
                source, pos, arity_of, line, allow_params, depth
            )
            tokens.append(macro)
        else:
            push_text(char)
            pos += 1
    return tokens


def _parse_macro(
    source, backslash_pos, arity_of, line=None, allow_params=False, depth=0
):
    """Parse a control word starting at ``backslash_pos`` and its arguments."""
    length = len(source)
    pos = backslash_pos + 1
    if pos >= length or not source[pos].isalpha():
        raise MacroSyntaxError(
            "a backslash must be followed by a macro name",
            position=backslash_pos,
            line=line,
        )
    name_start = pos
    while pos < length and source[pos].isalpha():
        pos += 1
    name = source[name_start:pos]

    arity = arity_of(name)
    arguments = []
    if arity is None:
        return MacroToken(name, arguments), pos

    for arg_index in range(arity):
        while pos < length and source[pos] in " \t\n":
            # Undelimited arguments skip intervening spaces, as in TeX.
            pos += 1
        if pos >= length:
            raise MacroSyntaxError(
                "macro \\{} expects {} argument(s), got {}".format(
                    name, arity, arg_index
                ),
                line=line,
            )
        char = source[pos]
        if char == "{":
            inner, after = _read_braced(source, pos, line=line)
            arguments.append(
                _tokenize(
                    inner,
                    arity_of,
                    line=line,
                    allow_params=allow_params,
                    depth=depth + 1,
                )
            )
            pos = after
        elif char == "}":
            raise MacroSyntaxError(
                "macro \\{} expects argument {} but found {!r}".format(
                    name, arg_index + 1, char
                ),
                position=pos,
                line=line,
            )
        elif char == "\\":
            nested, pos = _parse_macro(
                source,
                pos,
                arity_of,
                line,
                allow_params=allow_params,
                depth=depth + 1,
            )
            arguments.append([nested])
        else:
            arguments.append([TextToken(char)])
            pos += 1
    return MacroToken(name, arguments), pos


def _parse_definition_line(line, line_no):
    """Parse one line as a single definition.

    Returns ``None`` when the line is not exactly one definition (for example
    it contains text after the body, or does not start with a control word);
    such lines mark the beginning of the text section.  Malformed definitions
    still raise :class:`MacroSyntaxError`.
    """
    stripped = line.strip()
    if not stripped.startswith("\\"):
        return None
    pos = 1
    length = len(stripped)
    name_start = pos
    while pos < length and stripped[pos].isalpha():
        pos += 1
    if name_start == pos:
        raise MacroSyntaxError(
            "a macro name must consist of letters", line=line_no
        )
    name = stripped[name_start:pos]

    while pos < length and stripped[pos] in " \t":
        pos += 1
    num_args = 0
    if pos < length and stripped[pos] == "[":
        close = stripped.find("]", pos + 1)
        if close == -1 or not stripped[pos + 1:close].isdigit():
            raise MacroSyntaxError(
                "malformed parameter count, expected [n]", line=line_no
            )
        num_args = int(stripped[pos + 1:close])
        if not 1 <= num_args <= 9:
            raise MacroSyntaxError(
                "parameter count must be between 1 and 9", line=line_no
            )
        pos = close + 1
        while pos < length and stripped[pos] in " \t":
            pos += 1

    if pos >= length or stripped[pos] != "{":
        raise MacroSyntaxError(
            "macro \\{} is missing its {{body}}".format(name), line=line_no
        )
    depth = 0
    body_end = None
    for scan in range(pos, length):
        if stripped[scan] == "{":
            depth += 1
        elif stripped[scan] == "}":
            depth -= 1
            if depth == 0:
                body_end = scan
                break
    if body_end is None:
        raise MacroSyntaxError(
            "unbalanced brace in macro \\{} body".format(name), line=line_no
        )
    body = stripped[pos + 1:body_end]
    if stripped[body_end + 1:].strip():
        # More content follows a complete definition on the same line; this
        # cannot be a definition line, so the line belongs to the text.
        return None
    return MacroDef(name=name, num_args=num_args, body=body)


def parse_input(source):
    """Parse a full input string into ``(definitions, text)``.

    The returned definitions are fully tokenized, including bodies that refer
    to macros defined further down.
    """
    raw_definitions = []
    text_lines = []
    in_definitions = True
    for line_no, line in enumerate(source.splitlines(), start=1):
        if in_definitions:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped == "---":
                in_definitions = False
                continue
            if not stripped.startswith("\\"):
                in_definitions = False
                text_lines.append(line)
                continue
            definition = _parse_definition_line(line, line_no)
            if definition is None:
                in_definitions = False
                text_lines.append(line)
                continue
            raw_definitions.append(definition)
        else:
            text_lines.append(line)

    definitions = {}
    for definition in raw_definitions:
        if definition.name in definitions:
            raise MacroSyntaxError(
                "macro \\{} is defined more than once".format(definition.name)
            )
        definitions[definition.name] = definition

    def arity_of(name):
        definition = definitions.get(name)
        if definition is None:
            return None
        return definition.num_args

    for definition in raw_definitions:
        body_tokens = _tokenize(
            definition.body,
            arity_of,
            line=("body of \\" + definition.name),
            allow_params=True,
        )
        for token in _iter_tokens(body_tokens):
            if isinstance(token, ParamToken) and token.index > definition.num_args:
                raise MacroSyntaxError(
                    "macro \\{} has {} parameter(s) but its body uses #{}".format(
                        definition.name, definition.num_args, token.index
                    )
                )
        definition.body_tokens = body_tokens

    text = "\n".join(text_lines)
    text_tokens = _tokenize(text, arity_of)
    return raw_definitions, text, text_tokens


def _iter_tokens(tokens):
    for token in tokens:
        yield token
        if isinstance(token, GroupToken):
            for inner in _iter_tokens(token.inner):
                yield inner
        elif isinstance(token, MacroToken):
            for argument in token.arguments:
                for inner in _iter_tokens(argument):
                    yield inner


def _macro_calls_in(tokens):
    """Yield the names of every macro call nested in a token list."""
    for token in tokens:
        if isinstance(token, MacroToken):
            yield token.name
            for argument in token.arguments:
                for nested in _macro_calls_in(argument):
                    yield nested
        elif isinstance(token, GroupToken):
            for nested in _macro_calls_in(token.inner):
                yield nested


def _canonical_cycle(members):
    """Rotate a cycle's member list to a canonical first member."""
    rotated = min(
        tuple(members[k:] + members[:k]) for k in range(len(members))
    )
    return tuple(rotated) + (rotated[0],)


def _find_reachable_cycles(graph, roots):
    """Enumerate simple cycles of ``graph`` reachable from ``roots``.

    The graph maps a macro name to the names called from its body.  Each cycle
    is returned canonicalized (smallest rotation first, closing member
    repeated).  Terminating expansion of a system whose reachable subgraph has no
    cycle is guaranteed: along any expansion, body-level calls descend along
    graph edges, while calls supplied as arguments are finite fixed terms that
    reset the guard when substituted.
    """
    reachable = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in reachable or name not in graph:
            continue
        reachable.add(name)
        pending.extend(graph[name])

    cycles = []
    seen = set()

    def walk(start, name, stack, on_stack):
        for target in graph.get(name, ()):
            if target not in reachable:
                continue
            if target == start:
                cycle = _canonical_cycle(list(stack))
                if cycle not in seen:
                    seen.add(cycle)
                    cycles.append(cycle)
            elif target not in on_stack and target > start:
                # Every simple cycle has a smallest member, which is the only
                # root under which it is explored, so nothing is missed or
                # reported twice before canonicalization.
                stack.append(target)
                on_stack.add(target)
                walk(start, target, stack, on_stack)
                on_stack.remove(target)
                stack.pop()

    for root in sorted(reachable):
        walk(root, root, [root], set([root]))
    return cycles


class ExpansionResult(object):
    """Outcome of analyzing one piece of text."""

    __slots__ = (
        "total_chars",
        "call_counts",
        "char_contributions",
        "unsupported_macros",
        "cyclic_macros",
        "cycles",
        "truncated",
    )

    def __init__(
        self,
        total_chars,
        call_counts,
        char_contributions,
        unsupported_macros,
        cyclic_macros,
        cycles,
        truncated=False,
    ):
        #: Total number of characters after full expansion, or ``None`` when a
        #: reachable recursive cycle makes the expansion unbounded.
        self.total_chars = total_chars
        #: Number of times each macro name is invoked (defined or unsupported).
        #: For a cyclic input this counts only the bounded best-effort pass.
        self.call_counts = dict(call_counts)
        #: Additive character attribution.  On a finite input every output
        #: character is charged exactly once: literal body text to the
        #: defining macro, unsupported control words to their own name, and
        #: plain input text to ``None``.  For a cyclic input the counts cover
        #: only the bounded best-effort portion.
        self.char_contributions = dict(char_contributions)
        #: Names of macros that were invoked but never defined, in call order.
        self.unsupported_macros = list(unsupported_macros)
        #: Names participating in a reachable call-graph cycle.
        self.cyclic_macros = set(cyclic_macros)
        #: Concrete cycles found, each given as an ordered list of macro names
        #: with the first name repeated at the end.
        self.cycles = [list(cycle) for cycle in cycles]
        #: Whether the best-effort statistics for a cyclic input were cut off
        #: by the evaluation cap.  Always ``False`` on finite inputs.
        self.truncated = truncated

    @property
    def is_finite(self):
        return self.total_chars is not None

    def to_dict(self):
        return {
            "finite": self.is_finite,
            "total_chars": self.total_chars,
            "call_counts": self.call_counts,
            "char_contributions": {
                ("" if key is None else key): value
                for key, value in self.char_contributions.items()
            },
            "unsupported_macros": self.unsupported_macros,
            "cyclic_macros": sorted(self.cyclic_macros),
            "cycles": self.cycles,
            "truncated": self.truncated,
        }


#: Upper bound on macro invocations evaluated while gathering partial
#: statistics for an input whose call graph contains a reachable cycle.  The
#: bound never produces a fabricated total: such inputs report ``total_chars``
#: as ``None`` regardless.
_MAX_CYCLIC_ENTRIES = 10000

#: Upper bound on simultaneously nested expansion frames.  Evaluation runs on
#: an explicit heap-allocated frame stack rather than Python's call stack, so
#: this can be large; it bounds non-termination hidden behind parameter
#: forwarding without risking an interpreter recursion error.
_MAX_EXPANSION_DEPTH = 100000


class _LengthFrame(object):
    __slots__ = ("tokens", "pos", "env", "owner", "guard")

    def __init__(self, tokens, env, owner, guard):
        self.tokens = tokens
        self.pos = 0
        # Each environment entry is a thunk ``(token_list, enclosing_env)``:
        # parameter references inside a call argument resolve against the
        # caller's environment, not the callee's.
        self.env = env
        self.owner = owner
        self.guard = guard


def _evaluate_length(text_tokens, definitions_by_name, max_entries, max_depth):
    """Exact character count via an explicit frame stack.

    Returns ``(total, call_counts, contributions, unsupported, truncated)``.
    ``truncated`` means the entry or depth budget was exhausted; the returned
    total is then partial and must not be presented as an exact answer.
    """
    call_counts = {}
    contributions = {}
    unsupported = []
    truncated = False
    entries_left = max_entries
    stack = [_LengthFrame(text_tokens, [], None, frozenset())]
    total = 0

    def add(owner, amount):
        contributions[owner] = contributions.get(owner, 0) + amount

    while stack:
        frame = stack[-1]
        if frame.pos >= len(frame.tokens):
            stack.pop()
            continue
        token = frame.tokens[frame.pos]
        frame.pos += 1

        if isinstance(token, TextToken):
            total += len(token.text)
            add(frame.owner, len(token.text))
        elif isinstance(token, GroupToken):
            stack.append(
                _LengthFrame(token.inner, frame.env, frame.owner, frame.guard)
            )
        elif isinstance(token, ParamToken):
            argument_tokens, argument_env = frame.env[token.index - 1]
            # Substitution boundary: argument text is fixed call-site data, so
            # it starts a fresh body-recursion guard.  Its characters remain
            # charged to the macro performing the substitution, so a parameter
            # used twice contributes its text twice.
            stack.append(
                _LengthFrame(argument_tokens, argument_env, frame.owner, frozenset())
            )
        elif isinstance(token, MacroToken):
            name = token.name
            call_counts[name] = call_counts.get(name, 0) + 1
            definition = definitions_by_name.get(name)
            if definition is None:
                if name not in unsupported:
                    unsupported.append(name)
                total += len(token.raw_control_word)
                add(name, len(token.raw_control_word))
                continue
            if name in frame.guard:
                # Body-level re-entry: the call-graph analysis already named
                # this cycle; skip the unexpandable branch and keep going.
                continue
            if entries_left <= 0 or len(stack) >= max_depth:
                truncated = True
                continue
            entries_left -= 1
            bound_env = [
                (argument, frame.env) for argument in token.arguments
            ]
            stack.append(
                _LengthFrame(
                    definition.body_tokens,
                    bound_env,
                    name,
                    frame.guard.union((name,)),
                )
            )
        else:
            raise MacroSyntaxError(
                "internal error: unknown token {!r}".format(token)
            )

    return total, call_counts, contributions, unsupported, truncated


def analyze(source):
    """Parse ``source`` and estimate its fully expanded character count."""
    definitions, _text, text_tokens = parse_input(source)
    definitions_by_name = dict((d.name, d) for d in definitions)

    call_graph = {}
    for definition in definitions:
        call_graph[definition.name] = list(
            _macro_calls_in(definition.body_tokens)
        )
    roots = list(_macro_calls_in(text_tokens))
    cycles = _find_reachable_cycles(call_graph, roots)
    cyclic_macros = set()
    for cycle in cycles:
        cyclic_macros.update(cycle[:-1])
    graph_cyclic = bool(cycles)

    total, call_counts, contributions, unsupported, truncated = _evaluate_length(
        text_tokens,
        definitions_by_name,
        max_entries=_MAX_CYCLIC_ENTRIES,
        max_depth=_MAX_EXPANSION_DEPTH,
    )
    return ExpansionResult(
        total_chars=(None if (graph_cyclic or truncated) else total),
        call_counts=call_counts,
        char_contributions=contributions,
        unsupported_macros=unsupported,
        cyclic_macros=cyclic_macros,
        cycles=cycles,
        truncated=truncated,
    )


class _TextFrame(object):
    __slots__ = ("tokens", "pos", "env", "guard", "trail", "pieces")

    def __init__(self, tokens, env, guard, trail):
        self.tokens = tokens
        self.pos = 0
        self.env = env
        self.guard = guard
        self.trail = trail
        self.pieces = []


def expand_text(source, max_entries=100000):
    """Return the fully expanded text, or raise :class:`CyclicMacroError`.

    This is a convenience for checking the length engine against an actual
    expansion on finite inputs.  Recursive inputs, including recursion hidden
    behind parameter forwarding, raise :class:`CyclicMacroError`; evaluation
    runs on an explicit frame stack so the check cannot overflow Python's own
    recursion limit.
    """
    definitions, _text, text_tokens = parse_input(source)
    definitions_by_name = dict((d.name, d) for d in definitions)
    entries_left = max_entries
    root = _TextFrame(text_tokens, [], frozenset(), [])
    stack = [root]

    while stack:
        frame = stack[-1]
        if frame.pos >= len(frame.tokens):
            finished_text = "".join(frame.pieces)
            stack.pop()
            if stack:
                stack[-1].pieces.append(finished_text)
            continue
        token = frame.tokens[frame.pos]
        frame.pos += 1

        if isinstance(token, TextToken):
            frame.pieces.append(token.text)
        elif isinstance(token, GroupToken):
            stack.append(
                _TextFrame(token.inner, frame.env, frame.guard, frame.trail)
            )
        elif isinstance(token, ParamToken):
            argument_tokens, argument_env = frame.env[token.index - 1]
            stack.append(
                _TextFrame(argument_tokens, argument_env, frozenset(), [])
            )
        elif isinstance(token, MacroToken):
            definition = definitions_by_name.get(token.name)
            if definition is None:
                frame.pieces.append(token.raw_control_word)
                continue
            if token.name in frame.guard:
                start = frame.trail.index(token.name)
                raise CyclicMacroError(frame.trail[start:] + [token.name])
            if entries_left <= 0 or len(stack) >= _MAX_EXPANSION_DEPTH:
                raise CyclicMacroError(frame.trail + [token.name])
            entries_left -= 1
            bound_env = [
                (argument, frame.env) for argument in token.arguments
            ]
            stack.append(
                _TextFrame(
                    definition.body_tokens,
                    bound_env,
                    frame.guard.union((token.name,)),
                    frame.trail + [token.name],
                )
            )
        else:
            raise MacroSyntaxError(
                "internal error: unknown token {!r}".format(token)
            )

    return "".join(root.pieces)


def _format_human(result):
    lines = []
    if result.is_finite:
        lines.append("Total expanded characters: {}".format(result.total_chars))
    else:
        lines.append("Total expanded characters: UNBOUNDED (recursive macro)")
    lines.append("")
    lines.append("Macro calls and character contributions:")
    names = sorted(result.call_counts)
    width = max([len("macro")] + [len(name) for name in names]) if names else len("macro")
    lines.append(
        "  {name:<{width}}  {calls:>6}  {chars:>10}".format(
            name="macro", width=width, calls="calls", chars="chars"
        )
    )
    for name in names:
        chars = result.char_contributions.get(name)
        chars_text = "-" if chars is None else str(chars)
        lines.append(
            "  {name:<{width}}  {calls:>6}  {chars:>10}".format(
                name=name,
                width=width,
                calls=result.call_counts[name],
                chars=chars_text,
            )
        )
    plain = result.char_contributions.get(None)
    if plain is not None:
        lines.append("")
        lines.append(
            "Literal input text outside any macro body: {} character(s)".format(plain)
        )
    if result.unsupported_macros:
        lines.append("")
        lines.append(
            "Unsupported macros kept verbatim: "
            + ", ".join("\\" + name for name in result.unsupported_macros)
        )
    if result.cycles:
        lines.append("")
        lines.append("Reachable recursive cycles (no finite count exists):")
        for cycle in result.cycles:
            lines.append("  " + " -> ".join(cycle))
    if result.truncated:
        lines.append("")
        lines.append(
            "Evaluation was capped at {} macro entries / {} nesting levels; "
            "the statistics above are partial and no exact total exists.".format(
                _MAX_CYCLIC_ENTRIES, _MAX_EXPANSION_DEPTH
            )
        )
    return "\n".join(lines)


def main(argv=None):
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description=(
            "estimate the fully expanded character count of text under "
            "text-substitution macro definitions"
        )
    )
    parser.add_argument(
        "file",
        help="input file; use '-' to read the definition and text from stdin",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the result as JSON instead of a human-readable report",
    )
    args = parser.parse_args(argv)

    if args.file == "-":
        source = sys.stdin.read()
    else:
        with open(args.file, "r", encoding="utf-8") as handle:
            source = handle.read()

    try:
        result = analyze(source)
    except MacroSyntaxError as error:
        sys.stderr.write("error: {}\n".format(error))
        return 2

    if args.json:
        sys.stdout.write(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_format_human(result))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
