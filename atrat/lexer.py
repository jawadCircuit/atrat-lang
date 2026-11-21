"""Lexer for Atrat: indentation-sensitive, Python-flavoured surface syntax."""
from __future__ import annotations
from dataclasses import dataclass, field


class AtratError(Exception):
    """Base diagnostic. Carries a source position so the driver can render a caret."""

    kind = "error"

    def __init__(self, msg: str, line: int = 0, col: int = 0, hint: str = ""):
        super().__init__(msg)
        self.msg = msg
        self.line = line
        self.col = col
        self.hint = hint


class LexError(AtratError):
    kind = "lex error"


@dataclass
class Token:
    kind: str
    value: object
    line: int
    col: int

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.kind},{self.value!r},{self.line}:{self.col})"


KEYWORDS = {
    "fn", "let", "var", "const", "struct", "enum", "impl", "trait", "match",
    "if", "elif", "else", "while", "for", "in", "return", "break", "continue",
    "true", "false", "unsafe", "asm", "extern", "import", "as", "mut", "self",
    "pub", "type", "where", "loop", "defer", "pass",
}

# Longest first: the scanner tries these in order.
OPERATORS = [
    "..=", "<<=", ">>=", "->", "=>", "::", "..", "==", "!=", "<=", ">=",
    "&&", "||", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<", ">>",
    "**",
    "+", "-", "*", "/", "%", "=", "<", ">", "!", "&", "|", "^", "~",
    ".", ",", ":", ";", "(", ")", "[", "]", "{", "}", "@", "?", "_",
]

OPS_BY_LEN = sorted(OPERATORS, key=len, reverse=True)


@dataclass
class StrSeg:
    """One segment of an interpolated string literal."""

    kind: str                      # "lit" or "expr"
    text: str
    line: int
    col: int
    parts: list = field(default_factory=list)   # parsed expressions, filled by parser


def _is_ident_start(c: str) -> bool:
    return c.isalpha() or c == "_"


def _is_ident(c: str) -> bool:
    return c.isalnum() or c == "_"


class Lexer:
    def __init__(self, src: str, filename: str = "<input>"):
        self.src = src.replace("\r\n", "\n").replace("\r", "\n")
        self.filename = filename
        self.i = 0
        self.line = 1
        self.col = 1
        self.n = len(self.src)
        self.tokens: list[Token] = []
        self.indents = [0]
        self.depth = 0
        self.at_line_start = True
        self.last_significant = None

    # -- helpers ----------------------------------------------------------
    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.src[j] if j < self.n else ""

    def advance(self, k: int = 1) -> None:
        for _ in range(k):
            if self.i < self.n:
                if self.src[self.i] == "\n":
                    self.line += 1
                    self.col = 1
                else:
                    self.col += 1
                self.i += 1

    def error(self, msg: str, hint: str = "") -> LexError:
        return LexError(msg, self.line, self.col, hint)

    def emit(self, kind: str, value, line: int | None = None, col: int | None = None):
        tok = Token(kind, value, line or self.line, col or self.col)
        self.tokens.append(tok)
        if kind not in ("NEWLINE", "INDENT", "DEDENT"):
            self.last_significant = tok
        return tok

    # -- main loop --------------------------------------------------------
    def tokenize(self) -> list[Token]:
        while self.i < self.n:
            if self.at_line_start and self.depth == 0:
                if not self._handle_indent():
                    continue
            c = self.peek()

            if c in " \t":
                self.advance()
                continue

            if c == "\n":
                if self.depth == 0:
                    if self.tokens and self.tokens[-1].kind not in ("NEWLINE", "INDENT", "DEDENT"):
                        self.emit("NEWLINE", None)
                    self.at_line_start = True
                    self.advance()
                else:
                    self.advance()
                continue

            if c == "#":
                while self.i < self.n and self.peek() != "\n":
                    self.advance()
                continue

            if c == "\\" and self.peek(1) == "\n":
                self.advance(2)
                self.at_line_start = False
                continue

            if c.isdigit():
                self._number()
                continue

            if c == '"':
                self._string()
                continue

            if c == "'":
                self._char()
                continue

            if _is_ident_start(c):
                self._ident()
                continue

            if self._operator():
                continue

            raise self.error(f"unexpected character {c!r}")

        if self.tokens and self.tokens[-1].kind not in ("NEWLINE", "INDENT", "DEDENT"):
            self.emit("NEWLINE", None, self.line, self.col)
        while len(self.indents) > 1:
            self.indents.pop()
            self.emit("DEDENT", None, self.line, self.col)
        self.emit("EOF", None, self.line, self.col)
        return self.tokens

    def _handle_indent(self) -> bool:
        """Consume leading whitespace and emit INDENT/DEDENT. Returns False if the
        whole line was blank/comment and got consumed."""
        start_i = self.i
        width = 0
        while self.i < self.n and self.peek() in " \t":
            if self.peek() == "\t":
                raise self.error("tabs are not allowed for indentation", "use 4 spaces")
            width += 1
            self.advance()

        c = self.peek()
        if c == "\n" or c == "#" or c == "":
            # blank or comment-only line: no structural tokens
            if c == "#":
                while self.i < self.n and self.peek() != "\n":
                    self.advance()
            if self.peek() == "\n":
                self.advance()
            self.at_line_start = True
            return False

        top = self.indents[-1]
        if width > top:
            self.indents.append(width)
            self.emit("INDENT", None)
        elif width < top:
            while len(self.indents) > 1 and self.indents[-1] > width:
                self.indents.pop()
                self.emit("DEDENT", None)
            if self.indents[-1] != width:
                raise self.error(
                    f"inconsistent indentation: expected {self.indents[-1]} spaces, got {width}",
                    "align the block with the lines above it",
                )
        self.at_line_start = False
        return True

    def _ident(self) -> None:
        line, col = self.line, self.col
        start = self.i
        while self.i < self.n and _is_ident(self.peek()):
            self.advance()
        word = self.src[start:self.i]
        if word in ("true", "false"):
            self.emit("BOOL", word == "true", line, col)
        elif word in KEYWORDS:
            self.emit("KW", word, line, col)
        else:
            self.emit("NAME", word, line, col)

    def _number(self) -> None:
        line, col = self.line, self.col
        start = self.i
        if self.peek() == "0" and self.peek(1) in "xX":
            self.advance(2)
            digits = self.i
            while self.i < self.n and (self.peek().isdigit() or self.peek().lower() in "abcdef" or self.peek() == "_"):
                self.advance()
            raw = self.src[digits:self.i].replace("_", "")
            if not raw:
                raise self.error("malformed hexadecimal literal")
            self.emit("INT", int(raw, 16), line, col)
            return
        if self.peek() == "0" and self.peek(1) in "bB":
            self.advance(2)
            digits = self.i
            while self.i < self.n and (self.peek() in "01_"):
                self.advance()
            raw = self.src[digits:self.i].replace("_", "")
            if not raw:
                raise self.error("malformed binary literal")
            self.emit("INT", int(raw, 2), line, col)
            return
        if self.peek() == "0" and self.peek(1) in "oO":
            self.advance(2)
            digits = self.i
            while self.i < self.n and (self.peek() in "01234567_"):
                self.advance()
            raw = self.src[digits:self.i].replace("_", "")
            if not raw:
                raise self.error("malformed octal literal")
            self.emit("INT", int(raw, 8), line, col)
            return

        while self.i < self.n and (self.peek().isdigit() or self.peek() == "_"):
            self.advance()
        is_float = False
        if self.peek() == "." and self.peek(1) != ".":
            is_float = True
            self.advance()
            while self.i < self.n and (self.peek().isdigit() or self.peek() == "_"):
                self.advance()
        if self.peek() in "eE":
            # exponent must be followed by digits (allow sign)
            save_i, save_line, save_col = self.i, self.line, self.col
            self.advance()
            if self.peek() in "+-":
                self.advance()
            if not self.peek().isdigit():
                self.i, self.line, self.col = save_i, save_line, save_col
            else:
                is_float = True
                while self.i < self.n and self.peek().isdigit():
                    self.advance()
        raw = self.src[start:self.i].replace("_", "")
        if is_float:
            self.emit("FLOAT", float(raw), line, col)
        else:
            self.emit("INT", int(raw), line, col)

    # -- literals ---------------------------------------------------------
    def _char(self) -> None:
        line, col = self.line, self.col
        self.advance()  # opening quote
        if self.i >= self.n:
            raise self.error("unterminated character literal")
        if self.peek() == "\\":
            self.advance()
            ch = self._escape()
        else:
            ch = self.peek()
            self.advance()
        if self.peek() != "'":
            raise self.error("character literal must contain exactly one character")
        self.advance()
        self.emit("CHAR", ch, line, col)

    def _escape(self) -> str:
        c = self.peek()
        mapping = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'", "{": "{", "}": "}"}
        if c in mapping:
            self.advance()
            return mapping[c]
        if c == "x":
            self.advance()
            h = self.src[self.i:self.i + 2]
            if len(h) != 2 or any(ch not in "0123456789abcdefABCDEF" for ch in h):
                raise self.error("invalid \\x escape (expected two hex digits)")
            self.advance(2)
            return chr(int(h, 16))
        if c == "u":
            self.advance()
            h = self.src[self.i:self.i + 4]
            if len(h) != 4 or any(ch not in "0123456789abcdefABCDEF" for ch in h):
                raise self.error("invalid \\u escape (expected four hex digits)")
            self.advance(4)
            return chr(int(h, 16))
        raise self.error(f"unknown escape sequence \\{c}")

    def _string(self) -> None:
        line, col = self.line, self.col
        self.advance()
        segs: list[StrSeg] = []
        buf: list[str] = []
        buf_line, buf_col = self.line, self.col

        def flush():
            nonlocal buf
            if buf:
                segs.append(StrSeg("lit", "".join(buf), buf_line, buf_col))
                buf = []

        while True:
            if self.i >= self.n:
                raise self.error("unterminated string literal")
            c = self.peek()
            if c == '"':
                self.advance()
                break
            if c == "\n":
                raise self.error("unterminated string literal", "strings may not span lines")
            if c == "\\":
                self.advance()
                buf.append(self._escape())
                continue
            if c == "{":
                if self.peek(1) == "{":
                    self.advance(2)
                    buf.append("{")
                    continue
                flush()
                eline, ecol = self.line, self.col
                self.advance()
                depth = 1
                start = self.i
                in_str = None
                while self.i < self.n and depth > 0:
                    ch = self.peek()
                    if in_str:
                        if ch == "\\":
                            self.advance(2)
                            continue
                        if ch == in_str:
                            in_str = None
                        self.advance()
                        continue
                    if ch in "\"'":
                        in_str = ch
                        self.advance()
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    self.advance()
                if depth != 0:
                    raise self.error("unclosed '{' in string interpolation")
                expr_src = self.src[start:self.i]
                self.advance()  # closing }
                if not expr_src.strip():
                    raise self.error("empty interpolation in string")
                segs.append(StrSeg("expr", expr_src, eline + 1, ecol + 1))
                buf_line, buf_col = self.line, self.col
                continue
            if c == "}" and self.peek(1) == "}":
                self.advance(2)
                buf.append("}")
                continue
            buf.append(c)
            self.advance()
        flush()
        if not segs:
            segs.append(StrSeg("lit", "", line, col))
        self.emit("STRING", segs, line, col)

    # -- operators --------------------------------------------------------
    def _operator(self) -> bool:
        for op in OPS_BY_LEN:
            if self.src.startswith(op, self.i):
                # `..` never consumes a third dot
                if op == ".." and self.peek(2) == ".":
                    continue
                line, col = self.line, self.col
                self.advance(len(op))
                if op in ("(", "[", "{"):
                    self.depth += 1
                elif op in (")", "]", "}"):
                    self.depth = max(0, self.depth - 1)
                self.emit("OP", op, line, col)
                return True
        return False


def tokenize(src: str, filename: str = "<input>") -> list[Token]:
    return Lexer(src, filename).tokenize()
