"""Recursive-descent + precedence-climbing parser for Atrat."""
from __future__ import annotations

from . import ast
from .lexer import Token, LexError, tokenize


class ParseError(LexError):
    kind = "parse error"


BINARY_PREC = {
    "||": 1,
    "&&": 2,
    "==": 3, "!=": 3,
    "<": 4, "<=": 4, ">": 4, ">=": 4,
    "|": 5,
    "^": 6,
    "&": 7,
    "<<": 8, ">>": 8,
    "+": 9, "-": 9,
    "*": 10, "/": 10, "%": 10,
    "**": 11,
}
RIGHT_ASSOC = {"**"}
ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}


class Parser:
    def __init__(self, tokens: list[Token], filename: str = "<input>", src: str = ""):
        self.toks = tokens
        self.pos = 0
        self.filename = filename
        self.src = src

    # -- token helpers ----------------------------------------------------
    def peek(self, k: int = 0) -> Token:
        j = self.pos + k
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def at(self, kind: str, value=None) -> bool:
        t = self.peek()
        return t.kind == kind and (value is None or t.value == value)

    def at_op(self, value: str) -> bool:
        return self.at("OP", value)

    def at_kw(self, value: str) -> bool:
        return self.at("KW", value)

    def advance(self) -> Token:
        t = self.peek()
        if self.pos < len(self.toks) - 1:
            self.pos += 1
        return t

    def eat(self, kind: str, value=None):
        if self.at(kind, value):
            return self.advance()
        return None

    def eat_op(self, value: str):
        return self.eat("OP", value)

    def eat_kw(self, value: str):
        return self.eat("KW", value)

    def error(self, msg: str, hint: str = "", tok: Token | None = None):
        t = tok or self.peek()
        return ParseError(msg, t.line, t.col, hint)

    def expect(self, kind: str, value=None, what: str = "") -> Token:
        if self.at(kind, value):
            return self.advance()
        got = self.peek()
        shown = got.value if got.value is not None else got.kind
        expected = value if value is not None else kind
        raise self.error(f"expected {what or expected!r} but found {shown!r}")

    def expect_op(self, value: str) -> Token:
        return self.expect("OP", value)

    def expect_angle(self) -> Token:
        t = self.peek()
        if t.kind == "OP" and t.value == ">>":
            self.toks[self.pos] = Token("OP", ">", t.line, t.col)
            self.toks.insert(self.pos + 1, Token("OP", ">", t.line, t.col + 1))
            return self.advance()
        if t.kind == "OP" and t.value == ">=":
            self.toks[self.pos] = Token("OP", ">", t.line, t.col)
            self.toks.insert(self.pos + 1, Token("OP", "=", t.line, t.col + 1))
            return self.advance()
        return self.expect_op(">")

    def expect_name(self, what: str = "a name") -> Token:
        return self.expect("NAME", None, what)

    def expect_newline(self, expr=None):
        if self.at("NEWLINE"):
            self.advance()
        elif self.at("EOF") or self.at("DEDENT"):
            pass
        elif expr is not None and _ends_with_block(expr):
            pass
        else:
            raise self.error("expected end of line")

    # -- program ----------------------------------------------------------
    def parse_program(self) -> ast.Program:
        prog = ast.Program()
        while not self.at("EOF"):
            if self.at("NEWLINE") or self.at("DEDENT"):
                self.advance()
                continue
            d = self.parse_decl()
            if d is not None:
                prog.decls.append(d)
        return prog

    def parse_decl(self):
        t = self.peek()
        if t.kind == "OP" and t.value == "@":
            self.advance()
            attr = self.expect_name("attribute name")
            args = []
            if self.eat_op("("):
                while not self.at_op(")"):
                    args.append(self.advance().value)
                    self.eat_op(",")
                self.expect_op(")")
            self.expect_newline()
            if not self.at_kw("struct") and not self.at_kw("enum"):
                raise self.error("attribute must precede a struct or enum declaration")
            decl = self.parse_decl()
            if attr.value == "repr" and args and str(args[0]).lower() == "c":
                decl.repr_c = True
            elif attr.value != "repr":
                raise self.error(f"unknown attribute @{attr.value}")
            return decl

        if self.at_kw("pub"):
            self.advance()
            d = self.parse_decl()
            d.is_public = True
            return d
        if self.at_kw("import"):
            self.advance()
            if self.at("STRING"):
                tok = self.advance()
                segs = tok.value
                if len(segs) != 1 or segs[0].kind != "lit":
                    raise self.error("import path must be a plain string", tok=tok)
                path = segs[0].text
            else:
                parts = [self.expect_name("module path").value]
                while self.eat_op("."):
                    parts.append(self.expect_name("module path").value)
                path = ".".join(parts)
            alias = None
            if self.eat_kw("as"):
                alias = self.expect_name("alias").value
            self.expect_newline()
            return ast.Import(path=path, alias=alias, line=t.line, col=t.col)
        if self.at_kw("fn"):
            d = self.parse_fn()
            return d
        if self.at_kw("extern"):
            self.advance()
            if not self.at_kw("fn"):
                raise self.error("expected 'fn' after 'extern'")
            d = self.parse_fn(extern=True)
            return d
        if self.at_kw("struct"):
            return self.parse_struct()
        if self.at_kw("enum"):
            return self.parse_enum()
        if self.at_kw("impl"):
            return self.parse_impl()
        if self.at_kw("trait"):
            return self.parse_trait()
        raise self.error(f"expected a declaration, found {self.peek().value!r}")

    def parse_fn(self, extern: bool = False) -> ast.FnDecl:
        line, col = self.peek().line, self.peek().col
        self.expect("KW", "fn")
        name = self.expect_name("function name").value
        generics, bounds = self.parse_generics_fn()
        params = self.parse_params()
        ret = None
        if self.eat_op("->"):
            ret = self.parse_type()
        fn = ast.FnDecl(name=name, generics=generics, bounds=bounds,
                        params=params, ret=ret,
                        is_extern=extern, line=line, col=col)
        if extern:
            fn.body = []
            self.expect_newline()
            return fn
        self.expect_op(":")
        fn.body = self.parse_block()
        return fn

    def parse_generics_fn(self):
        names = []
        bounds = {}
        if self.eat_op("<"):
            while True:
                name = self.expect_name("type parameter").value
                names.append(name)
                if self.eat_op(":"):
                    ts = [self.expect_name("trait name").value]
                    while self.eat_op("+"):
                        ts.append(self.expect_name("trait name").value)
                    bounds[name] = ts
                if not self.eat_op(","):
                    break
            self.expect_angle()
        return names, bounds

    def parse_generics_opt(self) -> list[str]:
        names = []
        if self.eat_op("<"):
            while True:
                names.append(self.expect_name("type parameter").value)
                if not self.eat_op(","):
                    break
            self.expect_angle()
        return names

    def parse_params(self) -> list[ast.Param]:
        params = []
        self.expect_op("(")
        while not self.at_op(")"):
            mode = None
            line, col = self.peek().line, self.peek().col
            if self.eat_op("&"):
                mode = "&mut" if self.eat_kw("mut") else "&"
            ty = None
            if mode and self.at_kw("self"):
                name = self.advance().value
            else:
                name = self.expect_name("parameter name").value
                if self.eat_op(":"):
                    ty = self.parse_type()
                elif name != "self":
                    raise self.error(f"parameter '{name}' needs a type annotation")
            params.append(ast.Param(name=name, ty=ty, mode=mode, line=line, col=col))
            if not self.eat_op(","):
                break
        self.expect_op(")")
        return params

    def parse_struct(self) -> ast.StructDecl:
        line, col = self.peek().line, self.peek().col
        self.expect("KW", "struct")
        name = self.expect_name("struct name").value
        generics = self.parse_generics_opt()
        fields = []
        self.expect_op(":")
        self.expect_newline()
        self.expect("INDENT")
        while not self.at("DEDENT"):
            if self.at("NEWLINE"):
                self.advance()
                continue
            fname = self.expect_name("field name").value
            self.expect_op(":")
            fty = self.parse_type()
            self.eat_op(",")
            self.expect_newline()
            fields.append((fname, fty))
        self.expect("DEDENT")
        return ast.StructDecl(name=name, generics=generics, fields=fields, line=line, col=col)

    def parse_enum(self) -> ast.EnumDecl:
        line, col = self.peek().line, self.peek().col
        self.expect("KW", "enum")
        name = self.expect_name("enum name").value
        generics = self.parse_generics_opt()
        variants = []
        self.expect_op(":")
        self.expect_newline()
        self.expect("INDENT")
        while not self.at("DEDENT"):
            if self.at("NEWLINE"):
                self.advance()
                continue
            vname = self.expect_name("variant name").value
            tys = []
            if self.eat_op("("):
                while not self.at_op(")"):
                    tys.append(self.parse_type())
                    if not self.eat_op(","):
                        break
                self.expect_op(")")
            self.expect_newline()
            variants.append((vname, tys))
        self.expect("DEDENT")
        return ast.EnumDecl(name=name, generics=generics, variants=variants, line=line, col=col)

    def parse_trait(self) -> ast.TraitDecl:
        line, col = self.peek().line, self.peek().col
        self.expect("KW", "trait")
        name = self.expect_name("trait name").value
        generics = self.parse_generics_opt()
        self.expect_op(":")
        supertraits = []
        if self.at("NAME"):
            while True:
                supertraits.append(self.expect_name("supertrait name").value)
                if not self.eat_op(","):
                    break
            self.expect_op(":")
        self.expect_newline()
        self.expect("INDENT", None, "trait methods")
        methods = []
        while not self.at("DEDENT"):
            if self.at("NEWLINE"):
                self.advance()
                continue
            if not self.at_kw("fn"):
                raise self.error("only method signatures may appear in a trait")
            methods.append(self.parse_trait_fn())
        self.expect("DEDENT")
        if not methods:
            raise self.error("a trait needs at least one method")
        return ast.TraitDecl(name=name, generics=generics,
                             supertraits=supertraits, methods=methods,
                             line=line, col=col)

    def parse_trait_fn(self) -> ast.FnDecl:
        line, col = self.peek().line, self.peek().col
        self.expect("KW", "fn")
        name = self.expect_name("method name").value
        generics, bounds = self.parse_generics_fn()
        params = self.parse_params()
        ret = None
        if self.eat_op("->"):
            ret = self.parse_type()
        body = []
        if self.eat_op(":"):
            body = self.parse_block()
        else:
            self.expect_newline()
        return ast.FnDecl(name=name, generics=generics, bounds=bounds,
                          params=params, ret=ret, body=body, line=line, col=col)

    def parse_impl(self) -> ast.ImplBlock:
        line, col = self.peek().line, self.peek().col
        self.expect("KW", "impl")
        generics = self.parse_generics_opt()
        first = self.expect_name("type or trait name").value
        # skip generic args on the first name
        if self.at_op("<"):
            self.parse_generics_opt()
        trait_name = None
        tyname = first
        if self.eat_kw("for"):
            trait_name = first
            tyname = self.expect_name("type name").value
            if self.at_op("<"):
                self.parse_generics_opt()
        self.expect_op(":")
        self.expect_newline()
        self.expect("INDENT")
        methods = []
        while not self.at("DEDENT"):
            if self.at("NEWLINE"):
                self.advance()
                continue
            if self.at_kw("pass"):
                self.advance()
                self.expect_newline()
                continue
            if not self.at_kw("fn"):
                raise self.error("only functions or `pass` may appear in an impl block")
            methods.append(self.parse_fn())
        self.expect("DEDENT")
        return ast.ImplBlock(type_name=tyname, trait_name=trait_name,
                             generics=generics, methods=methods,
                             line=line, col=col)

    # -- types ------------------------------------------------------------
    def parse_type(self):
        t = self.peek()
        if t.kind == "OP" and t.value == "&":
            self.advance()
            mut = bool(self.eat_kw("mut"))
            inner = self.parse_type()
            return ast.TyRef(mut=mut, inner=inner, line=t.line, col=t.col)
        if t.kind == "OP" and t.value == "*":
            self.advance()
            mut = bool(self.eat_kw("mut"))
            inner = self.parse_type()
            return ast.TyPtr(mut=mut, inner=inner, line=t.line, col=t.col)
        if t.kind == "OP" and t.value == "[":
            self.advance()
            inner = self.parse_type()
            self.expect_op("]")
            return ast.TySlice(inner=inner, line=t.line, col=t.col)
        if t.kind == "OP" and t.value == "(":
            self.advance()
            self.expect_op(")")
            return ast.TyUnit(line=t.line, col=t.col)
        if t.kind == "KW" and t.value == "fn":
            self.advance()
            self.expect_op("(")
            params = []
            while not self.at_op(")"):
                params.append(self.parse_type())
                if not self.eat_op(","):
                    break
            self.expect_op(")")
            ret = ast.TyUnit(line=t.line, col=t.col)
            if self.eat_op("->"):
                ret = self.parse_type()
            return ast.TyFn(params=params, ret=ret, line=t.line, col=t.col)
        if t.kind == "NAME" and t.value == "dyn":
            self.advance()
            name = self.expect_name("trait name").value
            return ast.TyDyn(trait=name, line=t.line, col=t.col)
        if t.kind == "NAME":
            self.advance()
            args = []
            if self.at_op("<"):
                self.advance()
                while True:
                    args.append(self.parse_type())
                    if not self.eat_op(","):
                        break
                self.expect_angle()
            return ast.TyName(name=t.value, args=args, line=t.line, col=t.col)
        raise self.error(f"expected a type, found {t.value!r}")

    # -- statements -------------------------------------------------------
    def parse_block(self) -> list:
        self.expect_newline()
        self.expect("INDENT", None, "an indented block")
        stmts = []
        while not self.at("DEDENT"):
            if self.at("NEWLINE"):
                self.advance()
                continue
            stmts.append(self.parse_stmt())
        self.expect("DEDENT")
        if not stmts:
            raise self.error("empty block")
        return stmts

    def parse_stmt(self):
        t = self.peek()
        line, col = t.line, t.col

        if self.at_kw("let") or self.at_kw("var") or self.at_kw("const"):
            kw = self.advance().value
            mutable = kw == "var"
            name = self.expect_name("binding name").value
            ty = None
            if self.eat_op(":"):
                ty = self.parse_type()
            init = None
            if self.eat_op("="):
                init = self.parse_expr()
            self.expect_newline(init)
            return ast.Let(name=name, mutable=mutable, ty=ty, init=init, line=line, col=col)

        if self.at_kw("if"):
            return self.parse_if()

        if self.at_kw("while"):
            self.advance()
            cond = self.parse_expr()
            self.expect_op(":")
            body = self.parse_block()
            return ast.While(cond=cond, body=body, line=line, col=col)

        if self.at_kw("for"):
            self.advance()
            name = self.expect_name("loop variable").value
            if not self.eat_kw("in"):
                raise self.error("expected 'in' in for loop")
            it = self.parse_expr()
            self.expect_op(":")
            body = self.parse_block()
            return ast.For(name=name, iter=it, body=body, line=line, col=col)

        if self.at_kw("return"):
            self.advance()
            val = None
            if not self.at("NEWLINE"):
                val = self.parse_expr()
            self.expect_newline(val)
            return ast.Return(value=val, line=line, col=col)

        if self.at_kw("pass"):
            self.advance()
            self.expect_newline()
            return ast.Pass(line=line, col=col)
        if self.at_kw("break"):
            self.advance()
            self.expect_newline()
            return ast.Break(line=line, col=col)
        if self.at_kw("continue"):
            self.advance()
            self.expect_newline()
            return ast.Continue(line=line, col=col)

        if self.at_kw("unsafe"):
            self.advance()
            self.expect_op(":")
            body = self.parse_block()
            return ast.UnsafeStmt(body=body, line=line, col=col)

        if self.at_kw("fn"):
            raise self.error("nested functions are not supported",
                             "lift the function to the top level")

        expr = self.parse_expr()
        if self.peek().kind == "OP" and self.peek().value in ASSIGN_OPS:
            op = self.advance().value
            value = self.parse_expr()
            self.expect_newline(value)
            if not isinstance(expr, (ast.Name, ast.Field, ast.Index, ast.Deref)):
                raise self.error("invalid assignment target", tok=t)
            return ast.Assign(target=expr, op=op, value=value, line=line, col=col)
        self.expect_newline(expr)
        return ast.ExprStmt(expr=expr, line=line, col=col)

    def parse_if(self):
        line, col = self.peek().line, self.peek().col
        self.expect("KW", "if")
        cond = self.parse_expr()
        self.expect_op(":")
        then_body = self.parse_block()
        elifs = []
        else_body = None
        while self.at_kw("elif"):
            self.advance()
            c = self.parse_expr()
            self.expect_op(":")
            b = self.parse_block()
            elifs.append((c, b))
        if self.at_kw("else"):
            self.advance()
            self.expect_op(":")
            else_body = self.parse_block()
        return ast.If(cond=cond, then_body=then_body, elifs=elifs,
                      else_body=else_body, line=line, col=col)

    # -- expressions ------------------------------------------------------
    def parse_expr(self) -> ast.Expr:
        left = self.parse_binary(0)
        if self.at_op("..") or self.at_op("..="):
            op = self.advance().value
            end = None
            if not (self.at("NEWLINE") or self.at_op(":") or self.at_op(")") or self.at_op("]")):
                end = self.parse_binary(0)
            return ast.Range(start=left, end=end, inclusive=op == "..=",
                             line=left.line, col=left.col)
        return left

    def parse_binary(self, min_prec: int) -> ast.Expr:
        left = self.parse_unary()
        while True:
            t = self.peek()
            if t.kind != "OP" or t.value not in BINARY_PREC:
                break
            prec = BINARY_PREC[t.value]
            if prec < min_prec:
                break
            op = self.advance().value
            next_min = prec if op in RIGHT_ASSOC else prec + 1
            right = self.parse_binary(next_min)
            left = ast.Binary(op=op, left=left, right=right, line=left.line, col=left.col)
        return left

    def parse_unary(self) -> ast.Expr:
        t = self.peek()
        expr = self.parse_prefix()
        while self.at_kw("as"):
            self.advance()
            target = self.parse_type()
            expr = ast.Cast(base=expr, target=target, line=t.line, col=t.col)
        return expr

    def parse_prefix(self) -> ast.Expr:
        t = self.peek()
        if t.kind == "OP" and t.value in ("|", "||"):
            return self.parse_lambda()
        if t.kind == "OP" and t.value in ("-", "!", "~"):
            self.advance()
            operand = self.parse_prefix()
            return ast.Unary(op=t.value, operand=operand, line=t.line, col=t.col)
        if t.kind == "OP" and t.value == "&":
            self.advance()
            mut = bool(self.eat_kw("mut"))
            operand = self.parse_prefix()
            return ast.Ref(mut=mut, base=operand, line=t.line, col=t.col)
        if t.kind == "OP" and t.value == "*":
            self.advance()
            operand = self.parse_prefix()
            return ast.Deref(base=operand, line=t.line, col=t.col)
        return self.parse_postfix()

    def parse_lambda(self) -> ast.Expr:
        t = self.peek()
        params = []
        if self.eat_op("||"):
            pass
        else:
            self.expect_op("|")
            while not self.at_op("|"):
                line, col = self.peek().line, self.peek().col
                name = self.expect_name("closure parameter").value
                self.expect_op(":")
                ty = self.parse_type()
                params.append(ast.Param(name=name, ty=ty, mode=None,
                                        line=line, col=col))
                if not self.eat_op(","):
                    break
            self.expect_op("|")
        body = self.parse_expr()
        return ast.Lambda(params=params, body=body, line=t.line, col=t.col)

    def parse_postfix(self) -> ast.Expr:
        expr = self.parse_primary()
        while True:
            if self.at_op("("):
                line, col = self.peek().line, self.peek().col
                args = self.parse_args()
                expr = ast.Call(callee=expr, args=args, line=line, col=col)
            elif self.at_op("["):
                line, col = self.peek().line, self.peek().col
                self.advance()
                idx = self.parse_expr()
                self.expect_op("]")
                expr = ast.Index(base=expr, index=idx, line=line, col=col)
            elif self.at_op("."):
                line, col = self.peek().line, self.peek().col
                self.advance()
                fname = self.expect_name("field or method name").value
                expr = ast.Field(base=expr, field=fname, line=line, col=col)
            elif self.at_op("?"):
                line, col = self.peek().line, self.peek().col
                self.advance()
                expr = ast.Try(base=expr, line=line, col=col)
            else:
                break
        return expr

    def parse_args(self) -> list:
        args = []
        self.expect_op("(")
        while not self.at_op(")"):
            args.append(self.parse_expr())
            if not self.eat_op(","):
                break
        self.expect_op(")")
        return args

    def parse_primary(self) -> ast.Expr:
        t = self.peek()
        if t.kind == "INT":
            self.advance()
            return ast.IntLit(value=t.value, line=t.line, col=t.col)
        if t.kind == "FLOAT":
            self.advance()
            return ast.FloatLit(value=t.value, line=t.line, col=t.col)
        if t.kind == "BOOL":
            self.advance()
            return ast.BoolLit(value=t.value, line=t.line, col=t.col)
        if t.kind == "CHAR":
            self.advance()
            return ast.CharLit(value=t.value, line=t.line, col=t.col)
        if t.kind == "STRING":
            self.advance()
            return self.build_string(t)
        if t.kind == "NAME":
            self.advance()
            if self.at_op("{"):
                return self.parse_struct_lit(t)
            return ast.Name(name=t.value, line=t.line, col=t.col)
        if t.kind == "KW" and t.value == "self":
            self.advance()
            return ast.Name(name="self", line=t.line, col=t.col)
        if t.kind == "OP" and t.value == "(":
            self.advance()
            if self.eat_op(")"):
                return ast.UnitLit(line=t.line, col=t.col)
            e = self.parse_expr()
            self.expect_op(")")
            return e
        if t.kind == "OP" and t.value == "[":
            self.advance()
            items = []
            while not self.at_op("]"):
                items.append(self.parse_expr())
                if not self.eat_op(","):
                    break
            self.expect_op("]")
            return ast.ListLit(items=items, line=t.line, col=t.col)
        if t.kind == "KW" and t.value == "match":
            return self.parse_match()
        if t.kind == "KW" and t.value == "asm":
            return self.parse_asm()
        if t.kind == "OP" and t.value == "_":
            raise self.error("'_' is only valid in a pattern")
        raise self.error(f"expected an expression, found {t.value!r}")

    def build_string(self, tok: Token) -> ast.StrLit:
        node = ast.StrLit(segs=tok.value, line=tok.line, col=tok.col)
        for seg in tok.value:
            if seg.kind == "expr":
                from .lexer import tokenize as _tok
                sub = Parser(_tok(seg.text, self.filename), self.filename, seg.text)
                seg.parts = [sub.parse_expr()]
                while sub.at("NEWLINE"):
                    sub.advance()
                if not sub.at("EOF"):
                    raise self.error("could not parse interpolated expression", tok=tok)
        return node

    def parse_struct_lit(self, name_tok: Token) -> ast.StructLit:
        self.expect_op("{")
        fields = []
        while not self.at_op("}"):
            fname = self.expect_name("field name").value
            if self.eat_op(":"):
                val = self.parse_expr()
            else:
                val = ast.Name(name=fname, line=name_tok.line, col=name_tok.col)
            fields.append((fname, val))
            if not self.eat_op(","):
                break
        self.expect_op("}")
        return ast.StructLit(name=name_tok.value, fields=fields,
                             line=name_tok.line, col=name_tok.col)

    def parse_match(self) -> ast.Match:
        line, col = self.peek().line, self.peek().col
        self.expect("KW", "match")
        scrut = self.parse_expr()
        self.expect_op(":")
        self.expect_newline()
        self.expect("INDENT", None, "match arms")
        arms = []
        while not self.at("DEDENT"):
            if self.at("NEWLINE"):
                self.advance()
                continue
            pat = self.parse_pattern()
            if not self.eat_op("->"):
                raise self.error("expected '->' after match pattern")
            body = self.parse_expr()
            self.expect_newline()
            arms.append(ast.MatchArm(pattern=pat, body=body, line=pat.line, col=pat.col))
        self.expect("DEDENT")
        if not arms:
            raise self.error("match needs at least one arm")
        return ast.Match(scrutinee=scrut, arms=arms, line=line, col=col)

    def parse_pattern(self):
        t = self.peek()
        if t.kind == "NAME" and t.value == "_":
            self.advance()
            return ast.PatWild(line=t.line, col=t.col)
        if t.kind == "OP" and t.value == "-" and self.peek(1).kind in ("INT", "FLOAT"):
            self.advance()
            lit = self.advance()
            return ast.PatLit(value=-lit.value, line=t.line, col=t.col)
        if t.kind in ("INT", "FLOAT", "BOOL", "CHAR"):
            self.advance()
            return ast.PatLit(value=t.value, line=t.line, col=t.col)
        if t.kind == "STRING":
            self.advance()
            segs = t.value
            if len(segs) != 1 or segs[0].kind != "lit":
                raise self.error("string patterns cannot be interpolated", tok=t)
            return ast.PatLit(value=segs[0].text, line=t.line, col=t.col)
        if t.kind == "NAME":
            self.advance()
            enum_name = None
            vname = t.value
            if self.eat_op("."):
                enum_name = t.value
                vname = self.expect_name("variant name").value
            args = []
            if self.eat_op("("):
                while not self.at_op(")"):
                    args.append(self.parse_pattern())
                    if not self.eat_op(","):
                        break
                self.expect_op(")")
                return ast.PatVariant(enum=enum_name, variant=vname, args=args,
                                      line=t.line, col=t.col)
            if enum_name is not None:
                return ast.PatVariant(enum=enum_name, variant=vname, args=[],
                                      line=t.line, col=t.col)
            if vname and vname[0].isupper():
                return ast.PatVariant(enum=None, variant=vname, args=[],
                                      line=t.line, col=t.col)
            return ast.PatBind(name=vname, line=t.line, col=t.col)
        raise self.error(f"invalid pattern starting with {t.value!r}")

    def parse_asm(self) -> ast.AsmExpr:
        t = self.expect("KW", "asm")
        self.expect_op("(")
        tmpl = self.expect("STRING", None, "an assembly template string")
        segs = tmpl.value
        if len(segs) != 1 or segs[0].kind != "lit":
            raise self.error("assembly template must be a plain string", tok=tmpl)
        operands = []
        while self.eat_op(","):
            operands.append(self.parse_expr())
        self.expect_op(")")
        return ast.AsmExpr(template=segs[0].text, operands=operands,
                           line=t.line, col=t.col)


def _ends_with_block(e) -> bool:
    if isinstance(e, ast.Match):
        return True
    if isinstance(e, (ast.Ref, ast.Deref, ast.Unary, ast.Cast)):
        return _ends_with_block(getattr(e, "base", None) or getattr(e, "operand", None))
    if isinstance(e, ast.Binary):
        return _ends_with_block(e.right)
    if isinstance(e, ast.Call):
        return bool(e.args) and _ends_with_block(e.args[-1])
    return False


def parse(src: str, filename: str = "<input>") -> ast.Program:
    toks = tokenize(src, filename)
    return Parser(toks, filename, src).parse_program()
