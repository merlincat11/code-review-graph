"""Namespace identity and binding regressions for #946, through persisted graphs."""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import CSHARP_IDENTITY_VERSION, incremental_update
from code_review_graph.parser import CodeParser
from code_review_graph.scoped_resolver import resolve_scoped_calls


def _build(root: Path, files: dict[str, str]) -> GraphStore:
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    parser = CodeParser(root)
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        if path.suffix == ".cs":
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(
                str(path), nodes, edges, hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    store.set_metadata("csharp_identity_version", CSHARP_IDENTITY_VERSION)
    resolve_scoped_calls(store, root)
    store.resolve_bare_call_targets()
    store.resolve_bare_tested_by_sources()
    return store


def _calls(store: GraphStore, caller: str):
    return store._conn.execute(
        "SELECT * FROM edges WHERE kind = 'CALLS' AND source_qualified LIKE ? ORDER BY line, id",
        (f"%::{caller}",),
    ).fetchall()


@pytest.mark.parametrize(("source", "caller", "expected"), [
    (
        "namespace Other { class App { public class Report { public class ExportHandler "
        "{ public static void Run() {} } } } } "
        "namespace Consumer { class C { void Go() { App.Report.ExportHandler.Run(); } } }",
        "Consumer.C.Go", None,
    ),
    (
        "namespace Other { class Service { public static void Run() {} } } "
        "namespace Consumer { class C { void Go() { Service.Run(); } } }",
        "Consumer.C.Go", None,
    ),
    (
        "namespace Other { class Service { public static void Run() {} } } "
        "namespace Consumer { class C { void Go() { Other.Service.Run(); } } }",
        "Consumer.C.Go", "Other.Service.Run",
    ),
    (
        "namespace Other { class Service { public static void Run() {} } } "
        "namespace A { using Other; class C { void Go() { Service.Run(); } } } "
        "namespace B { class C { void Go() { Service.Run(); } } }",
        "B.C.Go", None,
    ),
    (
        "namespace Other { class Service { public static void Run() {} } } "
        "namespace A { using Other; class First {} } "
        "namespace A { class C { void Go() { Service.Run(); } } }",
        "A.C.Go", None,
    ),
    (
        "namespace Other { class Service { public static void Run() {} } } "
        "namespace A { using Other; namespace B { class C { void Go() { Service.Run(); } } } }",
        "A.B.C.Go", "Other.Service.Run",
    ),
    (
        "namespace A { class Service { public static void Run() {} } } "
        "namespace B { class Service {} } "
        "namespace Consumer { using A; using B; class C { void Go() { Service.Run(); } } }",
        "Consumer.C.Go", None,
    ),
    (
        "namespace A.B { class Service { public static void Run() {} } } "
        "namespace Consumer { using A; class C { void Go() { B.Service.Run(); } } }",
        "Consumer.C.Go", None,
    ),
    (
        "namespace A { class Service { public static void Run() {} } } "
        "namespace Consumer { using Alias = global::A; "
        "class C { void Go() { Alias.Service.Run(); } } }",
        "Consumer.C.Go", "A.Service.Run",
    ),
    (
        "namespace A { class Service { public static void Run() {} } } "
        "namespace Consumer { class A {} class C { void Go() { A.Service.Run(); } } }",
        "Consumer.C.Go", None,
    ),
    (
        "namespace A { class Service { public static void Run() {} } } "
        "namespace Consumer { class A {} class C { void Go() { global::A.Service.Run(); } } }",
        "Consumer.C.Go", "A.Service.Run",
    ),
    (
        "namespace A { class Outer { public class Service { public static void Run() {} } "
        "class C { public class Service {} void Go() { Service.Run(); } } } }",
        "A.Outer.C.Go", None,
    ),
    (
        "namespace A { class Outer { public class D { public class E { "
        "public static void Run() {} } } class C { void Go() { D.E.Run(); } } } } "
        "class D { public class E { public static void Run() {} } }",
        "A.Outer.C.Go", "A.Outer.D.E.Run",
    ),
    (
        "namespace A { class C { public void Run() {} void Go() { Run(); } } } "
        "namespace B { class C { public void Run() {} } }",
        "A.C.Go", "A.C.Run",
    ),
    (
        "namespace @A { namespace /* trivia */ B { "
        "class @Service { public static void @Run() {} } } } "
        "namespace Consumer { using /* trivia */ Alias = A.B; "
        "class C { void Go() { Alias.Service.Run(); } } }",
        "Consumer.C.Go", "A.B.Service.Run",
    ),
])
def test_namespace_binding(source, caller, expected, tmp_path):
    with _build(tmp_path, {"Case.cs": source}) as store:
        calls = _calls(store, caller)
        assert len(calls) == 1
        extra = json.loads(calls[0]["extra"])
        if expected:
            assert calls[0]["target_qualified"] == f"{tmp_path / 'Case.cs'}::{expected}"
            assert "unresolved_targets" not in extra
        else:
            assert calls[0]["target_qualified"] == extra["csharp_raw_target"]
            assert "unresolved_targets" in extra


def test_same_file_identities_and_same_line_calls_survive_storage(tmp_path):
    with _build(tmp_path, {"Case.cs": """
namespace A { class Outer { public class Inner { public static void Run() {} } } }
namespace B { class Outer { public class Inner { public static void Run() {} } } }
class Caller { void Go() { A.Outer.Inner.Run(); B.Outer.Inner.Run(); } }
"""}) as store:
        calls = _calls(store, "Caller.Go")
        assert {row["target_qualified"].split("::")[1] for row in calls} == {
            "A.Outer.Inner.Run", "B.Outer.Inner.Run",
        }
        nodes = store.get_nodes_by_file(str(tmp_path / "Case.cs"))
        assert len([node for node in nodes if node.name == "Run"]) == 2
        contains = store._conn.execute(
            "SELECT source_qualified, target_qualified FROM edges WHERE kind = 'CONTAINS'",
        ).fetchall()
        assert (f"{tmp_path / 'Case.cs'}::A.Outer", f"{tmp_path / 'Case.cs'}::A.Outer.Inner") in [
            tuple(row) for row in contains
        ]


def test_repeated_usings_on_one_line_retain_both_namespace_bodies(tmp_path):
    with _build(tmp_path, {"Case.cs":
        "namespace Other { class S { public static void Run() {} } } "
        "namespace A { using Other; class C { void Go() { S.Run(); } } } "
        "namespace B { using Other; class C { void Go() { S.Run(); } } }"
    }) as store:
        for caller in ("A.C.Go", "B.C.Go"):
            assert _calls(store, caller)[0]["target_qualified"].endswith("::Other.S.Run")


@pytest.mark.parametrize("inside", [False, True])
def test_using_before_and_after_file_scoped_namespace_has_distinct_lookup(tmp_path, inside):
    caller = "namespace App; using Other;" if inside else "using Other; namespace App;"
    with _build(tmp_path, {
        "Caller.cs": caller + " class C { void Go() { S.Run(); } }",
        "Types.cs": "namespace Other { class S { public static void Run() {} } } "
                    "namespace App.Other { class S { public static void Run() {} } }",
    }) as store:
        namespace = "App.Other" if inside else "Other"
        assert _calls(store, "App.C.Go")[0]["target_qualified"].endswith(f"::{namespace}.S.Run")


def test_partial_type_methods_keep_their_declaring_file(tmp_path):
    with _build(tmp_path, {
        "First.cs": "namespace A; partial class C { void Go() { Run(); } }",
        "Second.cs": "namespace A; partial class C { public void Run() {} }",
        "Caller.cs": "using A; class Consumer { void Go(C value) { value.Run(); } }",
    }) as store:
        for caller in ("A.C.Go", "Consumer.Go"):
            assert _calls(store, caller)[0]["target_qualified"] == (
                f"{tmp_path / 'Second.cs'}::A.C.Run"
            )


def test_generic_reference_is_not_erased_to_a_non_generic_declaration(tmp_path):
    with _build(tmp_path, {
        "Plain.cs": "namespace A; class I { public void Run() {} }",
        "Generic.cs": "namespace A; class I<T> { public void Run() {} }",
        "Caller.cs": "using A; class C { void Go(I<int> value) { value.Run(); } }",
    }) as store:
        call = _calls(store, "C.Go")[0]
        assert call["target_qualified"] == "I<int>::Run"
        assert json.loads(call["extra"])["receiver_scope"] == "I<int>"


def test_bare_type_receiver_does_not_select_generic_declaration(tmp_path):
    with _build(tmp_path, {
        "Generic.cs": "namespace A; class I<T> { public static void Run() {} }",
        "Caller.cs": "using A; class C { void Go() { I.Run(); } }",
    }) as store:
        assert _calls(store, "C.Go")[0]["target_qualified"] == "I::Run"


def test_this_does_not_bind_an_enclosing_types_instance_method(tmp_path):
    with _build(tmp_path, {"Case.cs":
        "namespace A { class Outer { public void Run() {} "
        "class Inner { void Go() { this.Run(); } } } }"
    }) as store:
        assert _calls(store, "A.Outer.Inner.Go")[0]["target_qualified"] == "this::Run"


@pytest.mark.parametrize("using", ["using Other;", "global using Other;"])
def test_file_scoped_namespace_and_typed_receiver(tmp_path, using):
    with _build(tmp_path, {
        "Imports.cs": "global using Other;" if using.startswith("global") else "",
        "Caller.cs": (
            (using if not using.startswith("global") else "")
            + "namespace Consumer; class C { "
            "void Go(global::Other.Service value) { value.Run(); } }"
        ),
        "Other.cs": "namespace Other; class Service { public void Run() {} }",
    }) as store:
        assert _calls(store, "Consumer.C.Go")[0]["target_qualified"] == (
            f"{tmp_path / 'Other.cs'}::Other.Service.Run"
        )


def test_global_usings_are_project_scoped_and_rebound_on_update(tmp_path):
    with _build(tmp_path, {
        "One/One.csproj": "<Project />",
        "Two/Two.csproj": "<Project />",
        "One/Imports.cs": "global using Other;",
        "One/Caller.cs": "namespace One; class C { void Go() { Service.Run(); } }",
        "Two/Caller.cs": "namespace Two; class C { void Go() { Service.Run(); } }",
        "One/Other.cs": "namespace Other; class Service { public static void Run() {} }",
    }) as store:
        assert _calls(store, "One.C.Go")[0]["target_qualified"].endswith("::Other.Service.Run")
        assert _calls(store, "Two.C.Go")[0]["target_qualified"] == "Service::Run"
        (tmp_path / "One/Imports.cs").write_text("// removed", encoding="utf-8")
        incremental_update(tmp_path, store, changed_files=["One/Imports.cs"])
        assert _calls(store, "One.C.Go")[0]["target_qualified"] == "Service::Run"
        (tmp_path / "One/Imports.cs").write_text("global using Other;", encoding="utf-8")
        incremental_update(tmp_path, store, changed_files=["One/Imports.cs"])
        assert _calls(store, "One.C.Go")[0]["target_qualified"].endswith("::Other.Service.Run")


def test_global_using_and_tested_by_survive_repeated_passes(tmp_path):
    with _build(tmp_path, {
        "Imports.cs": "global using Other;",
        "CaseTests.cs": "class CaseTests { void TestRun() { Service.Run(); } }",
        "Other.cs": "namespace Other; class Service { public static void Run() {} }",
    }) as store:
        first = [tuple(r) for r in store._conn.execute("SELECT * FROM edges ORDER BY id")]
        assert resolve_scoped_calls(store, tmp_path)["calls_resolved"] == 0
        assert first == [tuple(r) for r in store._conn.execute("SELECT * FROM edges ORDER BY id")]
        mirror = store._conn.execute("SELECT * FROM edges WHERE kind = 'TESTED_BY'").fetchone()
        assert mirror["source_qualified"].endswith("::Other.Service.Run")
        (tmp_path / "Imports.cs").write_text("// removed", encoding="utf-8")
        incremental_update(tmp_path, store, changed_files=["Imports.cs"])
        store.resolve_bare_call_targets()
        store.resolve_bare_tested_by_sources()
        mirror = store._conn.execute("SELECT * FROM edges WHERE kind = 'TESTED_BY'").fetchone()
        assert mirror["source_qualified"] == "Service::Run"
        assert "unresolved_targets" in json.loads(mirror["extra"])


@pytest.mark.parametrize("ambiguous_project", [False, True])
def test_global_alias_requires_known_shared_project_ownership(tmp_path, ambiguous_project):
    files = {
        "One.csproj": "<Project />",
        "Imports.cs": "global using Alias = Other.Service;",
        "Caller.cs": "class C { void Go() { Alias.Run(); } }",
        "Service.cs": "namespace Other; class Service { public static void Run() {} }",
    }
    if ambiguous_project:
        files["Two.csproj"] = "<Project />"
    with _build(tmp_path, files) as store:
        target = _calls(store, "C.Go")[0]["target_qualified"]
        if ambiguous_project:
            assert target == "Alias::Run"
        else:
            assert target.endswith("::Other.Service.Run")


def test_upgrade_retries_only_failed_files_and_bypasses_unchanged_hash(tmp_path):
    with _build(tmp_path, {
        "Good.cs": "namespace Good; class C { public void Run() {} }",
        "Bad.cs": "namespace Bad; class C { public void Run() {} }",
        "untouched.py": "def f(): pass",
    }) as store:
        store.set_metadata("csharp_identity_version", "1")
        # Seed the actual legacy format while retaining the current file hash.
        for name in ("Good", "Bad"):
            path = tmp_path / f"{name}.cs"
            nodes, edges = CodeParser().parse_file(path)
            for node in nodes:
                node.parent_name = (node.parent_name or "").removeprefix(name).lstrip(".") or None
                node.extra.pop("csharp_namespace", None)
            for edge in edges:
                edge.source = edge.source.replace(f"::{name}.", "::")
                edge.target = edge.target.replace(f"::{name}.", "::")
            store.store_file_nodes_edges(
                str(path), nodes, edges, hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        parser = CodeParser.parse_bytes
        attempts = []

        def fail_bad(self, path, source):
            attempts.append(path.name)
            if path.name == "Bad.cs":
                raise ValueError("persistent parser failure")
            return parser(self, path, source)

        with patch.object(CodeParser, "parse_bytes", fail_bad):
            upgraded = incremental_update(tmp_path, store, changed_files=[])
            assert upgraded["identity_rebuild"]
            assert set(attempts) == {"Good.cs", "Bad.cs"}
            assert store.get_node(f"{tmp_path / 'Good.cs'}::Good.C.Run") is not None
            assert store.get_node(f"{tmp_path / 'Good.cs'}::C.Run") is None
            attempts.clear()
            retried = incremental_update(tmp_path, store, changed_files=[])
            assert attempts == ["Bad.cs"]
            assert len(retried["errors"]) == 1
        recovered = incremental_update(tmp_path, store, changed_files=[])
        assert recovered["files_updated"] == 1
        assert store.get_metadata("csharp_identity_pending_files") == "[]"
        assert store.get_node(f"{tmp_path / 'Bad.cs'}::Bad.C.Run") is not None
        assert store.get_node(f"{tmp_path / 'Bad.cs'}::C.Run") is None


def test_deleted_callee_keeps_raw_reference_for_recreation(tmp_path):
    with _build(tmp_path, {
        "Caller.cs": "using Other; class C { void Go() { Service.Run(); } }",
        "Other.cs": "namespace Other; class Service { public static void Run() {} }",
    }) as store:
        source = (tmp_path / "Other.cs").read_text()
        (tmp_path / "Other.cs").unlink()
        incremental_update(tmp_path, store, changed_files=["Other.cs"])
        assert _calls(store, "C.Go")[0]["target_qualified"] == "Service::Run"
        (tmp_path / "Other.cs").write_text(source, encoding="utf-8")
        incremental_update(tmp_path, store, changed_files=["Other.cs"])
        assert _calls(store, "C.Go")[0]["target_qualified"].endswith("::Other.Service.Run")
