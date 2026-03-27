#!/bin/python3

import sys
import argparse
import logging
import re
from pathlib import Path

sys.path.append(str((Path(__file__).absolute().parent / "lib" / "pip")))


from pycparserext.ext_c_parser import GnuCParser
from pycparserext.ext_c_generator import GnuCGenerator
from pycparser import c_ast

logger = logging.getLogger(__name__)


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
            name='abort',
            quals=[],
            align=[],
            storage=[],
            funcspec=[],
            type=c_ast.FuncDecl(
                args=None,
                type=c_ast.TypeDecl(
                    declname='abort',
                    quals=[],
                    align=[],
                    type=c_ast.IdentifierType(names=['void'])
                )
            ),
            init=None,
            bitsize=None
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
        if isinstance(node.decl, c_ast.Decl) and isinstance(node.decl.type, c_ast.FuncDecl):
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

        for stmt in (node.block_items or []):
            if isinstance(stmt, c_ast.Typedef):
                # Hoist typedefs so globalized declarations that depend on them remain valid.
                self._record_typedef(stmt)
                continue

            # Handle variable declarations
            if isinstance(stmt, c_ast.Decl) and not isinstance(stmt.type, c_ast.FuncDecl):
                old = stmt.name

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
                        new = f"{self.current_func}_{old}"
                    else:
                        new = f"{self.current_func}_{count}_{old}"

                # register in scope
                self.scopes[-1][old] = new

                # rename declaration
                stmt.name = new
                self._rename_type(stmt.type, new)

                # move declaration to global scope
                init = stmt.init
                stmt.init = None
                self.global_decls.append(stmt)

                # keep initializer as assignment
                if init is not None:
                    new_block_items.append(
                        c_ast.Assignment(
                            op='=',
                            lvalue=c_ast.ID(name=new),
                            rvalue=init
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

            key = (self.current_func, old)
            count = self.counters.get(key, 0) + 1
            self.counters[key] = count

            if count == 1:
                new = f"{self.current_func}_{old}"
            else:
                new = f"{self.current_func}_{count}_{old}"

            self.scopes[-1][old] = new

            decl.name = new
            self._rename_type(decl.type, new)

            init = decl.init
            decl.init = None
            self.global_decls.append(decl)

            if init is not None:
                node.init = c_ast.Assignment(
                    op='=',
                    lvalue=c_ast.ID(name=new),
                    rvalue=init
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

    # ---------- Identifier ----------
    def visit_ID(self, node):
        node.name = self.resolve(node.name)

    # ---------- Helper ----------
    def _rename_type(self, typ, new_name):
        if isinstance(typ, c_ast.TypeDecl):
            typ.declname = new_name
        elif hasattr(typ, "type"):
            self._rename_type(typ.type, new_name)


class PrefixTransformer(c_ast.NodeVisitor):
    def __init__(self, prefix):
        self.prefix = prefix
        self.scopes = [{}]
        self.function_names = {}
        self.in_struct = 0

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

        # Visit all nodes EXCEPT external function declarations
        for ext in node.ext:
            # Skip external function declarations (Decl with FuncDecl type) - keep them as-is
            if isinstance(ext, c_ast.Decl) and isinstance(ext.type, c_ast.FuncDecl):
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
        for stmt in (node.block_items or []):
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
        self.in_struct += 1
        if node.decls:
            for decl in node.decls:
                self.visit(decl)
        self.in_struct -= 1

    def visit_StructRef(self, node):
        # Do not rename struct field identifiers.
        self.visit(node.name)

    def visit_Decl(self, node):
        if node.name is not None:
            if isinstance(node.type, c_ast.FuncDecl):
                node.name = self.function_names.get(node.name, self._prefixed(node.name))
                self._rename_type(node.type, node.name)
            elif self.in_struct == 0:
                old = node.name
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
        parser = GnuCParser()
        self.ast = parser.parse(code)
        self.generator = GnuCGenerator()
        self.prefix = prefix

    def transform(self):
        # Preprocessing: handle reach_error -> abort
        reach_error_transformer = ReachErrorTransformer()
        reach_error_transformer.visit(self.ast)

        # Globalize local variables
        t = GlobalizeTransformer()
        t.visit(self.ast)

        # Separate external function declarations from other nodes
        external_func_decls = []
        func_defs = []
        passthrough_nodes = []
        
        for ext in self.ast.ext:
            # External function declarations (Decl with FuncDecl type)
            if isinstance(ext, c_ast.Decl) and isinstance(ext.type, c_ast.FuncDecl):
                external_func_decls.append(ext)
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

        # Reconstruct: externals, typedefs, globals, passthrough nodes, then functions
        self.ast.ext = external_func_decls + t.typedef_decls + t.global_decls + passthrough_nodes + func_defs

        if self.prefix:
            prefix_transformer = PrefixTransformer(self.prefix)
            prefix_transformer.visit(self.ast)

        return self.generator.visit(self.ast)
    

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Transform C programs by globalizing local variable declarations"
    )
    parser.add_argument(
        "input",
        type=str,
        help="Path to the input C program file"
    )
    parser.add_argument(
        "output",
        type=str,
        help="Path to the output file where the transformed program will be written"
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default="",
        help="Prefix to prepend to every variable and function name"
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
        transformed_code = Transformer(code, prefix=args.prefix).transform()
    except Exception as e:
        logger.exception("Error transforming code: %s", e)
        sys.exit(1)
    
    # Write to output file
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(transformed_code)
        print(f"Saved transformed file to: {output_path.resolve()}")
    except IOError as e:
        logger.error("Error writing to output file %s: %s", output_path, e)
        sys.exit(1)
