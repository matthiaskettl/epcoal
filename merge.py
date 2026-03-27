#!/usr/bin/env python3

import sys
import argparse
import logging
import re
from pathlib import Path

sys.path.append(str((Path(__file__).absolute().parent / "lib" / "pip")))

from pycparser import c_ast
from pycparserext.ext_c_parser import GnuCParser
from pycparserext.ext_c_generator import GnuCGenerator


logger = logging.getLogger(__name__)


class AssertionBuilder:
    """Helper to build assertion comparisons for different types."""

    def __init__(self, prefix1, prefix2):
        self.prefix1 = prefix1
        self.prefix2 = prefix2

    def build_assert_equal(self, var_name, type_obj):
        """
        Build if statement code for comparing two variables with different prefixes.
        Returns a snippet of C code as a string.
        """
        var1 = f"{self.prefix1}{var_name}"
        var2 = f"{self.prefix2}{var_name}"

        # For arrays: need to compare element by element
        if isinstance(type_obj, c_ast.ArrayDecl):
            return self._build_array_assert(var1, var2, type_obj)

        # For simple types (primitives, pointers, etc.)
        return f"if({var1} != {var2}) {{ reach_error(); }}"

    def _build_array_assert(self, var1, var2, array_decl):
        """Build code to compare two arrays element by element."""
        # Get array dimension
        dim = array_decl.dim

        if dim is None:
            # Unbounded array, can't compare
            logger.warning(f"Cannot compare unbounded array {var1}")
            return f"/* Cannot compare unbounded array {var1} */"

        # Generate the dimension value
        dim_code = self._get_array_dim_code(dim)

        # Build loop with if statements
        loop_code = (
            f"{{ "
            f"int _i; "
            f"for (_i = 0; _i < {dim_code}; _i++) {{ "
            f"if({var1}[_i] != {var2}[_i]) {{ reach_error(); }} "
            f"}}"
            f" }}"
        )
        return loop_code

    def _get_array_dim_code(self, dim):
        """Extract array dimension as C code."""
        generator = GnuCGenerator()
        return generator.visit(dim)


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
            if isinstance(ext, c_ast.Decl) and not isinstance(ext.type, c_ast.FuncDecl):
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
        """Extract variable name from lvalue."""
        if isinstance(lvalue, c_ast.ID):
            return lvalue.name
        elif isinstance(lvalue, c_ast.ArrayRef):
            # For array references, get the base name
            return self._get_var_name(lvalue.name)
        elif isinstance(lvalue, c_ast.StructRef):
            return lvalue.field.name
        return None

    def _to_base_name(self, var_name):
        """Strip known prefixes so both versions use the same pure function name."""
        if not var_name:
            return None
        if var_name.startswith(self.prefix1):
            return var_name[len(self.prefix1):]
        if var_name.startswith(self.prefix2):
            return var_name[len(self.prefix2):]
        return var_name


class Merger:
    def __init__(self, file1_path, prefix1, file2_path, prefix2):
        self.file1_path = Path(file1_path)
        self.file2_path = Path(file2_path)
        self.prefix1 = prefix1
        self.prefix2 = prefix2

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

        # Replace all __VERIFIER_nondet_X() calls in function bodies with pure function calls
        replacer = NondetCallReplacer(self.prefix1, self.prefix2)
        merged_ast_temp = c_ast.FileAST(merged_ext)
        replacer.visit(merged_ast_temp)
        logger.info(f"Found and replaced {len(replacer.nondet_calls_found)} __VERIFIER_nondet calls in function bodies")
        
        # Remove initializers from matched nondet globals to avoid duplicate calls
        self._remove_nondet_initializers(merged_ext, nondet_pairs)

        # Build equality checks for all matched variables.
        # Nondet inputs are synchronized through shared pure functions and invocation counter,
        # so they should still be compared after both mains run.
        check_code = self._build_assertions(var_pairs)

        # Create external declarations for pure functions from both global nondet vars and function body calls
        self._add_pure_function_declarations_all(merged_ext, nondet_pairs, replacer.nondet_calls_found)

        # Add the shared global invocation counter used by pure function calls
        self._add_invocation_counter_global(merged_ext)

        # Remove unused verifier nondet extern declarations after replacement
        merged_ext = self._remove_verifier_nondet_declarations(merged_ext)

        # Ensure reach_error function is present
        self._add_reach_error_if_missing(merged_ext)

        # Create new main function
        new_main = self._create_merged_main(check_code, nondet_pairs)
        merged_ext.append(new_main)

        # Reorganize: externals at top, then globals, then function definitions
        merged_ext = self._reorganize_declarations(merged_ext)

        # Create new FileAST
        merged_ast = c_ast.FileAST(merged_ext)
        return merged_ast

    def _reorganize_declarations(self, ext_list):
        """
        Reorganize declarations to: typedefs, external functions, globals, then functions.
        Deduplicate typedefs, function declarations, globals, and function definitions.
        """
        typedefs = {}  # name -> Typedef
        external_funcs = {}  # name -> Decl(FuncDecl)
        globals_by_name = {}  # name -> Decl(non-FuncDecl)
        func_defs_by_name = {}  # name -> FuncDef
        passthrough = []

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

        globals_list = []
        func_defs = []

        for ext in ext_list:
            if isinstance(ext, c_ast.Typedef):
                name = ext.name
                sig = _normalize_decl_text(self.generator.visit(ext))
                if sig in seen_typedef_signatures:
                    logger.debug(f"Skipped duplicate typedef signature: {name}")
                    continue
                seen_typedef_signatures.add(sig)
                if name not in typedefs:
                    typedefs[name] = ext
                    logger.debug(f"Added typedef: {name}")
                else:
                    logger.debug(f"Skipped duplicate typedef by name: {name}")
                continue

            # External function declarations (Decl with FuncDecl type)
            if isinstance(ext, c_ast.Decl) and isinstance(ext.type, c_ast.FuncDecl):
                sig = _normalize_decl_text(self.generator.visit(ext))
                if sig in seen_external_signatures:
                    logger.debug(f"Skipped duplicate external function signature: {ext.name}")
                    continue
                seen_external_signatures.add(sig)
                # Deduplicate by name as well: keep first occurrence
                if ext.name not in external_funcs:
                    external_funcs[ext.name] = ext
                    logger.debug(f"Added external function: {ext.name}")
                else:
                    logger.debug(f"Skipped duplicate external function: {ext.name}")
            # Global variable declarations (Decl without FuncDecl type)
            elif isinstance(ext, c_ast.Decl):
                sig = _normalize_decl_text(self.generator.visit(ext))
                if sig in seen_global_signatures:
                    logger.debug(f"Skipped duplicate global signature: {ext.name}")
                    continue
                seen_global_signatures.add(sig)
                if ext.name not in globals_by_name:
                    globals_by_name[ext.name] = ext
                    logger.debug(f"Added global: {ext.name}")
                else:
                    logger.debug(f"Skipped duplicate global by name: {ext.name}")
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
                    logger.debug(f"Added function definition: {fname}")
                else:
                    logger.debug(f"Skipped duplicate function definition by name: {fname}")
            else:
                passthrough.append(ext)

        globals_list = list(globals_by_name.values())
        func_defs = list(func_defs_by_name.values())
        # Emit pure extern declarations first among external functions.
        pure_externals = []
        other_externals = []
        for decl in external_funcs.values():
            if decl.name and decl.name.startswith("__pure_"):
                pure_externals.append(decl)
            else:
                other_externals.append(decl)

        # Reconstruct: typedefs, pure externs, other externs, globals, passthrough, function defs
        result = list(typedefs.values()) + pure_externals + other_externals + globals_list + passthrough + func_defs
        logger.info(
            f"Reorganized: {len(typedefs)} typedefs, {len(external_funcs)} external functions, "
            f"{len(globals_list)} globals, {len(func_defs)} function definitions"
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

        # Try to match by stripping prefixes
        for name1, decl1 in globals1.items():
            # Try to find matching variable in file2
            if name1.startswith(self.prefix1):
                base_name = name1[len(self.prefix1) :]
            else:
                base_name = name1

            expected_name2 = f"{self.prefix2}{base_name}"

            if expected_name2 in globals2:
                var_pairs.append((name1, expected_name2, decl1))

        return var_pairs

    def _build_assertions(self, var_pairs):
        """Build equality check statements for matched variable pairs."""
        builder = AssertionBuilder(self.prefix1, self.prefix2)
        checks = []

        for var1, _var2, decl1 in var_pairs:
            base_name = var1[len(self.prefix1):]

            check_stmt = builder.build_assert_equal(base_name, decl1.type)
            checks.append(check_stmt)
            logger.debug(f"Added check for {base_name}: {check_stmt}")

        logger.info(f"Generated {len(checks)} equality checks")
        return checks

    def _create_merged_main(self, checks, nondet_pairs):
        """
        Create a new main function that:
        1. Initializes global invocation counter
        2. Assigns deterministic values via pure functions using invocation counter
        3. Calls prefix1_main()
        4. Calls prefix2_main()
        5. Checks all non-nondet globals are equal with if statements calling reach_error()
        """
        parser = GnuCParser()

        # Build pure function assignments using shared invocation counter
        pure_assignments = []
        if nondet_pairs:
            for _, var1, var2 in nondet_pairs:
                # Extract base name for pure function
                base_name = var1[len(self.prefix1):]
                pure_func_name = f"__pure_{base_name}"
                # Call pure function with post-increment counter for both variables
                pure_assignments.append(f"{var1} = {pure_func_name}(__invocation_count++);")
                pure_assignments.append(f"{var2} = {pure_func_name}(__invocation_count++);")

        pure_code_str = '\n'.join(pure_assignments)
        check_code_str = '\n'.join(checks)

        # Build the main function body as C code, then parse it
        main_code = f"""
        int main() {{
            {pure_code_str}
            __invocation_count = 0;
            {self.prefix1}main();
            __invocation_count = 0;
            {self.prefix2}main();
            {check_code_str}
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
            base_name = var1[len(self.prefix1):]
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
                and isinstance(ext.type, c_ast.FuncDecl)
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
                and isinstance(ext.type, c_ast.FuncDecl)
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

    def generate_code(self, merged_ast):
        """Generate C code from merged AST."""
        return self.generator.visit(merged_ast)


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

    args = parser.parse_args()

    try:
        merger = Merger(args.input1, args.prefix1, args.input2, args.prefix2)
        merged_ast = merger.merge()
        merged_code = merger.generate_code(merged_ast)

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(merged_code)

        print(f"Saved merged file to: {output_path.resolve()}")
    except Exception as e:
        logger.exception("Error during merge: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
