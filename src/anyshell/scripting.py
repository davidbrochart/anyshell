"""Python control flow around shell statements, without a second Python grammar."""

from __future__ import annotations

import ast
import builtins
import io
import re
import shlex
import tokenize
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from types import CodeType


class IncompleteSource(ValueError):
    """More input is needed before a statement can be submitted."""


INTERNAL_NAMES = {"__builtins__", "__anyshell__"}


class Namespace(dict):
    """A session's Python globals, with command names reserved for commands.

    This protects ordinary Python bindings, not against deliberately hostile
    Python code. Like commands, scripts are trusted code rather than a sandbox.
    """

    def __init__(self, commands: Callable[[], Mapping]) -> None:
        super().__init__()
        self.commands = commands
        dict.__setitem__(self, "__builtins__", vars(builtins).copy())

    def check_name(self, name: str) -> None:
        if name in self.commands():
            raise ValueError(f"{name!r} is a reserved command name")
        if name in INTERNAL_NAMES:
            raise ValueError(f"{name!r} is a reserved shell name")

    def __setitem__(self, name, value):
        self.check_name(name)
        super().__setitem__(name, value)

    def __delitem__(self, name):
        self.check_name(name)
        super().__delitem__(name)

    def update(self, *args, **kwargs):
        values = dict(*args, **kwargs)
        for name in values:
            self.check_name(name)
        super().update(values)

    def setdefault(self, name, default=None):
        self.check_name(name)
        return super().setdefault(name, default)

    def __ior__(self, values):
        self.update(values)
        return self


@dataclass
class Script:
    code: CodeType
    simple: bool
    compound: bool


def _offset(source: str, position: tuple[int, int]) -> int:
    row, column = position
    offset = 0
    for _ in range(row - 1):
        newline = source.find("\n", offset)
        if newline == -1:
            return len(source)
        offset = newline + 1
    return offset + column


def _expression_end(source: str, start: int) -> int:
    """Use Python's tokenizer to match braces, including nested literals/strings."""
    depth = 0
    text = source[start:]
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.OP:
                if token.string == "{":
                    depth += 1
                elif token.string == "}":
                    depth -= 1
                    if depth == 0:
                        return start + _offset(text, token.end)
    except tokenize.TokenError as error:
        raise IncompleteSource("unfinished Python expression") from error
    raise IncompleteSource("unfinished Python expression")


def _shell_end(source: str, start: int) -> int:
    quote = ""
    pipe = False
    index = start
    while index < len(source):
        char = source[index]
        if char == "\\" and quote != "'":
            if index + 1 == len(source):
                raise IncompleteSource("unfinished escape")
            if source[index + 1] != "\n":
                pipe = False
            index += 2
            continue
        if char == "{" and quote != "'":
            index = _expression_end(source, index)
            pipe = False
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            pipe = False
        elif char in ";&" or (char == "\n" and not pipe):
            return index
        elif not char.isspace():
            pipe = char == "|"
        index += 1
    if quote or pipe:
        raise IncompleteSource("unfinished command")
    return index


def _mask_expressions(source: str, marker: str) -> tuple[str, list[ast.expr]]:
    output = []
    expressions = []
    quote = ""
    index = 0
    while index < len(source):
        char = source[index]
        if char == "\\" and quote != "'":
            output.append(source[index : index + 2])
            index += 2
            continue
        if char == "{" and quote != "'":
            end = _expression_end(source, index)
            text = source[index + 1 : end - 1].strip()
            if not text:
                raise ValueError("empty Python expression")
            expression = ast.parse(
                "(" + text + "\n)", filename="<anyshell>", mode="eval"
            ).body
            output.append(f"{marker}{len(expressions)}__")
            expressions.append(expression)
            index = end
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        output.append(char)
        index += 1
    return "".join(output), expressions


def _command_name(source: str) -> str | None:
    lexer = shlex.shlex(source, posix=True, punctuation_chars="|&;<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return lexer.get_token()
    except ValueError:
        return None


def _assignment(source: str) -> tuple[str, str] | None:
    """Find a Python assignment target, leaving a possible shell RHS untouched."""
    depth = 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if depth == 0 and (
                token.type in {tokenize.NEWLINE, tokenize.ENDMARKER}
                or token.string == ";"
            ):
                break
            if token.type != tokenize.OP:
                continue
            if (
                token.string
                in {
                    "=",
                    "+=",
                    "-=",
                    "*=",
                    "/=",
                    "//=",
                    "%=",
                    "**=",
                    "&=",
                    "|=",
                    "^=",
                    ">>=",
                    "<<=",
                    "@=",
                }
                and depth == 0
            ):
                end = _offset(source, token.end)
                prefix, rhs = source[:end], source[end:]
                try:
                    tree = ast.parse(prefix + " None")
                except SyntaxError:
                    return None
                if len(tree.body) == 1 and isinstance(
                    tree.body[0], (ast.Assign, ast.AnnAssign, ast.AugAssign)
                ):
                    return prefix, rhs
                return None
            if token.string in {"(", "[", "{"}:
                depth += 1
            elif token.string in {")", "]", "}"}:
                depth -= 1
    except (tokenize.TokenError, IndentationError):
        pass
    return None


_BLOCK = re.compile(
    r"(?:async\s+(?:for|with|def)|for|while|if|elif|else|def|class|with|try|except|finally|match|case)\b"
)


def _valid_header(header: str) -> bool:
    kind = _BLOCK.match(header)[0]
    if kind in {"elif", "else"}:
        candidate = "if False: pass\n" + header + " pass"
    elif kind in {"except", "finally"}:
        candidate = "try: pass\n" + header + " pass"
    elif kind == "try":
        candidate = header + " pass\nexcept: pass"
    elif kind == "case":
        candidate = "match None:\n    " + header + " pass"
    else:
        candidate = header + " pass"
    try:
        ast.parse(candidate)
    except SyntaxError:
        return False
    return True


def _python_end(source: str, *, header: bool = False) -> int:
    depth = 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in {tokenize.NEWLINE, tokenize.NL} and depth == 0:
                return _offset(source, token.start)
            if token.type == tokenize.ENDMARKER:
                return len(source)
            if token.type != tokenize.OP:
                continue
            if depth == 0:
                if token.string == ";":
                    return _offset(source, token.start)
                if token.string == ":" and header:
                    end = _offset(source, token.end)
                    if _valid_header(source[:end]):
                        return end
            if token.string in {"(", "[", "{"}:
                depth += 1
            elif token.string in {")", "]", "}"}:
                depth -= 1
    except tokenize.TokenError as error:
        raise IncompleteSource("unfinished Python statement") from error
    return len(source)


class _Bindings(ast.NodeVisitor):
    def __init__(self, commands):
        self.commands = commands

    def check(self, name):
        if name in self.commands:
            raise ValueError(f"{name!r} is a reserved command name")
        if name in INTERNAL_NAMES:
            raise ValueError(f"{name!r} is a reserved shell name")

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.check(node.id)

    def visit_alias(self, node):
        if node.name != "*":
            self.check(node.asname or node.name.split(".")[0])

    def visit_arg(self, node):
        self.check(node.arg)

    def visit_FunctionDef(self, node):
        self.check(node.name)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_ExceptHandler(self, node):
        if node.name:
            self.check(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            self.check(node.name)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest:
            self.check(node.rest)
        self.generic_visit(node)


def _runtime_call(method, *args, **kwargs):
    return ast.Await(
        value=ast.Call(
            func=ast.Attribute(
                value=ast.Name(id="__anyshell__", ctx=ast.Load()),
                attr=method,
                ctx=ast.Load(),
            ),
            args=list(args),
            keywords=[
                ast.keyword(arg=name, value=value) for name, value in kwargs.items()
            ],
        )
    )


class _Lower(ast.NodeTransformer):
    def __init__(self, calls):
        self.calls = calls
        self.async_context = True

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in self.calls:
            source, masked, marker, expressions, capture = self.calls[node.func.id]
            expressions = [
                ast.increment_lineno(expr, node.lineno - 1) for expr in expressions
            ]
            return ast.copy_location(
                _runtime_call(
                    "run",
                    ast.Constant(source),
                    ast.Constant(masked),
                    ast.Constant(marker),
                    ast.Tuple(elts=expressions, ctx=ast.Load()),
                    capture=ast.Constant(capture),
                ),
                node,
            )
        return self.generic_visit(node)

    def visit_Expr(self, node):
        generated = (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in self.calls
        )
        if (
            generated
            or not self.async_context
            or isinstance(node.value, (ast.Yield, ast.YieldFrom))
        ):
            return self.generic_visit(node)
        if isinstance(node.value, ast.Name):
            value = _runtime_call("lookup", ast.Constant(node.value.id))
        else:
            value = _runtime_call("show", self.visit(node.value))
        return ast.copy_location(ast.Expr(value=value), node)

    def visit_For(self, node):
        node = self.generic_visit(node)
        if self.async_context:
            checkpoint = ast.Expr(value=_runtime_call("checkpoint"))
            node.body.insert(0, ast.copy_location(checkpoint, node))
        return node

    visit_While = visit_For
    visit_AsyncFor = visit_For

    def visit_FunctionDef(self, node):
        previous = self.async_context
        self.async_context = isinstance(node, ast.AsyncFunctionDef)
        node = self.generic_visit(node)
        self.async_context = previous
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        previous = self.async_context
        self.async_context = False
        node = self.generic_visit(node)
        self.async_context = previous
        return node


def compile_script(
    source: str, commands: Mapping, variables: Collection[str] = ()
) -> Script:
    """Lower shell statements and delegate Python syntax/indentation to Python."""
    prefix = "__anyshell_command_"
    while prefix in source:
        prefix += "_"
    marker = prefix + "value_"
    calls = {}
    pieces = []
    simple = True
    compound = False
    index = 0
    while index < len(source):
        start = index
        while index < len(source) and source[index] in " \t\r":
            index += 1
        indent = source[start:index]
        if index == len(source):
            pieces.append(indent)
            break
        if source[index] == "\n":
            pieces.append(indent + "\n")
            index += 1
            continue
        if source[index] in ";&":
            raise ValueError("empty command; && is not supported")
        remaining = source[index:]
        head = _command_name(remaining)
        if _BLOCK.match(remaining) and head not in commands:
            end = index + _python_end(remaining, header=True)
            if source[index:end].rstrip().endswith(":"):
                pieces.append(indent + source[index:end])
                simple = False
                compound = True
                index = end
                continue
        assignment = _assignment(remaining)
        capture = (
            assignment is not None
            and not assignment[1].lstrip().startswith(("'", '"'))
            and _command_name(assignment[1]) in commands
        )
        head = _command_name(remaining)
        command = head in commands and assignment is None
        if (
            assignment is None
            and head is not None
            and re.fullmatch(r"[A-Za-z_]\w*(?:-\w+)+", head)
            and head.split("-", 1)[0] not in variables
        ):
            command = True
        body_start = index + (len(assignment[0]) if capture else 0)
        if not command and not capture:
            try:
                end = index + _python_end(remaining)
                text = source[index:end]
                parsed = ast.parse(text, filename="<anyshell>")
            except SyntaxError:
                if assignment is not None or head in {
                    "import",
                    "from",
                    "del",
                    "global",
                    "nonlocal",
                    "raise",
                    "assert",
                    "return",
                    "yield",
                    "for",
                    "while",
                    "if",
                    "elif",
                    "else",
                    "def",
                    "class",
                    "try",
                    "except",
                    "finally",
                    "with",
                    "async",
                }:
                    raise
                command = True
            else:
                pieces.append(indent + text)
                bare_name = (
                    len(parsed.body) == 1
                    and isinstance(parsed.body[0], ast.Expr)
                    and isinstance(parsed.body[0].value, ast.Name)
                )
                simple = (
                    simple
                    and bare_name
                    and parsed.body[0].value.id not in variables
                    and parsed.body[0].value.id not in vars(builtins)
                )
                index = end
        if command or capture:
            end = _shell_end(source, body_start)
            text = source[body_start:end]
            background = end < len(source) and source[end] == "&"
            if background:
                text += "&"
            masked, expressions = _mask_expressions(text, marker)
            name = f"{prefix}{len(calls)}"
            calls[name] = (text, masked, marker, expressions, capture)
            replacement = (assignment[0] if capture else "") + f"{name}()"
            original = source[index:end]
            pieces.append(
                indent
                + replacement
                + "\n" * (original.count("\n") - replacement.count("\n"))
            )
            simple = simple and not capture and not expressions
            index = end
        if index < len(source) and source[index] in ";&\n":
            pieces.append(";" if source[index] == "&" else source[index])
            index += 1
    tree = ast.parse("".join(pieces), filename="<anyshell>")
    tree = _Lower(calls).visit(tree)
    _Bindings(commands).visit(tree)
    ast.fix_missing_locations(tree)
    code = compile(tree, "<anyshell>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    return Script(code, simple, compound)


def is_complete(source: str, commands: Mapping) -> bool:
    try:
        script = compile_script(source, commands)
    except IncompleteSource:
        return False
    except SyntaxError as error:
        return not (
            "expected an indented block" in error.msg
            or "was never closed" in error.msg
            or "unexpected EOF" in error.msg
            or "expected 'except' or 'finally'" in error.msg
        )
    except ValueError:
        return True  # Complete but invalid input should report its error on Enter.
    return not (
        script.compound and "\n" in source and source.rsplit("\n", 1)[-1].strip()
    )


def continuation_indent(source: str, commands: Mapping) -> str:
    """Indent a Python suite while leaving unfinished shell strings alone."""
    try:
        compile_script(source, commands)
    except IncompleteSource:
        return ""
    except (SyntaxError, ValueError):
        pass
    line = source.rsplit("\n", 1)[-1]
    indent = re.match(r"[ \t]*", line)[0]
    if _BLOCK.match(line.lstrip()) and _valid_header(line.lstrip()):
        indent += "    "
    return indent


def completion_segment(source: str) -> str:
    """Find the shell portion of a capture assignment while completing input."""
    assignment = _assignment(source.lstrip())
    if assignment is not None and not assignment[1].lstrip().startswith(("'", '"')):
        return assignment[1]
    return source
