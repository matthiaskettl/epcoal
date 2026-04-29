#!/bin/python3

import sys
import argparse
import logging
import re
from pathlib import Path
from typing import AnyStr

sys.path.append(str((Path(__file__).absolute().parent / "lib" / "pip")))


from pycparserext.ext_c_parser import GnuCParser, FuncDeclExt
from pycparserext.ext_c_generator import GnuCGenerator
from pycparser import c_ast

logger = logging.getLogger(__name__)


def blank(pattern: str, string: str, add_newline=False):
    """
    Blanks out the given pattern in the string.
    :param pattern: The pattern to be blanked out.
    :param string: the string to be modified.
    :param add_newline: Whether to add a newline at the end of the string.
    :return: The modified string.
    """
    res = re.search(pattern, string)
    if not res:
        return string
    matchsize = len(res.group(0))
    blanked_line = re.sub(pattern, " " * matchsize, string)
    if add_newline:
        blanked_line += "\n"
    return blanked_line


def rewrite_builtin_va_arg(content: str) -> str:
    """Rewrite `__builtin_va_arg(expr, type)` into a parser-friendly form.

    Handles nested parentheses in both arguments, e.g.:
    `__builtin_va_arg(p, __typeof__(on_off->optarg))`.
    """

    token = "__builtin_va_arg"
    n = len(content)
    i = 0
    out = []
    replaced = 0

    def _is_ident_char(ch: str) -> bool:
        return ch.isalnum() or ch == "_"

    while i < n:
        start = content.find(token, i)
        if start == -1:
            out.append(content[i:])
            break

        out.append(content[i:start])

        # Not part of a larger identifier.
        if start > 0 and _is_ident_char(content[start - 1]):
            out.append(content[start])
            i = start + 1
            continue

        j = start + len(token)
        if j < n and _is_ident_char(content[j]):
            out.append(content[start])
            i = start + 1
            continue

        while j < n and content[j].isspace():
            j += 1

        if j >= n or content[j] != "(":
            out.append(content[start])
            i = start + 1
            continue

        # Parse first argument until top-level comma.
        arg1_start = j + 1
        k = arg1_start
        depth = 0
        in_string = None
        escaped = False
        comma_pos = -1

        while k < n:
            ch = content[k]
            if in_string is not None:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == in_string:
                    in_string = None
            else:
                if ch == '"' or ch == "'":
                    in_string = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif ch == "," and depth == 0:
                    comma_pos = k
                    break
            k += 1

        if comma_pos == -1:
            out.append(content[start])
            i = start + 1
            continue

        arg1 = content[arg1_start:comma_pos].strip()

        # Parse second argument until matching ')' of builtin call.
        arg2_start = comma_pos + 1
        while arg2_start < n and content[arg2_start].isspace():
            arg2_start += 1

        k = arg2_start
        depth = 0
        in_string = None
        escaped = False
        close_pos = -1

        while k < n:
            ch = content[k]
            if in_string is not None:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == in_string:
                    in_string = None
            else:
                if ch == '"' or ch == "'":
                    in_string = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        close_pos = k
                        break
                    depth -= 1
            k += 1

        if close_pos == -1:
            out.append(content[start])
            i = start + 1
            continue

        arg2 = content[arg2_start:close_pos].strip()
        out.append(f"(({arg2}) __va_arg({arg1}))")
        replaced += 1
        i = close_pos + 1

    if replaced == 0:
        logger.warning(
            "No __builtin_va_arg occurrences were rewritten. This may cause issues if the input code uses this builtin."
        )

    return "".join(out)


def rewrite_unsupported_builtins(content: str) -> str:
    content = re.sub(r"\b__builtin_unreachable\s*\(\s*\)", "abort()", content)
    content = rewrite_builtin_va_arg(content)
    return content


def rewrite_cproblem_pycparserext(content: str) -> str:
    """
    Rewrites the given content to be compatible with pycparserext.
    This is necessary, since some special GNU C extension are not supported by pycparserext
    :param content: The content to be rewritten.
    :return: The rewritten content.
    """
    # TODO: This should be rewritten to be used inside pycparserext
    prepared_content = ""
    for line in [c + "\n" for c in content.split("\n")]:
        line = re.sub(r"__signed__", "  signed  ", line)
        line = blank(r"__attribute__\s*\(\s*\(\s*__always_inline__\s*\)\s*\)", line)
        line = re.sub(
            r"\*(.*) __attribute__\s*\(\s*\(\s*__aligned__"
            r"\s*\([\sa-zA-Z0-9()|<>+*-]*\)\s*\)\s*\)\s*",
            r"*\1",
            line,
        )
        line = blank(r"__extension__", line)
        prepared_content += line
    return prepared_content


def remove_comments(content: str):
    """
    Removes C/c++ style comments from the given content.
    :param content: The content to remove comments from.
    :return: The content without C/C++ style comments
    """
    in_cxx_comment = False
    prepared_content = ""
    line: AnyStr  # pylance runs into performance issues without this hint
    for line in [c + "\n" for c in content.split("\n")]:
        # remove C++-style comments
        if in_cxx_comment:
            if re.search(r"\*/", line):
                line = blank(r".*\*/", line)
                in_cxx_comment = False
            else:
                line = " " * (len(line) - 1) + "\n"
        else:
            line = blank(r"/\*.*?\*/", line)
        if re.search(r"/\*", line):
            line = blank(r"/\*.*", line)
            in_cxx_comment = True
        line = blank(r"//[^\r\n]*\n", line, add_newline=True)
        prepared_content += line
    return prepared_content


def blank_asm_volatile_with_brackets(content: str) -> str:
    """Blank out `__asm__ volatile (...) ;` calls that contain `[` or `]` inside `(...)`.

    The match may span multiple lines. Newlines are preserved so line numbers stay stable.
    """
    chars = list(content)
    n = len(content)
    i = 0

    while i < n:
        if not content.startswith("__asm__", i):
            i += 1
            continue

        # Avoid matching the middle of a longer identifier.
        if i > 0 and (content[i - 1].isalnum() or content[i - 1] == "_"):
            i += 1
            continue

        j = i + len("__asm__")
        while j < n and content[j].isspace():
            j += 1

        if not content.startswith("volatile", j):
            i += 1
            continue

        j += len("volatile")
        while j < n and content[j].isspace():
            j += 1

        if j >= n or content[j] != "(":
            i += 1
            continue

        # Find the matching ')' while handling nested parens and string/char literals.
        depth = 1
        k = j + 1
        in_string = None
        escaped = False
        has_square_bracket = False

        while k < n and depth > 0:
            ch = content[k]
            if in_string is not None:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == in_string:
                    in_string = None
            else:
                if ch == '"' or ch == "'":
                    in_string = ch
                elif ch == "[" or ch == "]":
                    has_square_bracket = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
            k += 1

        # Unbalanced parentheses: skip this candidate.
        if depth != 0:
            i += 1
            continue

        m = k
        while m < n and content[m].isspace():
            m += 1

        # Only handle function-style asm statements that end with `);`.
        if m >= n or content[m] != ";":
            i += 1
            continue

        if has_square_bracket:
            for pos in range(i, m + 1):
                if chars[pos] != "\n":
                    chars[pos] = " "

        i = m + 1

    return "".join(chars)


def blank_gnu_attributes(content: str) -> str:
    """Blank out `__attribute__((...))` and `__attribute (...)` blocks.

    Some generated kernel translation units contain complex/multiline attributes
    (e.g., section/alignment markers on `_ddebug` descriptors) that are not
    handled reliably by the parser. Replacing the attribute text with spaces
    keeps coordinates stable while removing parse blockers.
    """

    def _is_ident_char(ch: str) -> bool:
        return ch.isalnum() or ch == "_"

    chars = list(content)
    n = len(content)
    i = 0

    while i < n:
        if content.startswith("__attribute__", i):
            token = "__attribute__"
        elif content.startswith("__attribute", i):
            token = "__attribute"
        else:
            i += 1
            continue

        # Avoid matching in the middle of longer identifiers.
        if i > 0 and _is_ident_char(content[i - 1]):
            i += 1
            continue

        j = i + len(token)
        if j < n and _is_ident_char(content[j]):
            i += 1
            continue

        while j < n and content[j].isspace():
            j += 1

        if j >= n or content[j] != "(":
            i += 1
            continue

        depth = 1
        k = j + 1
        in_string = None
        escaped = False

        while k < n and depth > 0:
            ch = content[k]
            if in_string is not None:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == in_string:
                    in_string = None
            else:
                if ch == '"' or ch == "'":
                    in_string = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
            k += 1

        # If malformed, skip and continue scanning.
        if depth != 0:
            i += 1
            continue

        for pos in range(i, k):
            if chars[pos] != "\n":
                chars[pos] = " "

        i = k

    return "".join(chars)


def ensure_asm_volatile_semicolons(content: str) -> str:
    """Ensure `__asm__ (...)` statements are followed by `;`.

    Handles multiline asm blocks and optional `volatile`/`__volatile__` qualifiers.
    """

    def _is_ident_char(ch: str) -> bool:
        return ch.isalnum() or ch == "_"

    n = len(content)
    i = 0
    out = []

    while i < n:
        start = content.find("__asm__", i)
        if start == -1:
            out.append(content[i:])
            break

        out.append(content[i:start])

        # Reject identifier-contained matches.
        if start > 0 and _is_ident_char(content[start - 1]):
            out.append(content[start])
            i = start + 1
            continue

        j = start + len("__asm__")
        if j < n and _is_ident_char(content[j]):
            out.append(content[start])
            i = start + 1
            continue

        while j < n and content[j].isspace():
            j += 1

        # Support `__asm__ (...)`, `__asm__ volatile (...)`, and `__asm__ __volatile__ (...)`.
        if content.startswith("volatile", j):
            q = j + len("volatile")
            if q < n and _is_ident_char(content[q]):
                out.append(content[start])
                i = start + 1
                continue
            j = q
            while j < n and content[j].isspace():
                j += 1
        elif content.startswith("__volatile__", j):
            q = j + len("__volatile__")
            if q < n and _is_ident_char(content[q]):
                out.append(content[start])
                i = start + 1
                continue
            j = q
            while j < n and content[j].isspace():
                j += 1

        if j >= n or content[j] != "(":
            out.append(content[start])
            i = start + 1
            continue

        # Find matching ')' with support for nested parens and string/char literals.
        depth = 1
        k = j + 1
        in_string = None
        escaped = False

        while k < n and depth > 0:
            ch = content[k]
            if in_string is not None:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == in_string:
                    in_string = None
            else:
                if ch == '"' or ch == "'":
                    in_string = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
            k += 1

        # If malformed, keep content untouched from this position.
        if depth != 0:
            out.append(content[start])
            i = start + 1
            continue

        # k points to char right after matching ')'.
        p = k
        while p < n and content[p].isspace():
            p += 1

        if p < n and content[p] == ";":
            out.append(content[start : p + 1])
            i = p + 1
        else:
            # Insert semicolon immediately after ')', keep following whitespace/tokens as-is.
            out.append(content[start:k])
            out.append(";")
            out.append(content[k:p])
            i = p

    return "".join(out)


class ScalarCharInitListTransformer(c_ast.NodeVisitor):
    """Turn scalar char declarations with multi-item init lists into arrays.

    Some benchmark-generated `.modinfo` declarations are emitted as scalar
    `const char` declarations with brace-enclosed character lists. That shape
    is not valid for downstream parsers, but it is semantically an array of
    bytes, so we can recover a proper array dimension from the initializer.
    """

    @staticmethod
    def _is_char_type(decl_type):
        if not isinstance(decl_type, c_ast.TypeDecl):
            return False
        inner_type = decl_type.type
        if not isinstance(inner_type, c_ast.IdentifierType):
            return False
        return inner_type.names in (["char"], ["signed", "char"], ["unsigned", "char"])

    def visit_Decl(self, node):
        if (
            isinstance(node.init, c_ast.InitList)
            and self._is_char_type(node.type)
            and node.init.exprs is not None
            and len(node.init.exprs) > 1
        ):
            dim = c_ast.Constant(type="int", value=str(len(node.init.exprs)))
            node.type = c_ast.ArrayDecl(type=node.type, dim=dim, dim_quals=[])

        self.generic_visit(node)


def _is_func_decl_type(node_type):
    return isinstance(node_type, (c_ast.FuncDecl, FuncDeclExt))


def _is_func_decl_node(node):
    return isinstance(node, c_ast.Decl) and _is_func_decl_type(node.type)


class ReachErrorTransformer(c_ast.NodeVisitor):
    """Remove reach_error declarations and replace calls with abort()."""

    def __init__(self):
        self.has_abort_decl = False
        self.reach_error_indices = []  # Track indices to remove

    def visit_FileAST(self, node):
        # First pass: check for abort declaration and mark reach_error for removal
        for i, ext in enumerate(node.ext):
            if isinstance(ext, c_ast.Decl):
                if ext.name == "reach_error":
                    self.reach_error_indices.append(i)
                elif ext.name == "abort":
                    self.has_abort_decl = True
            elif isinstance(ext, c_ast.FuncDef):
                if ext.decl.name == "reach_error":
                    self.reach_error_indices.append(i)
                elif ext.decl.name == "abort":
                    self.has_abort_decl = True

        # Second pass: replace reach_error calls with abort
        for ext in node.ext:
            self.visit(ext)

        # Third pass: remove reach_error declarations (reverse order to maintain indices)
        for i in reversed(self.reach_error_indices):
            node.ext.pop(i)

        # Fourth pass: add abort declaration if not present
        if not self.has_abort_decl:
            abort_decl = self._create_abort_decl()
            node.ext.insert(0, abort_decl)

    def _create_abort_decl(self):
        """Create external declaration: void abort(void);"""
        return c_ast.Decl(
            name="abort",
            quals=[],
            align=[],
            storage=[],
            funcspec=[],
            type=c_ast.FuncDecl(
                args=None,
                type=c_ast.TypeDecl(
                    declname="abort",
                    quals=[],
                    align=[],
                    type=c_ast.IdentifierType(names=["void"]),
                ),
            ),
            init=None,
            bitsize=None,
        )

    def visit_FuncCall(self, node):
        # Replace reach_error() calls with abort()
        if isinstance(node.name, c_ast.ID) and node.name.name == "reach_error":
            node.name = c_ast.ID(name="abort")

        # Continue visiting children
        self.generic_visit(node)


class GlobalizeTransformer(c_ast.NodeVisitor):
    def __init__(self):
        self.generator = GnuCGenerator()

        self.current_func = None
        self.global_decls = []

        # scope stack: [{old_name: new_name}]
        self.scopes = [{}]

        # per-function counters
        self.counters = {}
        self.current_params = set()
        self.typedef_decls = []
        self._typedef_by_name = {}
        self._typedef_signature_by_name = {}

    # ---------- Scope ----------
    def push_scope(self):
        self.scopes.append({})

    def pop_scope(self):
        self.scopes.pop()

    def resolve(self, name):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return name

    # ---------- File ----------
    def visit_FileAST(self, node):
        for ext in node.ext:
            if isinstance(ext, c_ast.Typedef):
                self._record_typedef(ext)
                continue
            self.visit(ext)

    # ---------- Function ----------
    def visit_FuncDef(self, node):
        self.current_func = node.decl.name
        self.current_params = set()

        # Register function parameters so they are never globalized as locals.
        if isinstance(node.decl, c_ast.Decl) and _is_func_decl_type(node.decl.type):
            args = node.decl.type.args
            if args and args.params:
                for param in args.params:
                    if isinstance(param, c_ast.Decl) and param.name:
                        self.current_params.add(param.name)

        # K&R-style declarations may keep parameter declarations separately.
        if node.param_decls:
            for param in node.param_decls:
                if isinstance(param, c_ast.Decl) and param.name:
                    self.current_params.add(param.name)

        self.push_scope()
        for pname in self.current_params:
            self.scopes[-1][pname] = pname
        self.visit(node.body)
        self.pop_scope()

        self.current_func = None
        self.current_params = set()

    # ---------- Block ----------
    def visit_Compound(self, node):
        self.push_scope()

        new_block_items = []

        for stmt in node.block_items or []:
            if isinstance(stmt, c_ast.Typedef):
                # Hoist typedefs so globalized declarations that depend on them remain valid.
                self._record_typedef(stmt)
                continue

            # Handle variable declarations
            if isinstance(stmt, c_ast.Decl) and not _is_func_decl_type(stmt.type):
                old = stmt.name

                # Keep aggregate declarations local when their initializer references
                # identifiers. Hoisting such declarations to file scope would create
                # invalid C (e.g., initializers that depend on parameters/locals).
                if (
                    self.current_func is not None
                    and self._requires_decl_initializer(stmt.type)
                    and stmt.init is not None
                    and self._contains_identifier(stmt.init)
                ):
                    self.scopes[-1][old] = old
                    self.visit(stmt)
                    new_block_items.append(stmt)
                    continue

                # Keep function parameter declarations untouched (e.g., K&R style param decls
                # that may appear at top of the function compound in this AST).
                if (
                    self.current_func is not None
                    and old in self.current_params
                    and len(self.scopes) == 3
                ):
                    new_block_items.append(stmt)
                    continue

                # ----- naming -----
                if self.current_func is None:
                    new = f"global_{old}"
                else:
                    key = (self.current_func, old)
                    count = self.counters.get(key, 0) + 1
                    self.counters[key] = count

                    if count == 1:
                        new = f"{self.current_func}__local_{old}"
                    else:
                        new = f"{self.current_func}__local_{count}_{old}"

                # register in scope
                self.scopes[-1][old] = new

                # rename declaration
                stmt.name = new
                self._rename_type(stmt.type, new)

                # For aggregates (struct/union/array), keep initializer on declaration.
                # For other types, move initializer to a separate assignment.
                if self._requires_decl_initializer(stmt.type):
                    self.global_decls.append(stmt)
                else:
                    # Move initializer to assignment for scalar types
                    init = stmt.init
                    stmt.init = None
                    self.global_decls.append(stmt)

                    # keep initializer as assignment
                    if init is not None:
                        # Rewrite IDs in initializer based on the current scope mapping.
                        self.visit(init)
                        new_block_items.append(
                            c_ast.Assignment(
                                op="=", lvalue=c_ast.ID(name=new), rvalue=init
                            )
                        )

            else:
                self.visit(stmt)
                new_block_items.append(stmt)

        node.block_items = new_block_items
        self.pop_scope()

    def _record_typedef(self, typedef_node):
        """Keep one typedef per name and preserve first-seen declaration order."""
        name = typedef_node.name
        if name is None:
            return

        new_sig = self._canonical_typedef_signature(typedef_node)

        existing = self._typedef_by_name.get(name)
        if existing is None:
            self._typedef_by_name[name] = typedef_node
            self._typedef_signature_by_name[name] = new_sig
            self.typedef_decls.append(typedef_node)
            return

        old_sig = self._typedef_signature_by_name.get(name)
        if old_sig != new_sig:
            logger.warning("Conflicting typedef `%s`; keeping first declaration", name)

    def _canonical_typedef_signature(self, typedef_node):
        """Return a normalized typedef signature for deduplication.

        Treat `__extension__ typedef ...` and plain `typedef ...` as equivalent.
        """
        type_code = self.generator.visit(typedef_node.type)
        type_code = " ".join(type_code.split())

        storage = list(getattr(typedef_node, "storage", []) or [])
        storage = [item for item in storage if item != "__extension__"]

        quals = list(getattr(typedef_node, "quals", []) or [])

        decl_code = self.generator.visit(typedef_node)
        decl_code = " ".join(decl_code.split())
        decl_code = re.sub(r"^__extension__\s+typedef\b", "typedef", decl_code)

        return (
            tuple(storage),
            tuple(quals),
            type_code,
            decl_code,
        )

    # ---------- For loop ----------
    def visit_For(self, node):
        self.push_scope()

        # handle "for (int i = ...)"
        if isinstance(node.init, c_ast.Decl):
            decl = node.init
            old = decl.name

            # Keep aggregate for-init declarations local when initializer depends on
            # identifiers; file-scope hoisting would produce invalid initializers.
            if (
                self.current_func is not None
                and self._requires_decl_initializer(decl.type)
                and decl.init is not None
                and self._contains_identifier(decl.init)
            ):
                self.scopes[-1][old] = old
                self.visit(decl)
                node.init = decl
            else:
                key = (self.current_func, old)
                count = self.counters.get(key, 0) + 1
                self.counters[key] = count
                if count == 1:
                    new = f"{self.current_func}__local_{old}"
                else:
                    new = f"{self.current_func}__local_{count}_{old}"

                self.scopes[-1][old] = new

                decl.name = new
                self._rename_type(decl.type, new)

                # For aggregates (struct/union/array), keep initializer on declaration.
                if self._requires_decl_initializer(decl.type):
                    self.global_decls.append(decl)
                    node.init = c_ast.EmptyStatement()
                else:
                    init = decl.init
                    decl.init = None
                    self.global_decls.append(decl)

                    if init is not None:
                        # Rewrite IDs in initializer based on the current scope mapping.
                        self.visit(init)
                        node.init = c_ast.Assignment(
                            op="=", lvalue=c_ast.ID(name=new), rvalue=init
                        )
                    else:
                        node.init = c_ast.EmptyStatement()

        else:
            if node.init:
                self.visit(node.init)

        if node.cond:
            self.visit(node.cond)

        if node.next:
            self.visit(node.next)

        self.visit(node.stmt)

        self.pop_scope()

    # ---------- While ----------
    def visit_While(self, node):
        self.push_scope()
        self.visit(node.cond)
        self.visit(node.stmt)
        self.pop_scope()

    # ---------- If ----------
    def visit_If(self, node):
        self.visit(node.cond)

        self.push_scope()
        self.visit(node.iftrue)
        self.pop_scope()

        if node.iffalse:
            self.push_scope()
            self.visit(node.iffalse)
            self.pop_scope()

    # ---------- Struct ----------
    def visit_Struct(self, node):
        # Struct names (e.g. "Point" in "struct Point { ... }") must NEVER be renamed.
        # DO NOT visit struct field declarations at all - they are part of the struct type definition
        # and should never be globalized or have their names changed.
        pass

    # ---------- Union ----------
    def visit_Union(self, node):
        # Union names and field declarations must NEVER be renamed or globalized.
        # Field names are part of the union definition, not variable references.
        pass

    # ---------- StructRef ----------
    def visit_StructRef(self, node):
        # In a struct reference like "p.x", only globalize the VARIABLE NAME (p),
        # NEVER the FIELD NAME (x). Field names are part of the struct definition,
        # not variable references.
        # Only visit the variable being accessed; do NOT visit the field.
        self.visit(node.name)

    # ---------- Identifier ----------
    def visit_ID(self, node):
        node.name = self.resolve(node.name)

    # ---------- Helper ----------
    def _rename_type(self, typ, new_name):
        if isinstance(typ, c_ast.TypeDecl):
            typ.declname = new_name
        elif hasattr(typ, "type"):
            self._rename_type(typ.type, new_name)

    def _is_struct_or_union_type(self, decl_type):
        """Check if a declaration type is a struct or union."""
        if isinstance(decl_type, c_ast.TypeDecl):
            inner_type = decl_type.type
            return isinstance(inner_type, (c_ast.Struct, c_ast.Union))
        return False

    def _is_array_type(self, decl_type):
        """Check if a declaration type is an array (possibly wrapped)."""
        if isinstance(decl_type, c_ast.ArrayDecl):
            return True
        if hasattr(decl_type, "type"):
            return self._is_array_type(decl_type.type)
        return False

    def _requires_decl_initializer(self, decl_type):
        """Types whose initializers must stay on declaration."""
        return self._is_struct_or_union_type(decl_type) or self._is_array_type(
            decl_type
        )

    def _contains_identifier(self, node):
        """Return True if subtree contains at least one identifier reference."""
        if node is None:
            return False
        if isinstance(node, c_ast.ID):
            return True
        for _, child in node.children():
            if self._contains_identifier(child):
                return True
        return False


class PrefixTransformer(c_ast.NodeVisitor):
    def __init__(self, prefix, original_global_names=None):
        self.prefix = prefix
        self.scopes = [{}]
        self.function_names = {}
        self.in_struct = 0
        self.original_global_names = set(original_global_names or [])

    def push_scope(self):
        self.scopes.append({})

    def pop_scope(self):
        self.scopes.pop()

    def resolve_var(self, name):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def _rename_type(self, typ, new_name):
        if isinstance(typ, c_ast.TypeDecl):
            typ.declname = new_name
        elif hasattr(typ, "type"):
            self._rename_type(typ.type, new_name)

    def _prefixed(self, name):
        return f"{self.prefix}{name}"

    def visit_FileAST(self, node):
        # Collect function names first so calls can be renamed consistently.
        for ext in node.ext:
            if isinstance(ext, c_ast.FuncDef):
                old = ext.decl.name
                self.function_names[old] = self._prefixed(old)

        # Visit all nodes EXCEPT extern declarations and external function declarations.
        for ext in node.ext:
            if isinstance(ext, c_ast.Decl) and (
                "extern" in (ext.storage or []) or _is_func_decl_type(ext.type)
            ):
                continue
            # Visit everything else (global vars, function definitions, etc.)
            self.visit(ext)

    def visit_FuncDef(self, node):
        self.push_scope()
        self.visit(node.decl)
        if node.param_decls:
            for decl in node.param_decls:
                self.visit(decl)
        self.visit(node.body)
        self.pop_scope()

    def visit_Compound(self, node):
        self.push_scope()
        for stmt in node.block_items or []:
            self.visit(stmt)
        self.pop_scope()

    def visit_For(self, node):
        self.push_scope()
        if node.init:
            self.visit(node.init)
        if node.cond:
            self.visit(node.cond)
        if node.next:
            self.visit(node.next)
        self.visit(node.stmt)
        self.pop_scope()

    def visit_Struct(self, node):
        # Struct names (e.g. "Point" in "struct Point { ... }") must NEVER be renamed.
        # DO NOT visit struct field declarations at all - they are part of the struct type definition
        # and field names should never be changed.
        pass

    def visit_Union(self, node):
        # Union names and field declarations must NEVER be renamed.
        # Field names are part of the union definition, not variable references.
        pass

    def visit_StructRef(self, node):
        # Visit the struct variable being accessed (e.g. "p" in "p.x"),
        # but do NOT rename the field name (e.g. "x" in "p.x").
        # The field name is a string attribute, not an ID node, so it's safe.
        self.visit(node.name)

    def visit_Decl(self, node):
        # Never rename extern declarations.
        if "extern" in (node.storage or []):
            return

        if node.name is not None:
            if _is_func_decl_type(node.type):
                node.name = self.function_names.get(
                    node.name, self._prefixed(node.name)
                )
                self._rename_type(node.type, node.name)
            elif self.in_struct == 0:
                # Rename VARIABLE DECLARATIONS only (e.g. "p" in "struct Point p;").
                # Struct names themselves (e.g. "Point") are string attributes and are never renamed.
                old = node.name
                # Distinguish original translation-unit globals from globalized locals.
                is_file_scope = len(self.scopes) == 1
                if is_file_scope and old in self.original_global_names:
                    new = self._prefixed(f"global_{old}")
                else:
                    new = self._prefixed(old)
                self.scopes[-1][old] = new
                node.name = new
                self._rename_type(node.type, new)

        if node.bitsize:
            self.visit(node.bitsize)
        if node.init:
            self.visit(node.init)
        self.visit(node.type)

    def visit_TypeDecl(self, node):
        if node.type:
            self.visit(node.type)

    def visit_FuncDecl(self, node):
        if node.args:
            self.visit(node.args)
        if node.type:
            self.visit(node.type)

    def visit_ParamList(self, node):
        for param in node.params:
            self.visit(param)

    def visit_ID(self, node):
        renamed_var = self.resolve_var(node.name)
        if renamed_var is not None:
            node.name = renamed_var
            return

        if node.name in self.function_names:
            node.name = self.function_names[node.name]


class Transformer:
    def __init__(self, code, prefix=""):
        code = remove_comments(code)
        code = blank_gnu_attributes(code)
        code = blank_asm_volatile_with_brackets(code)
        code = rewrite_unsupported_builtins(code)
        code = rewrite_cproblem_pycparserext(code)
        code = rewrite_unsupported_builtins(code)
        parser = GnuCParser()
        self.ast = parser.parse(code)
        self.generator = GnuCGenerator()
        self.prefix = prefix

    def transform(self):
        """Apply all transformations and return the transformed AST."""
        # Normalize scalar char initializers like `.modinfo` payloads into arrays.
        ScalarCharInitListTransformer().visit(self.ast)

        # Preprocessing: handle reach_error -> abort
        reach_error_transformer = ReachErrorTransformer()
        reach_error_transformer.visit(self.ast)

        # Capture original translation-unit globals so they can be renamed as prefix_global_<name>.
        original_global_names = set()
        for ext in self.ast.ext:
            if isinstance(ext, c_ast.Decl) and not _is_func_decl_type(ext.type):
                if ext.name and "extern" not in (ext.storage or []):
                    original_global_names.add(ext.name)

        # Globalize local variables
        t = GlobalizeTransformer()
        t.visit(self.ast)

        # Separate external function declarations from other nodes
        external_func_decls = []
        original_global_decls = []
        func_defs = []
        passthrough_nodes = []

        for ext in self.ast.ext:
            # External function declarations (Decl with FuncDecl type)
            if _is_func_decl_node(ext):
                external_func_decls.append(ext)
            # Preserve original file-scope globals from input program.
            elif isinstance(ext, c_ast.Decl):
                original_global_decls.append(ext)
            # Function definitions
            elif isinstance(ext, c_ast.FuncDef):
                func_defs.append(ext)
            # Typedefs are emitted from the deduplicated/hoisted typedef list.
            elif isinstance(ext, c_ast.Typedef):
                continue
            # Preserve non-Decl translation-unit nodes (e.g., pragmas)
            elif not isinstance(ext, c_ast.Decl):
                passthrough_nodes.append(ext)
            # Skip other Decl nodes - they're local variables that got globalized

        def _coord_key(node, index):
            coord = getattr(node, "coord", None)
            if coord is None:
                return (1, index)
            line = getattr(coord, "line", None)
            column = getattr(coord, "column", None)
            return (
                0,
                line if line is not None else 0,
                column if column is not None else 0,
                index,
            )

        # Preserve source order as closely as possible for declarations.
        ordered_decls = list(
            enumerate(
                external_func_decls
                + t.typedef_decls
                + original_global_decls
                + t.global_decls
                + passthrough_nodes
            )
        )
        ordered_decls.sort(key=lambda item: _coord_key(item[1], item[0]))

        # Reconstruct: declarations in source order, then function definitions.
        self.ast.ext = [node for _, node in ordered_decls] + func_defs

        if self.prefix:
            prefix_transformer = PrefixTransformer(
                self.prefix, original_global_names=original_global_names
            )
            prefix_transformer.visit(self.ast)

        return self.ast

    def generate_code(self, ast=None):
        """Generate C code from the given AST (or the internally stored AST)."""
        if ast is None:
            ast = self.ast
        generated = self.generator.visit(ast)
        return ensure_asm_volatile_semicolons(generated)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Transform C programs by globalizing local variable declarations"
    )
    parser.add_argument("input", type=str, help="Path to the input C program file")
    parser.add_argument(
        "output",
        type=str,
        help="Path to the output file where the transformed program will be written",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default="",
        help="Prefix to prepend to every variable and function name",
    )

    args = parser.parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    # Read the input C file
    try:
        code = input_path.read_text()
    except FileNotFoundError:
        logger.error("Input file not found: %s", input_path)
        sys.exit(1)
    except IOError as e:
        logger.error("Error reading input file %s: %s", input_path, e)
        sys.exit(1)

    # Transform the code
    try:
        transformer = Transformer(code, prefix=args.prefix)
        transformed_ast = transformer.transform()
        transformed_code = transformer.generate_code(transformed_ast)
    except Exception as e:
        logger.exception("Error transforming code: %s", e)
        sys.exit(1)

    # Write to output file
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(transformed_code)
        logger.info("Saved transformed file to: %s", output_path.resolve())
    except IOError as e:
        logger.error("Error writing to output file %s: %s", output_path, e)
        sys.exit(1)
