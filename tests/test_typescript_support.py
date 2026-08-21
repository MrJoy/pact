"""Tests for TypeScript/Effect-TS extraction and smoke test generation."""

from __future__ import annotations

import textwrap

import pytest

from pact.codebase_analyzer import (
    _TS_SIDE_EFFECT_IMPORT_RE,
    _extract_ts_imports,
    _resolve_relative_specifier,
    analyze_codebase,
    map_test_coverage,
    discover_source_files,
    discover_tests,
    extract_functions_typescript,
)
from pact.adopt import (
    build_decomposition_tree,
    generate_smoke_tests,
)
from pact.schemas_testgen import (
    CodebaseAnalysis,
    ExtractedFunction,
    SourceFile,
    TestFile,
)


# ── File Discovery ─────────────────────────────────────────────────


class TestTypeScriptFileDiscovery:
    def test_finds_ts_files(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.ts").write_text("export const x = 1")
        (tmp_path / "src" / "utils.ts").write_text("export const y = 2")
        files = discover_source_files(tmp_path, language="typescript")
        paths = [f.path for f in files]
        assert "src/index.ts" in paths
        assert "src/utils.ts" in paths

    def test_skips_test_files(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.ts").write_text("export const x = 1")
        (tmp_path / "src" / "main.test.ts").write_text("test('x', () => {})")
        (tmp_path / "src" / "main.spec.ts").write_text("test('x', () => {})")
        files = discover_source_files(tmp_path, language="typescript")
        paths = [f.path for f in files]
        assert "src/main.ts" in paths
        assert "src/main.test.ts" not in paths
        assert "src/main.spec.ts" not in paths

    def test_skips_declaration_files(self, tmp_path):
        (tmp_path / "types.d.ts").write_text("declare module 'foo' {}")
        (tmp_path / "index.ts").write_text("export const x = 1")
        files = discover_source_files(tmp_path, language="typescript")
        paths = [f.path for f in files]
        assert "index.ts" in paths
        assert "types.d.ts" not in paths

    def test_skips_node_modules(self, tmp_path):
        (tmp_path / "node_modules" / "effect").mkdir(parents=True)
        (tmp_path / "node_modules" / "effect" / "index.ts").write_text("x = 1")
        (tmp_path / "index.ts").write_text("export const x = 1")
        files = discover_source_files(tmp_path, language="typescript")
        paths = [f.path for f in files]
        assert not any("node_modules" in p for p in paths)

    def test_discovers_test_files(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.test.ts").write_text("test('x', () => {})")
        (tmp_path / "src" / "bar.spec.ts").write_text("it('y', () => {})")
        (tmp_path / "__tests__").mkdir()
        (tmp_path / "__tests__" / "baz.ts").write_text("test('z', () => {})")
        tfiles = discover_tests(tmp_path, language="typescript")
        paths = [f.path for f in tfiles]
        assert "src/foo.test.ts" in paths
        assert "src/bar.spec.ts" in paths
        assert "__tests__/baz.ts" in paths


# ── Import Extraction ──────────────────────────────────────────────


class TestTypeScriptImportExtraction:
    def test_single_line_named_import(self):
        source = 'import { foo } from "./foo.ts"\n'
        assert _extract_ts_imports(source) == ["./foo.ts"]

    def test_multiline_named_import(self):
        source = textwrap.dedent("""\
            import {
              foo,
              bar,
            } from "./foo.ts"
        """)
        assert _extract_ts_imports(source) == ["./foo.ts"]

    def test_multiline_import_among_single_line_imports(self):
        source = textwrap.dedent("""\
            import { Effect } from "effect"
            import {
              makeThing,
              ThingTag,
            } from "../thing.ts"
            import { last } from "./last.ts"
        """)
        assert _extract_ts_imports(source) == ["effect", "../thing.ts", "./last.ts"]

    def test_multiline_type_only_import(self):
        source = textwrap.dedent("""\
            import type {
              Alpha,
              Beta,
            } from "./types.ts"
        """)
        assert _extract_ts_imports(source) == ["./types.ts"]

    def test_multiline_import_with_block_comment(self):
        source = textwrap.dedent("""\
            import {
              /* runtime helper */
              foo,
            } from "./foo.ts"
        """)
        assert _extract_ts_imports(source) == ["./foo.ts"]

    def test_multiline_import_with_line_comment(self):
        source = textwrap.dedent("""\
            import {
              foo, // retained for compatibility
              bar,
            } from "./foo.ts"
        """)
        assert _extract_ts_imports(source) == ["./foo.ts"]

    def test_default_and_namespace_imports(self):
        source = textwrap.dedent("""\
            import def from "./def.ts"
            import * as ns from "./ns.ts"
        """)
        assert _extract_ts_imports(source) == ["./def.ts", "./ns.ts"]

    def test_reexport_is_extracted(self):
        source = 'export { foo } from "./foo.ts"\n'
        assert _extract_ts_imports(source) == ["./foo.ts"]

    def test_multiline_reexport_barrel(self):
        source = textwrap.dedent("""\
            export {
              alpha,
              beta,
            } from "./alpha.ts"
            export * from "./star.ts"
        """)
        assert _extract_ts_imports(source) == ["./alpha.ts", "./star.ts"]

    def test_local_export_does_not_swallow_following_imports(self):
        """A bodiless `export { x }` must not lazily consume the next statement."""
        source = textwrap.dedent("""\
            const foo = 1
            export { foo }
            import { bar } from "./bar.ts"
            import { baz } from "./baz.ts"
        """)
        assert _extract_ts_imports(source) == ["./bar.ts", "./baz.ts"]

    def test_local_export_is_not_an_import(self):
        source = textwrap.dedent("""\
            const foo = 1
            export { foo }
        """)
        assert _extract_ts_imports(source) == []

    def test_bare_dynamic_import(self):
        source = 'import("./x.ts")\n'
        assert _extract_ts_imports(source) == ["./x.ts"]

    def test_awaited_dynamic_import(self):
        source = 'await import("./x.ts")\n'
        assert _extract_ts_imports(source) == ["./x.ts"]

    def test_dynamic_import_with_destructuring_assignment(self):
        source = 'const { filteredLogger } = await import("../../rpc/middleware.ts")\n'
        assert _extract_ts_imports(source) == ["../../rpc/middleware.ts"]

    def test_dynamic_import_single_quotes(self):
        source = "await import('./x.ts')\n"
        assert _extract_ts_imports(source) == ["./x.ts"]

    def test_dynamic_import_inside_function_body(self):
        source = textwrap.dedent("""\
            export async function boot() {
              const mod = await import("./boot-impl.ts")
              return mod.default
            }
        """)
        assert _extract_ts_imports(source) == ["./boot-impl.ts"]

    def test_dynamic_import_spanning_lines(self):
        source = textwrap.dedent("""\
            const mod = await import(
              "./wrapped.ts",
            )
        """)
        assert _extract_ts_imports(source) == ["./wrapped.ts"]

    def test_dynamic_import_with_options(self):
        source = 'import("./data.json", { with: { type: "json" } })\n'
        assert _extract_ts_imports(source) == ["./data.json"]

    def test_concatenated_dynamic_import_is_skipped(self):
        source = 'import("./locales/" + locale + ".ts")\n'
        assert _extract_ts_imports(source) == []

    def test_computed_string_dynamic_import_is_skipped(self):
        source = 'import("./plugin.ts".trim())\n'
        assert _extract_ts_imports(source) == []

    def test_dynamic_and_static_imports_are_returned_in_source_order(self):
        source = textwrap.dedent("""\
            import { Effect } from "effect"

            export async function boot() {
              const a = await import("./a.ts")
              const b = await import("./b.ts")
              return [a, b]
            }

            import { last } from "./last.ts"
        """)
        assert _extract_ts_imports(source) == [
            "effect",
            "./a.ts",
            "./b.ts",
            "./last.ts",
        ]

    def test_template_literal_specifier_is_skipped(self):
        """A computed specifier is not statically resolvable — emit nothing, not junk."""
        source = textwrap.dedent("""\
            const name = "alpha"
            const mod = await import(`./${name}.ts`)
        """)
        assert _extract_ts_imports(source) == []

    def test_template_literal_specifier_does_not_hide_neighbours(self):
        source = textwrap.dedent("""\
            const mod = await import(`./${name}.ts`)
            const other = await import("./other.ts")
        """)
        assert _extract_ts_imports(source) == ["./other.ts"]

    def test_identifier_ending_in_import_is_not_a_dynamic_import(self):
        source = 'notimport("./x.ts")\n'
        assert _extract_ts_imports(source) == []

    def test_method_named_import_is_not_a_dynamic_import(self):
        source = 'loader.import("./x.ts")\n'
        assert _extract_ts_imports(source) == []

    def test_importsomething_call_is_not_a_dynamic_import(self):
        source = 'importAll("./x.ts")\n'
        assert _extract_ts_imports(source) == []

    def test_relative_side_effect_import(self):
        source = 'import "./polyfill.ts"\n'
        assert _extract_ts_imports(source) == ["./polyfill.ts"]

    def test_bare_package_side_effect_import(self):
        source = 'import "reflect-metadata"\n'
        assert _extract_ts_imports(source) == ["reflect-metadata"]

    def test_side_effect_import_single_quotes(self):
        source = "import './polyfill.ts'\n"
        assert _extract_ts_imports(source) == ["./polyfill.ts"]

    def test_side_effect_import_with_semicolon(self):
        source = 'import "./polyfill.ts";\n'
        assert _extract_ts_imports(source) == ["./polyfill.ts"]

    def test_indented_side_effect_import(self):
        source = '    import "./polyfill.ts"\n'
        assert _extract_ts_imports(source) == ["./polyfill.ts"]

    def test_side_effect_import_with_trailing_comment(self):
        source = 'import "./polyfill.ts" // installs the global\n'
        assert _extract_ts_imports(source) == ["./polyfill.ts"]

    def test_side_effect_and_static_imports_are_returned_in_source_order(self):
        source = textwrap.dedent("""\
            import "reflect-metadata"
            import { Effect } from "effect"
            import "./polyfill.ts"
            import { last } from "./last.ts"
        """)
        assert _extract_ts_imports(source) == [
            "reflect-metadata",
            "effect",
            "./polyfill.ts",
            "./last.ts",
        ]

    def test_repeated_side_effect_import_is_not_deduplicated(self):
        source = textwrap.dedent("""\
            import "./polyfill.ts"
            import "./polyfill.ts"
        """)
        assert _extract_ts_imports(source) == ["./polyfill.ts", "./polyfill.ts"]

    def test_static_import_is_not_counted_twice(self):
        """`import x from "y"` must match one pattern, not both."""
        source = 'import foo from "./foo.ts"\n'
        assert _extract_ts_imports(source) == ["./foo.ts"]

    def test_multiline_clause_is_not_matched_as_a_side_effect_import(self):
        """The inner specifier of a wrapped clause is not a bare side-effect import.

        Asserted against the pattern rather than `_extract_ts_imports` so the
        guard stays true whatever the `from` pattern is later taught to match.
        """
        source = textwrap.dedent("""\
            import {
              foo,
              bar,
            } from "./foo.ts"
        """)
        assert _TS_SIDE_EFFECT_IMPORT_RE.findall(source) == []

    def test_export_from_is_not_matched_as_a_side_effect_import(self):
        source = 'export { helper } from "./helper.ts"\n'
        assert _TS_SIDE_EFFECT_IMPORT_RE.findall(source) == []

    def test_static_import_is_not_matched_as_a_side_effect_import(self):
        source = 'import foo from "./foo.ts"\n'
        assert _TS_SIDE_EFFECT_IMPORT_RE.findall(source) == []

    def test_import_inside_an_identifier_is_not_a_side_effect_import(self):
        source = 'importSomething "./x.ts"\n'
        assert _extract_ts_imports(source) == []

    def test_block_comment_inside_a_single_line_clause(self):
        source = 'import { foo, /* keep */ bar } from "./m.ts"\n'
        assert _extract_ts_imports(source) == ["./m.ts"]

    def test_block_comment_between_keyword_and_clause(self):
        source = 'import /* side note */ { foo } from "./m.ts"\n'
        assert _extract_ts_imports(source) == ["./m.ts"]

    def test_line_comment_inside_a_multiline_clause(self):
        source = textwrap.dedent("""\
            import {
              alpha,
              // beta is deprecated
              gamma,
            } from "./m.ts"
        """)
        assert _extract_ts_imports(source) == ["./m.ts"]

    def test_block_comment_inside_a_multiline_clause(self):
        source = textwrap.dedent("""\
            import {
              alpha, /* keep */
              gamma,
            } from "./m.ts"
        """)
        assert _extract_ts_imports(source) == ["./m.ts"]

    def test_multiline_block_comment_inside_a_clause(self):
        source = textwrap.dedent("""\
            import {
              alpha,
              /*
               * beta is deprecated
               */
              gamma,
            } from "./m.ts"
        """)
        assert _extract_ts_imports(source) == ["./m.ts"]

    def test_comment_inside_a_reexport_clause(self):
        source = 'export { helper /* re-exported */ } from "./helper.ts"\n'
        assert _extract_ts_imports(source) == ["./helper.ts"]

    def test_from_inside_a_line_comment_is_not_the_specifier(self):
        """A commented-out specifier must not win over the real one."""
        source = textwrap.dedent("""\
            import { foo } // from "./decoy.ts"
              from "./real.ts"
        """)
        assert _extract_ts_imports(source) == ["./real.ts"]

    def test_from_inside_a_block_comment_is_not_the_specifier(self):
        source = 'import { foo } /* from "./decoy.ts" */ from "./real.ts"\n'
        assert _extract_ts_imports(source) == ["./real.ts"]

    def test_url_specifier_is_not_read_as_a_comment(self):
        """The `//` in a Deno URL import sits after `from`, inside the specifier."""
        source = 'import { assert } from "https://deno.land/std/assert/mod.ts"\n'
        assert _extract_ts_imports(source) == [
            "https://deno.land/std/assert/mod.ts"
        ]

    def test_url_reexport_specifier_is_not_read_as_a_comment(self):
        source = 'export * from "https://deno.land/std/assert/mod.ts"\n'
        assert _extract_ts_imports(source) == [
            "https://deno.land/std/assert/mod.ts"
        ]

    def test_commented_out_import_line_is_not_extracted(self):
        """Teaching the clause about comments must not start matching inside one.

        A whole import statement behind `//` is not a dependency. The line
        anchor already excludes it; this pins that the in-clause comment
        handling does not accidentally open a door to it.
        """
        source = '// import { foo } from "./m.ts"\n'
        assert _extract_ts_imports(source) == []


# ── Function Extraction: Standard TypeScript ────────────────────────


class TestTypeScriptFunctionExtraction:
    def test_function_declaration(self):
        source = 'export function greet(name: string): string { return `Hello ${name}` }'
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "greet"]
        assert len(fns) == 1
        f = fns[0]
        assert f.name == "greet"
        assert "export" in f.decorators
        assert len(f.params) == 1
        assert f.params[0].name == "name"
        assert f.params[0].type_annotation == "string"
        assert f.return_type == "string"

    def test_async_function(self):
        source = "export async function fetchData(url: string): Promise<Response> { return fetch(url) }"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "fetchData"]
        assert len(fns) == 1
        assert fns[0].is_async is True
        assert fns[0].return_type == "Promise<Response>"

    def test_non_exported_function(self):
        source = "function helper(x: number): number { return x + 1 }"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "helper"]
        assert len(fns) == 1
        assert "export" not in fns[0].decorators

    def test_arrow_function(self):
        source = "export const add = (a: number, b: number): number => a + b"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "add"]
        assert len(fns) == 1
        f = fns[0]
        assert "export" in f.decorators
        assert len(f.params) == 2
        assert f.params[0].name == "a"
        assert f.params[1].name == "b"

    def test_async_arrow_function(self):
        source = "export const fetchItems = async (ids: string[]): Promise<Item[]> => { /* ... */ }"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "fetchItems"]
        assert len(fns) == 1
        assert fns[0].is_async is True

    def test_class_declaration(self):
        source = "export class UserService { constructor(private db: Database) {} }"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "UserService"]
        assert len(fns) == 1
        assert "class" in fns[0].decorators
        assert fns[0].return_type == "class"

    def test_interface_declaration(self):
        source = "export interface UserConfig { name: string; age: number }"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "UserConfig"]
        assert len(fns) == 1
        assert "interface" in fns[0].decorators

    def test_type_alias(self):
        source = "export type UserId = string & { readonly _brand: 'UserId' }"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "UserId"]
        assert len(fns) == 1
        assert "type" in fns[0].decorators

    def test_multiple_functions(self):
        source = textwrap.dedent("""\
            export function a(): void {}
            export function b(): void {}
            export const c = () => {}
        """)
        funcs = extract_functions_typescript("test.ts", source)
        names = [f.name for f in funcs]
        assert "a" in names
        assert "b" in names
        assert "c" in names

    def test_generator_function(self):
        source = "export function* items(): Generator<number> { yield 1 }"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "items"]
        assert len(fns) == 1
        assert "generator" in fns[0].decorators

    def test_optional_and_default_params(self):
        source = 'export function init(name: string, debug?: boolean, level: number = 1): void {}'
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "init"]
        assert len(fns) == 1
        assert len(fns[0].params) == 3
        assert fns[0].params[2].default == "1"

    def test_line_numbers(self):
        source = "\n\nexport function third(): void {}"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "third"]
        assert fns[0].line_number == 3

    def test_generic_function(self):
        source = "export function identity<T>(value: T): T { return value }"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "identity"]
        assert len(fns) == 1
        assert fns[0].return_type == "T"


# ── Function Extraction: Effect-TS Patterns ────────────────────────


class TestEffectTSExtraction:
    def test_effect_gen(self):
        source = textwrap.dedent("""\
            export const getUser = Effect.gen(function*() {
              const repo = yield* UserRepo
              return yield* repo.getById("123")
            })
        """)
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "getUser"]
        assert len(fns) == 1
        f = fns[0]
        assert "effect_gen" in f.decorators
        assert "export" in f.decorators
        assert f.is_async is True  # Effect.gen is async-like

    def test_effect_gen_with_type_annotation(self):
        source = textwrap.dedent("""\
            export const getUser: Effect.Effect<User, UserNotFound, UserRepo> = Effect.gen(function*() {
              const repo = yield* UserRepo
              return yield* repo.getById("123")
            })
        """)
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "getUser"]
        assert len(fns) == 1
        # Should extract the type annotation as return type
        assert "Effect.Effect<User, UserNotFound, UserRepo>" in fns[0].return_type

    def test_pipe_composition(self):
        source = textwrap.dedent("""\
            export const processUser = pipe(
              getUser,
              Effect.flatMap(validate),
              Effect.catchTag("UserNotFound", handleNotFound)
            )
        """)
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "processUser"]
        assert len(fns) == 1
        assert "pipe" in fns[0].decorators

    def test_layer_definition(self):
        source = textwrap.dedent("""\
            export const UserRepoLive = Layer.succeed(
              UserRepo,
              { getById: (id: string) => Effect.succeed({ id, name: "test" }) }
            )
        """)
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "UserRepoLive"]
        assert len(fns) == 1
        assert "layer" in fns[0].decorators

    def test_layer_effect(self):
        source = "export const DbLive = Layer.effect(Database, Effect.gen(function*() { /* ... */ }))"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "DbLive"]
        assert len(fns) == 1
        assert "layer" in fns[0].decorators

    def test_layer_scoped(self):
        source = "export const PoolLive = Layer.scoped(Pool, Effect.gen(function*() { /* ... */ }))"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "PoolLive"]
        assert len(fns) == 1
        assert "layer" in fns[0].decorators

    def test_context_tag_service(self):
        source = textwrap.dedent("""\
            export class UserRepo extends Context.Tag("UserRepo")<
              UserRepo,
              { getById: (id: string) => Effect.Effect<User, UserNotFound> }
            >() {}
        """)
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "UserRepo"]
        assert len(fns) == 1
        assert "service" in fns[0].decorators
        assert fns[0].return_type == "service"

    def test_data_tagged_error(self):
        source = textwrap.dedent("""\
            export class UserNotFound extends Data.TaggedError("UserNotFound")<{
              id: string
            }>() {}
        """)
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "UserNotFound"]
        assert len(fns) == 1
        assert "tagged_error" in fns[0].decorators

    def test_schema_struct(self):
        source = textwrap.dedent("""\
            export const User = Schema.Struct({
              id: Schema.String,
              name: Schema.String,
              age: Schema.Number,
            })
        """)
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "User"]
        assert len(fns) == 1
        assert "schema" in fns[0].decorators

    def test_schema_class(self):
        source = "export const User = Schema.Class<User>('User')({ id: Schema.String, name: Schema.String })"
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "User"]
        assert len(fns) == 1
        assert "schema" in fns[0].decorators

    def test_arrow_returning_effect(self):
        source = textwrap.dedent("""\
            export const getUser = (id: string): Effect.Effect<User, UserNotFound, UserRepo> =>
              Effect.gen(function*() {
                const repo = yield* UserRepo
                return yield* repo.getById(id)
              })
        """)
        funcs = extract_functions_typescript("test.ts", source)
        fns = [f for f in funcs if f.name == "getUser"]
        assert len(fns) == 1
        f = fns[0]
        # Should be detected as arrow function with Effect return
        assert len(f.params) == 1
        assert f.params[0].name == "id"

    def test_mixed_effect_file(self):
        """A realistic Effect-TS module with multiple patterns."""
        source = textwrap.dedent("""\
            import { Effect, Context, Layer, Data, Schema, pipe } from "effect"

            // Service definition
            export class UserRepo extends Context.Tag("UserRepo")<
              UserRepo,
              { getById: (id: string) => Effect.Effect<User, UserNotFound> }
            >() {}

            // Error type
            export class UserNotFound extends Data.TaggedError("UserNotFound")<{
              id: string
            }>() {}

            // Schema
            export const User = Schema.Struct({
              id: Schema.String,
              name: Schema.String,
            })

            // Effect.gen function
            export const getUser = Effect.gen(function*() {
              const repo = yield* UserRepo
              return yield* repo.getById("123")
            })

            // Layer
            export const UserRepoLive = Layer.succeed(UserRepo, {
              getById: (id) => Effect.succeed({ id, name: "test" }),
            })

            // Pipe composition
            export const program = pipe(
              getUser,
              Effect.tap(Effect.log),
            )

            // Regular function
            export function createApp(): void {}
        """)
        funcs = extract_functions_typescript("test.ts", source)
        names = {f.name for f in funcs}

        assert "UserRepo" in names
        assert "UserNotFound" in names
        assert "User" in names
        assert "getUser" in names
        assert "UserRepoLive" in names
        assert "program" in names
        assert "createApp" in names

        # Check specific types
        by_name = {f.name: f for f in funcs}
        assert "service" in by_name["UserRepo"].decorators
        assert "tagged_error" in by_name["UserNotFound"].decorators
        assert "schema" in by_name["User"].decorators
        assert "effect_gen" in by_name["getUser"].decorators
        assert "layer" in by_name["UserRepoLive"].decorators
        assert "pipe" in by_name["program"].decorators

    def test_plain_const_not_extracted(self):
        """Plain constants that aren't function-like should be skipped."""
        source = textwrap.dedent("""\
            export const MAX_RETRIES = 3
            export const DEFAULT_TIMEOUT = 5000
            export const CONFIG = { debug: false }
        """)
        funcs = extract_functions_typescript("test.ts", source)
        names = {f.name for f in funcs}
        assert "MAX_RETRIES" not in names
        assert "DEFAULT_TIMEOUT" not in names
        assert "CONFIG" not in names


# ── Relative-Import Coverage Mapping ───────────────────────────────


def _covered_names(analysis):
    return {e.function_name for e in analysis.coverage.entries if e.covered}


class TestRelativeImportCoverage:
    def test_windows_paths_are_resolved(self):
        assert _resolve_relative_specifier(
            r"src\app.test.ts",
            "./app.ts",
            {r"src\app.ts"},
        ) == r"src\app.ts"

    def test_sibling_relative_import_marks_coverage(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.ts").write_text("export function run(): void {}\n")
        (tmp_path / "src" / "app.test.ts").write_text(textwrap.dedent("""\
            import { run } from "./app.ts"
            import { describe, it, expect } from "vitest"

            describe("app", () => {
              it("runs", () => {
                expect(run()).toBeUndefined()
              })
            })
        """))

        analysis = analyze_codebase(tmp_path, language="typescript")
        assert "run" in _covered_names(analysis)

    def test_extensionless_relative_import_marks_coverage(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.ts").write_text("export function run(): void {}\n")
        (tmp_path / "src" / "app.test.ts").write_text(
            'import { run } from "./app"\nit("runs", () => { run() })\n'
        )

        analysis = analyze_codebase(tmp_path, language="typescript")
        assert "run" in _covered_names(analysis)

    def test_parent_relative_import_from_tests_subdir(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "__tests__").mkdir()
        (tmp_path / "src" / "handler.ts").write_text(
            "export function handle(): void {}\n"
        )
        (tmp_path / "src" / "__tests__" / "handler.unit.test.ts").write_text(
            'import { handle } from "../handler.ts"\nit("handles", () => { handle() })\n'
        )

        analysis = analyze_codebase(tmp_path, language="typescript")
        assert "handle" in _covered_names(analysis)

    def test_deep_parent_relative_import(self, tmp_path):
        (tmp_path / "src" / "a" / "b" / "__tests__").mkdir(parents=True)
        (tmp_path / "src" / "util.ts").write_text("export function helper(): void {}\n")
        (tmp_path / "src" / "a" / "b" / "__tests__" / "x.unit.test.ts").write_text(
            'import { helper } from "../../../util.ts"\nit("x", () => { helper() })\n'
        )

        analysis = analyze_codebase(tmp_path, language="typescript")
        assert "helper" in _covered_names(analysis)

    def test_unreferenced_function_stays_uncovered(self, tmp_path):
        """Importing the module is not enough — the name must be referenced."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.ts").write_text(textwrap.dedent("""\
            export function run(): void {}
            export function neverCalled(): void {}
        """))
        (tmp_path / "src" / "app.test.ts").write_text(
            'import { run } from "./app.ts"\nit("runs", () => { run() })\n'
        )

        analysis = analyze_codebase(tmp_path, language="typescript")
        covered = _covered_names(analysis)
        assert "run" in covered
        assert "neverCalled" not in covered

    def test_relative_import_to_missing_module_is_not_coverage(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.ts").write_text("export function run(): void {}\n")
        (tmp_path / "src" / "other.test.ts").write_text(
            'import { run } from "./nowhere.ts"\nit("x", () => { run() })\n'
        )

        analysis = analyze_codebase(tmp_path, language="typescript")
        assert "run" not in _covered_names(analysis)

    def test_relative_import_does_not_escape_project_root(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.ts").write_text("export function run(): void {}\n")
        (tmp_path / "src" / "app.test.ts").write_text(
            'import { run } from "../../../../etc/app.ts"\nit("x", () => { run() })\n'
        )

        analysis = analyze_codebase(tmp_path, language="typescript")
        assert "run" not in _covered_names(analysis)

    def test_python_dotted_imports_still_map(self):
        """Regression guard: relative resolution must not disturb Python mapping."""
        source = SourceFile(path="pkg/auth.py", language="python")
        source.functions = [
            ExtractedFunction(name="login", signature="login()", line_number=1)
        ]
        test = TestFile(path="tests/test_auth.py", language="python")
        test.imported_modules = ["pkg.auth"]
        test.referenced_names = ["login"]

        cov = map_test_coverage([source], [test])
        assert [e.covered for e in cov.entries] == [True]


# ── Cyclomatic Complexity ──────────────────────────────────────────


def _complexity_of(source: str, name: str) -> int:
    funcs = extract_functions_typescript("sample.ts", source)
    matches = [f for f in funcs if f.name == name]
    assert matches, f"{name!r} not extracted from sample; got {[f.name for f in funcs]}"
    return matches[0].complexity


class TestTypeScriptComplexity:
    def test_straight_line_function_is_one(self):
        source = textwrap.dedent("""\
            export function add(a: number, b: number): number {
              return a + b
            }
        """)
        assert _complexity_of(source, "add") == 1

    def test_if_adds_one(self):
        source = textwrap.dedent("""\
            export function clamp(n: number): number {
              if (n < 0) {
                return 0
              }
              return n
            }
        """)
        assert _complexity_of(source, "clamp") == 2

    def test_else_alone_adds_nothing(self):
        source = textwrap.dedent("""\
            export function sign(n: number): string {
              if (n < 0) {
                return "neg"
              } else {
                return "pos"
              }
            }
        """)
        assert _complexity_of(source, "sign") == 2

    def test_else_if_chain_counts_each_branch(self):
        source = textwrap.dedent("""\
            export function bucket(n: number): string {
              if (n < 0) {
                return "neg"
              } else if (n === 0) {
                return "zero"
              } else if (n < 10) {
                return "small"
              }
              return "big"
            }
        """)
        assert _complexity_of(source, "bucket") == 4

    def test_loops_add_one_each(self):
        source = textwrap.dedent("""\
            export function total(rows: number[][]): number {
              let sum = 0
              for (const row of rows) {
                let i = 0
                while (i < row.length) {
                  sum += row[i]
                  i++
                }
              }
              return sum
            }
        """)
        assert _complexity_of(source, "total") == 3

    def test_catch_adds_one(self):
        source = textwrap.dedent("""\
            export function safe(fn: () => number): number {
              try {
                return fn()
              } catch (e) {
                return 0
              }
            }
        """)
        assert _complexity_of(source, "safe") == 2

    def test_switch_counts_each_case(self):
        source = textwrap.dedent("""\
            export function name(kind: string): string {
              switch (kind) {
                case "a":
                  return "alpha"
                case "b":
                  return "beta"
                default:
                  return "other"
              }
            }
        """)
        assert _complexity_of(source, "name") == 3

    def test_logical_operators_add_one_each(self):
        source = textwrap.dedent("""\
            export function ok(a: boolean, b: boolean, c: boolean): boolean {
              return a && b || c
            }
        """)
        assert _complexity_of(source, "ok") == 3

    def test_nullish_coalescing_adds_one(self):
        source = textwrap.dedent("""\
            export function pick(a: string | null): string {
              return a ?? "fallback"
            }
        """)
        assert _complexity_of(source, "pick") == 2

    def test_ternary_adds_one(self):
        source = textwrap.dedent("""\
            export function label(n: number): string {
              return n > 0 ? "pos" : "nonpos"
            }
        """)
        assert _complexity_of(source, "label") == 2

    def test_optional_chaining_is_not_a_decision_point(self):
        source = textwrap.dedent("""\
            export function deep(o: { a?: { b?: string } }): string | undefined {
              return o?.a?.b
            }
        """)
        assert _complexity_of(source, "deep") == 1

    def test_keywords_in_strings_are_not_counted(self):
        source = textwrap.dedent("""\
            export function describe(): string {
              return "if you switch the case for a while"
            }
        """)
        assert _complexity_of(source, "describe") == 1

    def test_keywords_in_comments_are_not_counted(self):
        source = textwrap.dedent("""\
            export function plain(): number {
              // if the value is odd, while looping, switch case
              /* for example: a && b || c ?? d */
              return 1
            }
        """)
        assert _complexity_of(source, "plain") == 1

    def test_identifier_containing_keyword_is_not_counted(self):
        source = textwrap.dedent("""\
            export function run(): number {
              const iffy = 1
              const switcher = 2
              const forward = 3
              return iffy + switcher + forward
            }
        """)
        assert _complexity_of(source, "run") == 1

    def test_arrow_function_with_block_body(self):
        source = textwrap.dedent("""\
            export const check = (n: number): boolean => {
              if (n > 0) {
                return true
              }
              return false
            }
        """)
        assert _complexity_of(source, "check") == 2

    def test_arrow_with_function_typed_parameter_uses_outer_body(self):
        source = textwrap.dedent("""\
            export const run = (callback: () => void): number => {
              if (callback) {
                return 1
              }
              return 0
            }
        """)
        assert _complexity_of(source, "run") == 2

    def test_arrow_with_arrow_default_uses_outer_body(self):
        source = textwrap.dedent("""\
            export const run = (callback = () => true): number => {
              if (callback()) {
                return 1
              }
              return 0
            }
        """)
        assert _complexity_of(source, "run") == 2

    def test_arrow_function_with_expression_body(self):
        source = 'export const check = (n: number): string => n > 0 ? "y" : "n"\n'
        assert _complexity_of(source, "check") == 2

    def test_effect_gen_body_is_measured(self):
        source = textwrap.dedent("""\
            export const getUser = Effect.gen(function*() {
              const repo = yield* UserRepo
              if (repo === null) {
                return yield* Effect.fail("no repo")
              }
              return yield* repo.getById("123")
            })
        """)
        assert _complexity_of(source, "getUser") == 2

    def test_only_own_body_is_measured(self):
        """A later function's branches must not leak into an earlier one."""
        source = textwrap.dedent("""\
            export function first(): number {
              return 1
            }

            export function second(n: number): number {
              if (n > 0) {
                return 1
              }
              if (n < 0) {
                return -1
              }
              return 0
            }
        """)
        assert _complexity_of(source, "first") == 1
        assert _complexity_of(source, "second") == 3

    def test_template_literal_keywords_are_not_counted(self):
        source = textwrap.dedent("""\
            export function greet(n: string): string {
              return `if ${n} while switch`
            }
        """)
        assert _complexity_of(source, "greet") == 1

    def test_schema_declaration_stays_at_one(self):
        source = textwrap.dedent("""\
            export const UserSchema = Schema.Struct({
              id: Schema.String,
              name: Schema.String,
            })
        """)
        assert _complexity_of(source, "UserSchema") == 1


# ── Smoke Test Generation ─────────────────────────────────────────


class TestTypeScriptSmokeTests:
    def test_generates_vitest_tests(self):
        analysis = CodebaseAnalysis(
            root_path="/tmp/test",
            language="typescript",
            source_files=[
                SourceFile(path="src/utils.ts", functions=[
                    ExtractedFunction(name="add", decorators=["export"]),
                    ExtractedFunction(name="multiply", decorators=["export"]),
                ]),
            ],
        )
        suites = generate_smoke_tests(analysis, language="typescript")
        assert len(suites) == 1
        key = list(suites.keys())[0]
        assert key.endswith(".test.ts")
        code = suites[key]
        assert "import { describe, it, expect } from 'vitest'" in code
        assert "exports add" in code
        assert "exports multiply" in code
        assert "typeof mod.add" in code

    def test_skips_private_functions(self):
        analysis = CodebaseAnalysis(
            root_path="/tmp/test",
            language="typescript",
            source_files=[
                SourceFile(path="src/utils.ts", functions=[
                    ExtractedFunction(name="publicFn", decorators=["export"]),
                    ExtractedFunction(name="_privateFn", decorators=["export"]),
                ]),
            ],
        )
        suites = generate_smoke_tests(analysis, language="typescript")
        code = list(suites.values())[0]
        assert "publicFn" in code
        assert "_privateFn" not in code

    def test_effect_exports_use_defined_check(self):
        """Effect-TS values (layers, schemas) should use toBeDefined, not typeof function."""
        analysis = CodebaseAnalysis(
            root_path="/tmp/test",
            language="typescript",
            source_files=[
                SourceFile(path="src/services.ts", functions=[
                    ExtractedFunction(name="UserRepoLive", decorators=["export", "layer"]),
                    ExtractedFunction(name="User", decorators=["export", "schema"]),
                    ExtractedFunction(name="getUser", decorators=["export", "effect_gen"]),
                ]),
            ],
        )
        suites = generate_smoke_tests(analysis, language="typescript")
        code = list(suites.values())[0]
        # Layer, schema, effect_gen should NOT have typeof function check
        assert "typeof mod.UserRepoLive" not in code
        assert "typeof mod.User" not in code
        assert "typeof mod.getUser" not in code
        # But all should have toBeDefined
        assert "mod.UserRepoLive" in code
        assert "mod.User" in code
        assert "mod.getUser" in code

    def test_skips_unexported(self):
        analysis = CodebaseAnalysis(
            root_path="/tmp/test",
            language="typescript",
            source_files=[
                SourceFile(path="src/utils.ts", functions=[
                    ExtractedFunction(name="exported", decorators=["export"]),
                    ExtractedFunction(name="internal", decorators=[]),
                ]),
            ],
        )
        suites = generate_smoke_tests(analysis, language="typescript")
        code = list(suites.values())[0]
        assert "exported" in code
        # internal should only appear in the module import test, not as its own export test
        assert "exports internal" not in code

    def test_empty_codebase(self):
        analysis = CodebaseAnalysis(
            root_path="/tmp/test",
            language="typescript",
            source_files=[],
        )
        suites = generate_smoke_tests(analysis, language="typescript")
        assert suites == {}


# ── Tree Construction (TS extension handling) ─────────────────────


class TestTreeConstructionTS:
    def test_component_id_strips_ts(self):
        analysis = CodebaseAnalysis(
            root_path="/tmp/test",
            language="typescript",
            source_files=[
                SourceFile(path="src/auth/login.ts", functions=[
                    ExtractedFunction(name="authenticate"),
                ]),
            ],
        )
        tree = build_decomposition_tree(analysis)
        assert "src_auth_login" in tree.nodes

    def test_component_id_strips_js(self):
        analysis = CodebaseAnalysis(
            root_path="/tmp/test",
            language="javascript",
            source_files=[
                SourceFile(path="src/utils.js", functions=[
                    ExtractedFunction(name="helper"),
                ]),
            ],
        )
        tree = build_decomposition_tree(analysis)
        assert "src_utils" in tree.nodes


# ── Full Analysis Integration ──────────────────────────────────────


class TestAnalyzeCodebaseTS:
    def test_analyzes_typescript_project(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.ts").write_text(textwrap.dedent("""\
            import { Effect, Layer, Context, Schema, pipe } from "effect"

            export class UserRepo extends Context.Tag("UserRepo")<
              UserRepo,
              { getById: (id: string) => Effect.Effect<string> }
            >() {}

            export const getUser = Effect.gen(function*() {
              const repo = yield* UserRepo
              return yield* repo.getById("123")
            })

            export function healthCheck(): string {
              return "ok"
            }

            export const UserRepoLive = Layer.succeed(UserRepo, {
              getById: (id) => Effect.succeed(id),
            })
        """))

        analysis = analyze_codebase(tmp_path, language="typescript")
        assert analysis.total_source_files >= 1
        assert analysis.total_functions >= 3

        # Check specific extractions
        sf = analysis.source_files[0]
        names = {f.name for f in sf.functions}
        assert "UserRepo" in names
        assert "getUser" in names
        assert "healthCheck" in names
        assert "UserRepoLive" in names

    def test_empty_ts_project(self, tmp_path):
        analysis = analyze_codebase(tmp_path, language="typescript")
        assert analysis.total_functions == 0
        assert analysis.total_source_files == 0

    def test_discovers_ts_tests(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.ts").write_text("export function run(): void {}")
        (tmp_path / "src" / "app.test.ts").write_text(textwrap.dedent("""\
            import { run } from './app'
            import { describe, it, expect } from 'vitest'

            describe('app', () => {
              it('should run', () => {
                expect(run()).toBeUndefined()
              })
            })
        """))

        analysis = analyze_codebase(tmp_path, language="typescript")
        assert analysis.total_source_files >= 1
        assert analysis.total_test_files >= 1

        # Source should not include test files
        source_paths = [sf.path for sf in analysis.source_files]
        assert "src/app.ts" in source_paths
        assert "src/app.test.ts" not in source_paths


# ── Dry-Run Adoption Integration ──────────────────────────────────


class TestAdoptTypeScript:
    @pytest.mark.asyncio
    async def test_dry_run_ts_project(self, tmp_path):
        """Full dry-run adoption of a TypeScript Effect project."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.ts").write_text(textwrap.dedent("""\
            import { Effect, Context, Layer } from "effect"

            export class AppConfig extends Context.Tag("AppConfig")<
              AppConfig,
              { port: number; debug: boolean }
            >() {}

            export const startServer = Effect.gen(function*() {
              const config = yield* AppConfig
              console.log("starting on port", config.port)
            })

            export const AppConfigLive = Layer.succeed(AppConfig, {
              port: 3000,
              debug: false,
            })

            export function healthCheck(): string {
              return "ok"
            }
        """))

        from pact.adopt import adopt_codebase

        result = await adopt_codebase(tmp_path, language="typescript", dry_run=True)
        assert result.dry_run is True
        assert result.components >= 1
        assert result.total_functions >= 3  # AppConfig, startServer, AppConfigLive, healthCheck
        assert result.smoke_tests_generated >= 1

        # Check smoke test was generated
        smoke_dir = tmp_path / "tests" / "smoke"
        assert smoke_dir.exists()
        test_files = list(smoke_dir.glob("*.test.ts"))
        assert len(test_files) >= 1

        # Verify content has vitest imports
        code = test_files[0].read_text()
        assert "vitest" in code
        assert "healthCheck" in code
