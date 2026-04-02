#!/usr/bin/env python3

import sys
import argparse
import logging
import re
import copy
from pathlib import Path

sys.path.append(str((Path(__file__).absolute().parent / "lib" / "pip")))

from pycparser import c_ast
from pycparserext.ext_c_parser import GnuCParser, FuncDeclExt
from pycparserext.ext_c_generator import GnuCGenerator


logger = logging.getLogger(__name__)


def _is_func_decl_type(node_type):
    return isinstance(node_type, (c_ast.FuncDecl, FuncDeclExt))


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

        if depth != 0:
            out.append(content[start])
            i = start + 1
            continue

        # k points to char right after matching ')'.
        p = k
        while p < n and content[p].isspace():
            p += 1

        if p < n and content[p] == ";":
            out.append(content[start:p + 1])
            i = p + 1
        else:
            # Insert semicolon immediately after ')', keep following whitespace/tokens as-is.
            out.append(content[start:k])
            out.append(";")
            out.append(content[k:p])
            i = p

    return "".join(out)


class AssertionBuilder:
    """Helper to build assertion comparisons for different types."""

    def __init__(
        self,
        prefix1,
        prefix2,
        struct_defs=None,
        union_defs=None,
        typedef_defs=None,
        no_memcmp=False,
        pointer_policy="strict",
    ):
        self.prefix1 = prefix1
        self.prefix2 = prefix2
        self.struct_defs = dict(struct_defs or {})
        self.union_defs = dict(union_defs or {})
        self.typedef_defs = dict(typedef_defs or {})
        self.no_memcmp = no_memcmp
        self.pointer_policy = pointer_policy
        self.generator = GnuCGenerator()
        self._loop_counter = 0
        self.skipped_memcmp_sites = 0

    def build_assert_equal(self, var_name, type_obj):
        """
        Build if statement code for comparing two variables with different prefixes.
        Returns a snippet of C code as a string.
        """
        var1 = f"{self.prefix1}{var_name}"
        var2 = f"{self.prefix2}{var_name}"

        return self._build_compare_assert(var1, var2, type_obj)

    def _build_compare_assert(self, lhs, rhs, type_obj):
        """Build comparison code for a typed lhs/rhs pair."""
        if isinstance(type_obj, c_ast.ArrayDecl):
            return self._build_array_assert(lhs, rhs, type_obj)

        ptr_kind = self._resolve_pointer_kind(type_obj)
        if ptr_kind is not None:
            return self._build_pointer_assert(lhs, rhs, ptr_kind)

        struct_type = self._resolve_struct_type(type_obj)
        if struct_type is not None:
            return self._build_struct_assert(lhs, rhs, struct_type)

        union_type = self._resolve_union_type(type_obj)
        if union_type is not None:
            return self._build_union_assert(lhs, rhs, union_type)

        # Fallback for primitives, pointers, enums, etc.
        return f"if({lhs} != {rhs}) {{ reach_error(); }}"

    def _build_pointer_assert(self, lhs, rhs, ptr_kind):
        """Build pointer comparison according to configured policy."""
        policy = self.pointer_policy
        if policy == "nullness":
            return f"if((({lhs}) == 0) != (({rhs}) == 0)) {{ reach_error(); }}"

        if policy == "ignore-funcptr" and ptr_kind == "funcptr":
            logger.warning("Ignoring function pointer comparison for `%s` due to policy", lhs)
            return ""

        return f"if({lhs} != {rhs}) {{ reach_error(); }}"

    def _build_array_assert(self, var1, var2, array_decl):
        """Build code to compare two arrays element by element."""
        # Get array dimension
        dim = array_decl.dim

        if dim is None:
            # Unbounded array, can't compare
            logger.warning(f"Cannot compare unbounded array {var1}")
            return ""

        # Generate the dimension value
        dim_code = self._get_array_dim_code(dim)
        idx = f"_cmp_i{self._loop_counter}"
        self._loop_counter += 1

        inner_lhs = f"{var1}[{idx}]"
        inner_rhs = f"{var2}[{idx}]"
        inner_code = self._build_compare_assert(inner_lhs, inner_rhs, array_decl.type)

        return (
            f"{{ "
            f"int {idx}; "
            f"for ({idx} = 0; {idx} < {dim_code}; {idx}++) {{ "
            f"{inner_code} "
            f"}} "
            f"}}"
        )

    def _build_memcmp_or_byte_loop_assert(self, lhs, rhs):
        """Build fallback equality for opaque sub-objects."""
        if not self.no_memcmp:
            return f"if(memcmp(&( {lhs} ), &( {rhs} ), sizeof({lhs})) != 0) {{ reach_error(); }}"

        self.skipped_memcmp_sites += 1
        return ""

    def _build_struct_assert(self, lhs, rhs, struct_node):
        """Build field-by-field comparison for structs."""
        decls = struct_node.decls
        if decls is None and struct_node.name:
            resolved = self.struct_defs.get(struct_node.name)
            if resolved is not None:
                decls = resolved.decls

        # If struct layout is not available, fallback to bytewise comparison.
        if not decls:
            logger.warning("Struct layout unavailable for `%s`, using memcmp fallback", struct_node.name)
            return self._build_memcmp_or_byte_loop_assert(lhs, rhs)

        checks = []
        needs_fallback = False
        for field in decls:
            if not isinstance(field, c_ast.Decl):
                logger.warning("Skipping unsupported struct field node in `%s`", struct_node.name)
                needs_fallback = True
                continue

            # Handle anonymous nested structs by comparing their promoted members directly.
            if not field.name:
                anon_struct = self._resolve_struct_type(field.type)
                if anon_struct is not None and anon_struct.decls:
                    for nested in anon_struct.decls:
                        if not isinstance(nested, c_ast.Decl) or not nested.name:
                            needs_fallback = True
                            continue
                        nested_lhs = f"({lhs}).{nested.name}"
                        nested_rhs = f"({rhs}).{nested.name}"
                        checks.append(self._build_compare_assert(nested_lhs, nested_rhs, nested.type))
                    continue

                logger.warning("Skipping anonymous/unsupported struct field in `%s`", struct_node.name)
                needs_fallback = True
                continue

            field_lhs = f"({lhs}).{field.name}"
            field_rhs = f"({rhs}).{field.name}"
            checks.append(self._build_compare_assert(field_lhs, field_rhs, field.type))

        # If parts of the struct are opaque/unsupported, keep precise checks for known
        # fields and add a bytewise fallback to cover the rest.
        if needs_fallback:
            checks.append(self._build_memcmp_or_byte_loop_assert(lhs, rhs))

        if not checks:
            return self._build_memcmp_or_byte_loop_assert(lhs, rhs)

        return "{ " + " ".join(checks) + " }"

    def _build_union_assert(self, lhs, rhs, union_node):
        """Build comparison code for unions.

        Prefer a field comparison only in the trivial case of exactly one named field.
        Otherwise use bytewise equality over the storage.
        """
        decls = union_node.decls
        if decls is None and union_node.name:
            resolved = self.union_defs.get(union_node.name)
            if resolved is not None:
                decls = resolved.decls

        if decls:
            named_fields = [f for f in decls if isinstance(f, c_ast.Decl) and f.name]
            if len(named_fields) == 1:
                field = named_fields[0]
                field_lhs = f"({lhs}).{field.name}"
                field_rhs = f"({rhs}).{field.name}"
                return self._build_compare_assert(field_lhs, field_rhs, field.type)

        return self._build_memcmp_or_byte_loop_assert(lhs, rhs)

    def _resolve_struct_type(self, type_obj, seen_typedefs=None):
        """Resolve a type object to a concrete struct node if possible."""
        if seen_typedefs is None:
            seen_typedefs = set()

        if isinstance(type_obj, c_ast.Struct):
            if type_obj.decls:
                return type_obj
            if type_obj.name and type_obj.name in self.struct_defs:
                return self.struct_defs[type_obj.name]
            return type_obj

        if isinstance(type_obj, c_ast.TypeDecl):
            inner = type_obj.type
            if isinstance(inner, c_ast.Struct):
                return self._resolve_struct_type(inner, seen_typedefs)
            if isinstance(inner, c_ast.Union):
                return None
            if isinstance(inner, c_ast.IdentifierType) and len(inner.names) == 1:
                alias = inner.names[0]
                if alias in seen_typedefs:
                    return None
                target = self.typedef_defs.get(alias)
                if target is not None:
                    seen_typedefs.add(alias)
                    return self._resolve_struct_type(target, seen_typedefs)
            return None

        if isinstance(type_obj, c_ast.IdentifierType):
            # Case: direct alias usage, e.g. `wait_queue_head_t`.
            if len(type_obj.names) == 1:
                alias = type_obj.names[0]
                if alias in seen_typedefs:
                    return None
                target = self.typedef_defs.get(alias)
                if target is not None:
                    seen_typedefs.add(alias)
                    return self._resolve_struct_type(target, seen_typedefs)

            # Case: tokenizer emits "struct X" as IdentifierType names.
            if len(type_obj.names) >= 2 and type_obj.names[0] == "struct":
                struct_name = type_obj.names[-1]
                resolved = self.struct_defs.get(struct_name)
                if resolved is not None:
                    return resolved
                return c_ast.Struct(name=struct_name, decls=None)

            return None

        return None

    def _resolve_union_type(self, type_obj, seen_typedefs=None):
        """Resolve a type object to a concrete union node if possible."""
        if seen_typedefs is None:
            seen_typedefs = set()

        if isinstance(type_obj, c_ast.Union):
            if type_obj.decls:
                return type_obj
            if type_obj.name and type_obj.name in self.union_defs:
                return self.union_defs[type_obj.name]
            return type_obj

        if isinstance(type_obj, c_ast.TypeDecl):
            inner = type_obj.type
            if isinstance(inner, c_ast.Union):
                return self._resolve_union_type(inner, seen_typedefs)
            if isinstance(inner, c_ast.Struct):
                return None
            if isinstance(inner, c_ast.IdentifierType) and len(inner.names) == 1:
                alias = inner.names[0]
                if alias in seen_typedefs:
                    return None
                target = self.typedef_defs.get(alias)
                if target is not None:
                    seen_typedefs.add(alias)
                    return self._resolve_union_type(target, seen_typedefs)
            return None

        if isinstance(type_obj, c_ast.IdentifierType):
            # Case: direct alias usage, e.g. typedef to union.
            if len(type_obj.names) == 1:
                alias = type_obj.names[0]
                if alias in seen_typedefs:
                    return None
                target = self.typedef_defs.get(alias)
                if target is not None:
                    seen_typedefs.add(alias)
                    return self._resolve_union_type(target, seen_typedefs)

            # Case: tokenizer emits "union X" as IdentifierType names.
            if len(type_obj.names) >= 2 and type_obj.names[0] == "union":
                union_name = type_obj.names[-1]
                resolved = self.union_defs.get(union_name)
                if resolved is not None:
                    return resolved
                return c_ast.Union(name=union_name, decls=None)

            return None

        return None

    def _resolve_pointer_kind(self, type_obj, seen_typedefs=None):
        """Resolve whether type is pointer/function-pointer: returns ptr kind or None."""
        if seen_typedefs is None:
            seen_typedefs = set()

        if isinstance(type_obj, c_ast.PtrDecl):
            target = type_obj.type
            if isinstance(target, c_ast.FuncDecl):
                return "funcptr"
            if isinstance(target, c_ast.TypeDecl) and isinstance(target.type, c_ast.IdentifierType):
                names = target.type.names
                if len(names) == 1:
                    alias = names[0]
                    if alias not in seen_typedefs:
                        seen_typedefs.add(alias)
                        td = self.typedef_defs.get(alias)
                        if td is not None:
                            nested = self._resolve_pointer_kind(td, seen_typedefs)
                            if nested is not None:
                                return nested
            return "ptr"

        if isinstance(type_obj, c_ast.TypeDecl):
            inner = type_obj.type
            if isinstance(inner, c_ast.IdentifierType) and len(inner.names) == 1:
                alias = inner.names[0]
                if alias in seen_typedefs:
                    return None
                target = self.typedef_defs.get(alias)
                if target is not None:
                    seen_typedefs.add(alias)
                    return self._resolve_pointer_kind(target, seen_typedefs)
            return None

        if isinstance(type_obj, c_ast.IdentifierType) and len(type_obj.names) == 1:
            alias = type_obj.names[0]
            if alias in seen_typedefs:
                return None
            target = self.typedef_defs.get(alias)
            if target is not None:
                seen_typedefs.add(alias)
                return self._resolve_pointer_kind(target, seen_typedefs)

        return None

    def _get_array_dim_code(self, dim):
        """Extract array dimension as C code."""
        return self.generator.visit(dim)


class StructDefCollector(c_ast.NodeVisitor):
    """Collect named struct definitions that have field declarations."""

    def __init__(self):
        self.struct_defs = {}

    def visit_Struct(self, node):
        if node.name and node.decls and node.name not in self.struct_defs:
            self.struct_defs[node.name] = node
        self.generic_visit(node)


class UnionDefCollector(c_ast.NodeVisitor):
    """Collect named union definitions that have field declarations."""

    def __init__(self):
        self.union_defs = {}

    def visit_Union(self, node):
        if node.name and node.decls and node.name not in self.union_defs:
            self.union_defs[node.name] = node
        self.generic_visit(node)


class TypedefDefCollector(c_ast.NodeVisitor):
    """Collect typedef definitions for recursive type resolution."""

    def __init__(self):
        self.typedef_defs = {}

    def visit_Typedef(self, node):
        if node.name and node.name not in self.typedef_defs:
            self.typedef_defs[node.name] = node.type
        self.generic_visit(node)


class NondetDetector:
    """Helper to detect and extract __VERIFIER_nondet_X() calls."""

    @staticmethod
    def is_nondet_call(init_node):
        """
        Check if an init node is a __VERIFIER_nondet_X() call.
        Returns (True, nondet_type) if it is, (False, None) otherwise.
        nondet_type is the X in __VERIFIER_nondet_X (e.g., 'int', 'float').
        """
        if not isinstance(init_node, c_ast.FuncCall):
            return False, None

        func_name = None
        if isinstance(init_node.name, c_ast.ID):
            func_name = init_node.name.name
        
        if not func_name or not func_name.startswith("__VERIFIER_nondet_"):
            return False, None

        nondet_type = func_name[len("__VERIFIER_nondet_"):]
        return True, nondet_type

    @staticmethod
    def get_nondet_type_str(nondet_type):
        """Convert nondet type shorthand to C type string."""
        nondet_to_type = {
            "int": "int",
            "uint": "unsigned int",
            "long": "long",
            "ulong": "unsigned long",
            "short": "short",
            "ushort": "unsigned short",
            "char": "char",
            "uchar": "unsigned char",
            "float": "float",
            "double": "double",
            "bool": "_Bool",
        }
        return nondet_to_type.get(nondet_type, "int")


class GlobalVariableExtractor(c_ast.NodeVisitor):
    """Extract all global variable declarations from an AST."""

    def __init__(self):
        self.global_vars = {}  # name -> c_ast.Decl
        self.nondet_vars = {}  # name -> nondet_type (if initialized with __VERIFIER_nondet)

    def visit_FileAST(self, node):
        for ext in node.ext:
            if isinstance(ext, c_ast.Decl) and not _is_func_decl_type(ext.type):
                if not ext.name:
                    # Anonymous declarations cannot be matched across files by name.
                    logger.debug("Skipping anonymous global declaration during extraction")
                    continue
                self.global_vars[ext.name] = ext
                
                # Check if initialized with nondet call
                if ext.init:
                    is_nondet, nondet_type = NondetDetector.is_nondet_call(ext.init)
                    if is_nondet:
                        self.nondet_vars[ext.name] = nondet_type

    def visit_Struct(self, node):
        # Don't visit into struct definitions
        pass

    def visit_Union(self, node):
        # Don't visit into union definitions
        pass


class NondetCallReplacer(c_ast.NodeVisitor):
    """Replace __VERIFIER_nondet_X() calls in function bodies with pure function calls."""
    
    def __init__(self, prefix1, prefix2):
        self.prefix1 = prefix1
        self.prefix2 = prefix2
        self.nondet_calls_found = []  # List of (nondet_type, var_name)
    
    def visit_Assignment(self, node):
        """Check assignments and replace nondet calls with pure function calls."""
        # Handle assignment RHS first so we can derive pure function name from lvalue.
        if isinstance(node.rvalue, c_ast.FuncCall):
            is_nondet, nondet_type = NondetDetector.is_nondet_call(node.rvalue)
            if is_nondet:
                # Extract variable name from lvalue
                var_name = self._get_var_name(node.lvalue)
                base_name = self._to_base_name(var_name)
                if base_name:
                    self.nondet_calls_found.append((nondet_type, base_name))
                    # Replace the function call with pure function call
                    pure_func_name = f"__pure_{base_name}"
                    func_id = c_ast.ID(pure_func_name)
                    # Create post-increment: __invocation_count++
                    counter_postinc = c_ast.UnaryOp("p++", c_ast.ID("__invocation_count"))
                    expr_list = c_ast.ExprList([counter_postinc])
                    node.rvalue = c_ast.FuncCall(func_id, expr_list)
                    logger.debug(f"Replaced __VERIFIER_nondet call with __pure_{base_name}(__invocation_count++)")
                    # Don't recurse into the replaced call.
                    self.visit(node.lvalue)
                    return

        # For non-nondet assignments, continue recursion normally.
        self.generic_visit(node)

    def visit_FuncCall(self, node):
        """Replace standalone/embedded nondet calls (conditions, args, expressions, etc.)."""
        # Recurse into nested call/args first.
        self.generic_visit(node)

        is_nondet, nondet_type = NondetDetector.is_nondet_call(node)
        if not is_nondet:
            return

        # No lvalue context here; use a stable pure function name by nondet type.
        base_name = f"nondet_{nondet_type}"
        self.nondet_calls_found.append((nondet_type, base_name))

        node.name = c_ast.ID(f"__pure_{base_name}")
        node.args = c_ast.ExprList([c_ast.UnaryOp("p++", c_ast.ID("__invocation_count"))])
        logger.debug(
            "Replaced standalone __VERIFIER_nondet call with __pure_%s(__invocation_count++)",
            base_name,
        )
    
    def _get_var_name(self, lvalue):
        """Extract a stable key from lvalue for naming pure nondet streams."""
        if isinstance(lvalue, c_ast.ID):
            return lvalue.name
        elif isinstance(lvalue, c_ast.ArrayRef):
            base = self._get_var_name(lvalue.name)
            return f"{base}__idx" if base else None
        elif isinstance(lvalue, c_ast.StructRef):
            base = self._get_var_name(lvalue.name)
            field = lvalue.field.name if isinstance(lvalue.field, c_ast.ID) else "field"
            op = "ptr" if lvalue.type == "->" else "dot"
            if base:
                return f"{base}__{op}__{field}"
            return field
        return None

    def _to_base_name(self, var_name):
        """Strip known prefixes so both versions use the same pure function name."""
        if not var_name:
            return None
        if var_name.startswith(self.prefix1):
            var_name = var_name[len(self.prefix1):]
        if var_name.startswith(self.prefix2):
            var_name = var_name[len(self.prefix2):]

        # Keep generated name a valid C identifier.
        var_name = re.sub(r"[^A-Za-z0-9_]", "_", var_name)
        var_name = re.sub(r"_+", "_", var_name).strip("_")
        return var_name or "nondet_site"


class TerminationCallReplacer(c_ast.NodeVisitor):
    """Rewrite terminating calls to side-specific helpers."""

    TERMINATION_KIND_BY_NAME = {
        "abort": "abort",
        "__assert_fail": "abort",
        "__assert": "abort",
        "__assert_perror_fail": "abort",
        "__builtin_trap": "abort",
        "exit": "exit",
        "_Exit": "exit",
        "quick_exit": "exit",
        "reach_error": "reach_error",
    }

    def __init__(self, prefix1, prefix2):
        self.prefix1 = prefix1
        self.prefix2 = prefix2
        self._side_stack = []

    def _current_side(self):
        if not self._side_stack:
            return None
        return self._side_stack[-1]

    def _get_side_for_function(self, name):
        if isinstance(name, str) and name.startswith(self.prefix1):
            return "original"
        if isinstance(name, str) and name.startswith(self.prefix2):
            return "mutant"
        return None

    def visit_FuncDef(self, node):
        side = self._get_side_for_function(node.decl.name)
        self._side_stack.append(side)
        self.generic_visit(node)
        self._side_stack.pop()

    def visit_FuncCall(self, node):
        # First recurse so nested calls get rewritten as well.
        self.generic_visit(node)

        if not isinstance(node.name, c_ast.ID):
            return

        kind = self.TERMINATION_KIND_BY_NAME.get(node.name.name)
        if kind is None:
            return

        side = self._current_side()
        if side == "original":
            helper_name = "__handle_original_exit"
        elif side == "mutant":
            helper_name = "__compare_global_state"
        else:
            return

        logger.debug("Replaced terminating call `%s` with `%s()`", node.name.name, helper_name)
        node.name = c_ast.ID(helper_name)
        node.args = None


class MainExitInstrumenter(c_ast.NodeVisitor):
    """Instrument prefixed main functions to trigger exit handling on return/fall-through."""

    def __init__(self, prefix1, prefix2):
        self.original_main_name = f"{prefix1}main"
        self.mutant_main_name = f"{prefix2}main"

    def visit_FuncDef(self, node):
        if node.decl.name == self.original_main_name:
            helper = "__handle_original_exit"
            self._instrument_main_body(node, helper)
            return

        if node.decl.name == self.mutant_main_name:
            helper = "__compare_global_state"
            self._instrument_main_body(node, helper)
            return

        self.generic_visit(node)

    def _instrument_main_body(self, node, helper_name):
        node.body = self._rewrite_stmt(node.body, helper_name)
        if isinstance(node.body, c_ast.Compound):
            if node.body.block_items is None:
                node.body.block_items = []
            # Fall-through exit at end of main body.
            node.body.block_items.append(c_ast.FuncCall(c_ast.ID(helper_name), None))

    def _wrap_return(self, ret_stmt, helper_name):
        return c_ast.Compound([
            c_ast.FuncCall(c_ast.ID(helper_name), None),
            ret_stmt,
        ])

    def _rewrite_stmt(self, stmt, helper_name):
        if stmt is None:
            return None

        if isinstance(stmt, c_ast.Return):
            return self._wrap_return(stmt, helper_name)

        if isinstance(stmt, c_ast.Compound):
            items = stmt.block_items or []
            stmt.block_items = [self._rewrite_stmt(s, helper_name) for s in items]
            return stmt

        if isinstance(stmt, c_ast.If):
            stmt.iftrue = self._rewrite_stmt(stmt.iftrue, helper_name)
            if stmt.iffalse is not None:
                stmt.iffalse = self._rewrite_stmt(stmt.iffalse, helper_name)
            return stmt

        if isinstance(stmt, c_ast.For):
            stmt.stmt = self._rewrite_stmt(stmt.stmt, helper_name)
            return stmt

        if isinstance(stmt, c_ast.While):
            stmt.stmt = self._rewrite_stmt(stmt.stmt, helper_name)
            return stmt

        if isinstance(stmt, c_ast.DoWhile):
            stmt.stmt = self._rewrite_stmt(stmt.stmt, helper_name)
            return stmt

        if isinstance(stmt, c_ast.Switch):
            stmt.stmt = self._rewrite_stmt(stmt.stmt, helper_name)
            return stmt

        if isinstance(stmt, c_ast.Label):
            stmt.stmt = self._rewrite_stmt(stmt.stmt, helper_name)
            return stmt

        if isinstance(stmt, c_ast.Case):
            stmt.stmts = [self._rewrite_stmt(s, helper_name) for s in (stmt.stmts or [])]
            return stmt

        if isinstance(stmt, c_ast.Default):
            stmt.stmts = [self._rewrite_stmt(s, helper_name) for s in (stmt.stmts or [])]
            return stmt

        return stmt


class Merger:
    def __init__(
        self,
        file1_path,
        prefix1,
        file2_path,
        prefix2,
        ast1=None,
        ast2=None,
        no_memcmp=False,
        pointer_policy="strict",
        compare_modified_only=False,
    ):
        self.file1_path = Path(file1_path) if file1_path is not None else None
        self.file2_path = Path(file2_path) if file2_path is not None else None
        self.prefix1 = prefix1
        self.prefix2 = prefix2
        self.no_memcmp = no_memcmp
        self.pointer_policy = pointer_policy
        self.compare_modified_only = compare_modified_only
        self.skipped_memcmp_sites = 0

        if ast1 is not None and ast2 is not None:
            self.ast1 = ast1
            self.ast2 = ast2
            logger.info("Using pre-parsed AST inputs for merge")
        else:
            if self.file1_path is None or self.file2_path is None:
                raise ValueError("Either both ASTs or both input file paths must be provided")

            parser = GnuCParser()

            # Parse both files
            try:
                code1 = self.file1_path.read_text()
                self.ast1 = parser.parse(code1)
                logger.info(f"Parsed {self.file1_path}")
            except Exception as e:
                logger.error(f"Error parsing {self.file1_path}: {e}")
                raise

            try:
                code2 = self.file2_path.read_text()
                self.ast2 = parser.parse(code2)
                logger.info(f"Parsed {self.file2_path}")
            except Exception as e:
                logger.error(f"Error parsing {self.file2_path}: {e}")
                raise

        self.generator = GnuCGenerator()

    @classmethod
    def from_asts(
        cls,
        ast1,
        prefix1,
        ast2,
        prefix2,
        no_memcmp=False,
        pointer_policy="strict",
        compare_modified_only=False,
    ):
        """Create a merger from already parsed ASTs."""
        return cls(
            None,
            prefix1,
            None,
            prefix2,
            ast1=ast1,
            ast2=ast2,
            no_memcmp=no_memcmp,
            pointer_policy=pointer_policy,
            compare_modified_only=compare_modified_only,
        )

    def _strip_prefix(self, name, prefix):
        """Strip a known prefix if present; otherwise return name unchanged."""
        if not isinstance(name, str):
            return name
        if prefix and isinstance(prefix, str) and name.startswith(prefix):
            return name[len(prefix):]
        return name

    def merge(self):
        """
        Merge two ASTs:
        1. Concatenate external declarations
        2. Extract global variables and detect nondet calls
        3. Remove initializers from matched nondet variables (will be assigned in main)
        4. Create merged main with shared nondet calls and if-based checks
        5. Add reach_error function if not present
        6. Reorganize: externals + globals + functions at top
        7. Return the merged AST
        """
        # Extract global variables from both files
        extractor1 = GlobalVariableExtractor()
        extractor1.visit(self.ast1)
        globals1 = extractor1.global_vars
        nondet1 = extractor1.nondet_vars

        extractor2 = GlobalVariableExtractor()
        extractor2.visit(self.ast2)
        globals2 = extractor2.global_vars
        nondet2 = extractor2.nondet_vars

        logger.info(f"Found {len(globals1)} globals in file 1 ({len(nondet1)} with nondet)")
        logger.info(f"Found {len(globals2)} globals in file 2 ({len(nondet2)} with nondet)")

        # Match globals by their base name (without prefix)
        var_pairs = self._match_globals(globals1, globals2)
        logger.info(f"Matched {len(var_pairs)} global variable pairs")

        # Match nondet variables with same type
        nondet_pairs = self._match_nondet(nondet1, nondet2, var_pairs)
        logger.info(f"Matched {len(nondet_pairs)} nondet variable pairs")

        # Create merged AST: concatenate both ASTs
        merged_ext = list(self.ast1.ext) + list(self.ast2.ext)

        # Rewrite terminating calls to side-specific helpers.
        termination_replacer = TerminationCallReplacer(self.prefix1, self.prefix2)
        merged_ast_temp = c_ast.FileAST(merged_ext)
        termination_replacer.visit(merged_ast_temp)

        # Instrument return/fall-through exits in both prefixed main functions.
        main_exit_instrumenter = MainExitInstrumenter(self.prefix1, self.prefix2)
        main_exit_instrumenter.visit(merged_ast_temp)

        # Remove duplicate adjacent compare calls after instrumentation/rewrite.
        self._deduplicate_consecutive_compare_calls(merged_ast_temp)

        if self.compare_modified_only:
            modified_globals = self._collect_written_prefixed_globals(merged_ast_temp)
            before = len(var_pairs)
            var_pairs = [
                (v1, v2, d)
                for (v1, v2, d) in var_pairs
                if v1 in modified_globals or v2 in modified_globals
            ]
            logger.info(
                "Filtered global comparisons to modified-only set: %d -> %d",
                before,
                len(var_pairs),
            )

        # Replace all __VERIFIER_nondet_X() calls in function bodies with pure function calls
        replacer = NondetCallReplacer(self.prefix1, self.prefix2)
        replacer.visit(merged_ast_temp)
        logger.info(f"Found and replaced {len(replacer.nondet_calls_found)} __VERIFIER_nondet calls in function bodies")
        
        # Remove initializers from matched nondet globals to avoid duplicate calls
        self._remove_nondet_initializers(merged_ext, nondet_pairs)

        # Build equality checks for all matched variables.
        # Nondet inputs are synchronized through shared pure functions and invocation counter,
        # so they should still be compared after both mains run.
        struct_defs = self._collect_struct_definitions(merged_ast_temp)
        union_defs = self._collect_union_definitions(merged_ast_temp)
        typedef_defs = self._collect_typedef_definitions(merged_ast_temp)
        check_code = self._build_assertions(var_pairs, struct_defs, union_defs, typedef_defs)

        # Some fallback equality checks use memcmp; ensure a declaration exists.
        self._add_memcmp_decl_if_needed(merged_ext, check_code)

        # Create external declarations for pure functions from both global nondet vars and function body calls
        self._add_pure_function_declarations_all(merged_ext, nondet_pairs, replacer.nondet_calls_found)

        # Add the shared global invocation counter used by pure function calls
        self._add_invocation_counter_global(merged_ext)

        # Remove unused verifier nondet extern declarations after replacement
        merged_ext = self._remove_verifier_nondet_declarations(merged_ext)

        # Ensure reach_error function is present
        self._add_reach_error_if_missing(merged_ext)

        # Add shared exit helpers and comparison helper.
        self._add_global_compare_helper(merged_ext, check_code)
        self._add_exit_helpers(merged_ext)

        # Create new main function
        new_main = self._create_merged_main(nondet_pairs)
        merged_ext.append(new_main)

        # Reorganize: externals at top, then globals, then function definitions
        merged_ext = self._reorganize_declarations(merged_ext)

        # Create new FileAST
        merged_ast = c_ast.FileAST(merged_ext)
        return merged_ast

    def _reorganize_declarations(self, ext_list):
        """
        Reorganize declarations while preserving source order as closely as possible.

        Deduplicate exact duplicates, keep the first occurrence of a declaration, and
        keep function definitions after declarations. This helps preserve dependency
        order for types such as structs/unions/typedefs.
        """
        ordered_decls = []
        func_defs_by_name = {}
        func_defs_order = []

        # Signatures used to deduplicate exact duplicates while keeping first occurrence.
        seen_typedef_signatures = set()
        seen_external_signatures = set()
        seen_global_signatures = set()
        seen_funcdef_signatures = set()

        def _normalize_decl_text(text):
            text = " ".join(text.split())
            # Treat '__extension__ typedef ...' and 'typedef ...' as equivalent.
            text = re.sub(r"^__extension__\\s+typedef\\b", "typedef", text)
            return text

        func_defs = []

        for ext in ext_list:
            if isinstance(ext, c_ast.Typedef):
                name = ext.name
                sig = _normalize_decl_text(self.generator.visit(ext))
                if sig in seen_typedef_signatures:
                    logger.debug(f"Skipped duplicate typedef signature: {name}")
                    continue
                seen_typedef_signatures.add(sig)
                ordered_decls.append(ext)
                logger.debug(f"Added typedef: {name}")
                continue

            # External function declarations (Decl with FuncDecl type)
            if isinstance(ext, c_ast.Decl) and _is_func_decl_type(ext.type):
                sig = _normalize_decl_text(self.generator.visit(ext))
                if sig in seen_external_signatures:
                    logger.debug(f"Skipped duplicate external function signature: {ext.name}")
                    continue
                seen_external_signatures.add(sig)
                ordered_decls.append(ext)
                logger.debug(f"Added external function: {ext.name}")
            # Global variable declarations (Decl without FuncDecl type)
            elif isinstance(ext, c_ast.Decl):
                sig = _normalize_decl_text(self.generator.visit(ext))
                if sig in seen_global_signatures:
                    logger.debug(f"Skipped duplicate global signature: {ext.name}")
                    continue
                seen_global_signatures.add(sig)
                ordered_decls.append(ext)
                logger.debug(f"Added global: {ext.name}")
            # Function definitions
            elif isinstance(ext, c_ast.FuncDef):
                fname = ext.decl.name
                sig = _normalize_decl_text(self.generator.visit(ext.decl))
                if sig in seen_funcdef_signatures:
                    logger.debug(f"Skipped duplicate function definition signature: {fname}")
                    continue
                seen_funcdef_signatures.add(sig)
                if fname not in func_defs_by_name:
                    func_defs_by_name[fname] = ext
                    func_defs_order.append(ext)
                    logger.debug(f"Added function definition: {fname}")
                else:
                    logger.debug(f"Skipped duplicate function definition by name: {fname}")
            else:
                ordered_decls.append(ext)

        # Emit forward declarations for function definitions so globals that
        # reference function symbols in initializers (e.g., ops tables) have
        # visible declarations during binding/type checks.
        forward_func_decls = []
        seen_forward_signatures = set()
        for fdef in func_defs_order:
            fdecl = copy.deepcopy(fdef.decl)
            sig = _normalize_decl_text(self.generator.visit(fdecl))
            if sig in seen_forward_signatures:
                continue
            seen_forward_signatures.add(sig)
            forward_func_decls.append(fdecl)
        # Reconstruct with dependency-safe ordering:
        # 1) type/extern/passthrough declarations in original order
        # 2) forward declarations for function definitions
        # 3) named globals (which may reference function symbols in initializers)
        # 4) function definitions
        pre_decls = []
        named_globals = []
        for decl in ordered_decls:
            if isinstance(decl, c_ast.Decl) and not _is_func_decl_type(decl.type) and decl.name is not None:
                named_globals.append(decl)
            else:
                pre_decls.append(decl)

        result = pre_decls + forward_func_decls + named_globals + func_defs_order
        logger.info(
            f"Reorganized: {len([n for n in ordered_decls if isinstance(n, c_ast.Typedef)])} typedefs, "
            f"{len([n for n in ordered_decls if isinstance(n, c_ast.Decl) and _is_func_decl_type(n.type)])} external functions, "
            f"{len([n for n in ordered_decls if isinstance(n, c_ast.Decl) and not _is_func_decl_type(n.type)])} globals, "
            f"{len(func_defs_order)} function definitions"
        )
        return result


    def _match_nondet(self, nondet1, nondet2, var_pairs):
        """
        Match nondet variables between the two files.
        Returns list of tuples: (nondet_type, var1_prefixed, var2_prefixed)
        """
        nondet_pairs = []

        for var1, var2, _ in var_pairs:
            if var1 in nondet1 and var2 in nondet2:
                nondet_type1 = nondet1[var1]
                nondet_type2 = nondet2[var2]
                
                # Only match if same nondet type
                if nondet_type1 == nondet_type2:
                    nondet_pairs.append((nondet_type1, var1, var2))

        return nondet_pairs

    def _remove_nondet_initializers(self, ext_list, nondet_pairs):
        """Remove initializers from globals that will be assigned in main."""
        nondet_vars_to_clear = set()
        for _, var1, var2 in nondet_pairs:
            nondet_vars_to_clear.add(var1)
            nondet_vars_to_clear.add(var2)

        for ext in ext_list:
            if isinstance(ext, c_ast.Decl) and ext.name in nondet_vars_to_clear:
                ext.init = None

    def _match_globals(self, globals1, globals2):
        """
        Match globals by base name.
        Returns list of tuples: (var_name_in_file1, var_name_in_file2, decl1)
        """
        var_pairs = []
        unmatched_left = []
        matched_right = set()

        # Try to match by stripping prefixes
        for name1, decl1 in globals1.items():
            if not isinstance(name1, str):
                unmatched_left.append(str(name1))
                continue

            # Try to find matching variable in file2.
            base_name = self._strip_prefix(name1, self.prefix1)
            if not isinstance(base_name, str):
                unmatched_left.append(str(name1))
                continue

            expected_name2 = f"{self.prefix2}{base_name}"

            if expected_name2 in globals2:
                var_pairs.append((name1, expected_name2, decl1))
                matched_right.add(expected_name2)
            else:
                unmatched_left.append(name1)

        if unmatched_left:
            logger.info(
                "No matching candidate in second file for %d globals (skipping): %s",
                len(unmatched_left),
                ", ".join(unmatched_left),
            )

        unmatched_right = [name for name in globals2.keys() if name not in matched_right]
        if unmatched_right:
            logger.info(
                "No matching candidate in first file for %d globals (skipping): %s",
                len(unmatched_right),
                ", ".join(unmatched_right),
            )

        return var_pairs

    def _collect_struct_definitions(self, root):
        """Collect named struct definitions from the merged translation unit."""
        collector = StructDefCollector()
        collector.visit(root)
        return collector.struct_defs

    def _collect_union_definitions(self, root):
        """Collect named union definitions from the merged translation unit."""
        collector = UnionDefCollector()
        collector.visit(root)
        return collector.union_defs

    def _collect_typedef_definitions(self, root):
        """Collect typedef definitions from the merged translation unit."""
        collector = TypedefDefCollector()
        collector.visit(root)
        return collector.typedef_defs

    def _build_assertions(self, var_pairs, struct_defs=None, union_defs=None, typedef_defs=None):
        """Build equality check statements for matched variable pairs."""
        builder = AssertionBuilder(
            self.prefix1,
            self.prefix2,
            struct_defs=struct_defs,
            union_defs=union_defs,
            typedef_defs=typedef_defs,
            no_memcmp=self.no_memcmp,
            pointer_policy=self.pointer_policy,
        )
        checks = []

        for var1, _var2, decl1 in var_pairs:
            base_name = self._strip_prefix(var1, self.prefix1)

            check_stmt = builder.build_assert_equal(base_name, decl1.type)
            checks.append(check_stmt)
            logger.debug(f"Added check for {base_name}: {check_stmt}")

        self.skipped_memcmp_sites = builder.skipped_memcmp_sites

        logger.info(f"Generated {len(checks)} equality checks")
        return checks

    def _create_merged_main(self, nondet_pairs):
        """
        Create a new main function that:
        1. Initializes global invocation counter
        2. Assigns deterministic values via pure functions using invocation counter
        3. Calls prefix1_main()
        4. Lets instrumented exit helpers run prefix2_main() and compare on matching exits
        """
        parser = GnuCParser()

        # Build pure function assignments using shared invocation counter
        pure_assignments = []
        if nondet_pairs:
            for _, var1, var2 in nondet_pairs:
                # Extract base name for pure function
                base_name = self._strip_prefix(var1, self.prefix1)
                pure_func_name = f"__pure_{base_name}"
                # Call pure function with post-increment counter for both variables
                pure_assignments.append(f"{var1} = {pure_func_name}(__invocation_count++);")
                pure_assignments.append(f"{var2} = {pure_func_name}(__invocation_count++);")

        pure_code_str = '\n'.join(pure_assignments)

        # Build the main function body as C code, then parse it
        main_code = f"""
        int main() {{
            {pure_code_str}
            __invocation_count = 0;
            {self.prefix1}main();
            return 0;
        }}
        """

        try:
            parsed = parser.parse(main_code)
            func_def = parsed.ext[0]
            return func_def
        except Exception as e:
            logger.warning(f"Error parsing main function: {e}")
            logger.warning(f"Main code was: {main_code}")
            raise

    def _add_pure_function_declarations_all(self, ext_list, nondet_pairs, function_body_nondet_calls):
        """Add extern declarations for pure functions from both global nondet vars and function body calls.
        
        Args:
            ext_list: List of external declarations (FileAST.ext)
            nondet_pairs: List of (nondet_type, var1, var2) tuples for global vars
            function_body_nondet_calls: List of (nondet_type, var_name) tuples from function bodies
        """
        # Track seen base names to avoid duplicate declarations
        seen_bases = {}  # base_name -> nondet_type
        
        # Process global nondet pairs
        for nondet_type, var1, var2 in nondet_pairs:
            # Extract base name from var1 (strip prefix)
            base_name = self._strip_prefix(var1, self.prefix1)
            seen_bases[base_name] = nondet_type
        
        # Process function body nondet calls (these use the full variable name directly)
        for nondet_type, var_name in function_body_nondet_calls:
            if var_name not in seen_bases:
                seen_bases[var_name] = nondet_type
            else:
                logger.debug(f"Pure function `__pure_{var_name}` already registered as global")
        
        if not seen_bases:
            logger.debug("No pure functions needed, skipping declarations")
            return
        
        # Track existing pure declarations to avoid adding duplicates.
        existing_pure_names = set()
        for ext in ext_list:
            if (
                isinstance(ext, c_ast.Decl)
                and _is_func_decl_type(ext.type)
                and ext.name
                and ext.name.startswith("__pure_")
            ):
                existing_pure_names.add(ext.name)

        # Create declarations for all unique pure functions
        parser = GnuCParser()
        for base_name, nondet_type in seen_bases.items():
            # Get the C type string for this nondet type
            type_str = NondetDetector.get_nondet_type_str(nondet_type)
            
            # Create extern declaration: extern <type> __pure_<base_name>(int count);
            pure_name = f"__pure_{base_name}"
            if pure_name in existing_pure_names:
                logger.debug(f"Pure function declaration already exists: {pure_name}")
                continue

            decl_code = f"extern {type_str} {pure_name}(int count);"
            
            try:
                parsed = parser.parse(decl_code)
                func_decl = parsed.ext[0]
                ext_list.insert(0, func_decl)
                existing_pure_names.add(pure_name)
                logger.info(f"Added pure function declaration for `{pure_name}`: {decl_code}")
            except Exception as e:
                logger.error(f"Error parsing pure function declaration '{decl_code}': {e}")
                raise

    def _add_invocation_counter_global(self, ext_list):
        """Ensure a single global counter declaration exists for pure invocation ordering."""
        for ext in ext_list:
            if isinstance(ext, c_ast.Decl) and ext.name == "__invocation_count":
                return

        parser = GnuCParser()
        parsed = parser.parse("int __invocation_count;")
        counter_decl = parsed.ext[0]
        ext_list.insert(0, counter_decl)
        logger.info("Added global invocation counter declaration: int __invocation_count;")

    def _remove_verifier_nondet_declarations(self, ext_list):
        """Drop stale extern declarations of __VERIFIER_nondet_* after call replacement."""
        filtered = []
        removed = 0
        for ext in ext_list:
            if (
                isinstance(ext, c_ast.Decl)
                and _is_func_decl_type(ext.type)
                and ext.name
                and ext.name.startswith("__VERIFIER_nondet_")
            ):
                removed += 1
                continue
            filtered.append(ext)

        if removed:
            logger.info(f"Removed {removed} __VERIFIER_nondet external declarations")
        return filtered

    def _add_reach_error_if_missing(self, ext_list):
        """Add reach_error function with empty body at top if not already present."""
        # Check if reach_error already exists
        for ext in ext_list:
            if isinstance(ext, c_ast.FuncDef) and ext.decl.name == "reach_error":
                return  # Already present
            if isinstance(ext, c_ast.Decl) and ext.name == "reach_error":
                return  # Already present

        # Create reach_error function with empty body
        parser = GnuCParser()
        reach_error_code = "void reach_error() { }"
        try:
            parsed = parser.parse(reach_error_code)
            reach_error_func = parsed.ext[0]
            # Insert at the BEGINNING (position 0), not at the end
            ext_list.insert(0, reach_error_func)
            logger.info("Added reach_error() function at top of merged code")
        except Exception as e:
            logger.warning(f"Could not add reach_error function: {e}")

    def _add_memcmp_decl_if_needed(self, ext_list, checks):
        """Add `extern int memcmp(...)` when memcmp is referenced by generated checks."""
        if self.no_memcmp:
            return

        if not any("memcmp(" in stmt for stmt in checks):
            return

        for ext in ext_list:
            if isinstance(ext, c_ast.Decl) and _is_func_decl_type(ext.type) and ext.name == "memcmp":
                return

        parser = GnuCParser()
        # Use unsigned long for size to avoid dependency on typedef size_t ordering.
        decl_code = "extern int memcmp(const void *lhs, const void *rhs, unsigned long n);"
        parsed = parser.parse(decl_code)
        ext_list.insert(0, parsed.ext[0])
        logger.info("Added external declaration for memcmp")

    def _add_global_compare_helper(self, ext_list, checks):
        """Add a single helper that compares all matched globals."""
        helper_name = "__compare_global_state"
        for ext in ext_list:
            if isinstance(ext, c_ast.FuncDef) and ext.decl.name == helper_name:
                return

        parser = GnuCParser()
        check_code = "\n".join(checks)
        helper_code = f"""
        void {helper_name}() {{
            {check_code}
            abort();
        }}
        """
        parsed = parser.parse(helper_code)
        ext_list.insert(0, parsed.ext[0])
        logger.info("Added global comparison helper `%s`", helper_name)

    def _add_exit_helpers(self, ext_list):
        """Add original-side exit handler."""

        existing_funcs = {
            ext.decl.name
            for ext in ext_list
            if isinstance(ext, c_ast.FuncDef)
        }

        parser = GnuCParser()

        original_helper = "__handle_original_exit"
        if original_helper not in existing_funcs:
            original_code = f"""
            void {original_helper}() {{
                __invocation_count = 0;
                {self.prefix2}main();
            }}
            """
            parsed = parser.parse(original_code)
            ext_list.insert(0, parsed.ext[0])
            existing_funcs.add(original_helper)
            logger.info("Added termination helper `%s`", original_helper)

    def _deduplicate_consecutive_compare_calls(self, root):
        """Drop immediately repeated compare/original-exit helper calls."""

        def _target_call_name(stmt):
            if isinstance(stmt, c_ast.FuncCall) and isinstance(stmt.name, c_ast.ID):
                name = stmt.name.name
                if name in {"__compare_global_state", "__handle_original_exit"}:
                    return name
            return None

        class _Deduper(c_ast.NodeVisitor):
            def visit_Compound(self, node):
                items = node.block_items or []
                deduped = []
                prev_target_name = None
                for stmt in items:
                    current_target_name = _target_call_name(stmt)
                    if current_target_name is not None and current_target_name == prev_target_name:
                        continue
                    deduped.append(stmt)
                    prev_target_name = current_target_name

                node.block_items = deduped

                for stmt in node.block_items:
                    self.visit(stmt)

        _Deduper().visit(root)

    def _collect_written_prefixed_globals(self, root):
        """Collect prefixed globals that appear on assignment/update lvalues."""

        def _base_id(expr):
            if isinstance(expr, c_ast.ID):
                return expr.name
            if isinstance(expr, c_ast.ArrayRef):
                return _base_id(expr.name)
            if isinstance(expr, c_ast.StructRef):
                return _base_id(expr.name)
            return None

        writes = set()
        prefixes = (self.prefix1, self.prefix2)

        class _WriteCollector(c_ast.NodeVisitor):
            def _record(self, expr):
                name = _base_id(expr)
                if isinstance(name, str) and any(name.startswith(p) for p in prefixes):
                    writes.add(name)

            def visit_Assignment(self, node):
                self._record(node.lvalue)
                self.generic_visit(node)

            def visit_UnaryOp(self, node):
                if node.op in {"p++", "p--", "++", "--"}:
                    self._record(node.expr)
                self.generic_visit(node)

        _WriteCollector().visit(root)
        return writes

    def generate_code(self, merged_ast):
        """Generate C code from merged AST."""
        generated = self.generator.visit(merged_ast)
        return ensure_asm_volatile_semicolons(generated)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Merge two C programs transformed with different prefixes and add assertions for global variable equality"
    )
    parser.add_argument("input1", type=str, help="Path to first transformed C file")
    parser.add_argument("prefix1", type=str, help="Prefix used for first file")
    parser.add_argument("input2", type=str, help="Path to second transformed C file")
    parser.add_argument("prefix2", type=str, help="Prefix used for second file")
    parser.add_argument("output", type=str, help="Path to output merged C file")
    parser.add_argument(
        "--no-memcmp",
        action="store_true",
        help="Skip opaque fallback comparisons that would otherwise use memcmp",
    )
    parser.add_argument(
        "--pointer-policy",
        choices=["strict", "nullness", "ignore-funcptr"],
        default="strict",
        help="Pointer equality policy used in generated comparisons",
    )
    parser.add_argument(
        "--compare-modified-only",
        action="store_true",
        help="Compare only globals that are assigned/updated in either version (heuristic)",
    )

    args = parser.parse_args()

    try:
        merger = Merger(
            args.input1,
            args.prefix1,
            args.input2,
            args.prefix2,
            no_memcmp=args.no_memcmp,
            pointer_policy=args.pointer_policy,
            compare_modified_only=args.compare_modified_only,
        )
        merged_ast = merger.merge()
        merged_code = merger.generate_code(merged_ast)

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(merged_code)
        logger.info("Saved merged file to: %s", output_path.resolve())
    except Exception as e:
        logger.exception("Error during merge: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
