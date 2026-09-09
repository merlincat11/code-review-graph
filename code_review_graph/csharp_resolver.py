"""Bind C# calls using declaration identity and lexical namespace evidence.

Raw references survive binding: every pass can revoke a previously inferred
edge when declarations or usings change. This is a structural binder, not a
C# compiler; unsupported type expressions and ambiguous declarations stay raw.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore


def _extra(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _parents(name: str):
    while name:
        yield name
        name = name.rpartition(".")[0]
    yield ""


def _join(parent: str, name: str) -> str:
    return f"{parent}.{name}" if parent else name


def resolve_csharp_calls(store: GraphStore, repo_root: Path | None = None) -> dict:
    conn = store._conn
    nodes = conn.execute("SELECT * FROM nodes WHERE language = 'csharp'").fetchall()
    files = {n["file_path"] for n in nodes}
    if not files:
        return {"files_indexed": 0, "calls_resolved": 0}
    root = repo_root or Path(os.path.commonpath([str(Path(f).parent) for f in files]))

    @lru_cache(maxsize=None)
    def project(directory: Path) -> str | None:
        try:
            projects = list(directory.glob("*.csproj"))
        except OSError:
            return None
        if projects:
            return str(projects[0]) if len(projects) == 1 else None
        if directory == root or directory.parent == directory:
            # Loose .cs files share the review root. Resolving linked or
            # conditional source ownership properly needs MSBuild compile-item
            # evaluation, which is outside this pass.
            return str(root)
        return project(directory.parent)

    projects = {f: project(Path(f).parent) for f in files}
    types: dict[str, list[str]] = {}
    methods: dict[tuple[str, str], list[str]] = {}
    static_methods: set[str] = set()
    parents: dict[str, str] = {}
    partial_types: set[str] = set()
    type_files: dict[str, str] = {}
    namespaces = {""}
    for node in nodes:
        extra = _extra(node["extra"])
        namespace = extra.get("csharp_namespace")
        if node["kind"] == "File":
            for name in extra.get("csharp_namespaces", []):
                namespaces.update(_parents(name))
        if not isinstance(namespace, str):
            continue  # Legacy nodes do not establish namespace identity.
        namespaces.update(_parents(namespace))
        qualified = node["qualified_name"]
        parent = node["parent_name"] or ""
        parents[qualified] = parent
        if node["kind"] == "Class" and not extra.get("csharp_generic"):
            types.setdefault(_join(parent, node["name"]), []).append(qualified)
            type_files[qualified] = node["file_path"]
            if extra.get("csharp_partial"):
                partial_types.add(qualified)
        elif node["kind"] in ("Function", "Method", "Test"):
            method_key = (f"{node['file_path']}::{parent}", node["name"])
            methods.setdefault(method_key, []).append(qualified)
            if extra.get("csharp_static"):
                static_methods.add(qualified)

    imports: dict[tuple[str, int], list[tuple[str, dict]]] = {}
    global_imports: dict[str, list[tuple[str, dict]]] = {}
    csharp_files_sql = "SELECT file_path FROM nodes WHERE kind = 'File' AND language = 'csharp'"
    for row in conn.execute(
        f"SELECT * FROM edges WHERE kind = 'IMPORTS_FROM' AND file_path IN ({csharp_files_sql})",
    ):
        extra = _extra(row["extra"])
        scopes = extra.get("csharp_scopes")
        if row["file_path"] not in files or not scopes:
            continue
        directive = (row["target_qualified"], extra)
        scope = scopes[0][1]
        if extra.get("csharp_global") and scope == -1:
            # Ambiguous project ownership never exports a using to other files.
            key = projects[row["file_path"]] or row["file_path"]
            global_imports.setdefault(key, []).append(directive)
        else:
            imports.setdefault((row["file_path"], scope), []).append(directive)

    def import_target(raw: str, extra: dict) -> str | None:
        absolute = raw.startswith("global::")
        raw = raw.removeprefix("global::")
        if not all(part.isidentifier() for part in raw.split(".")):
            return None
        first = raw.split(".", 1)[0]
        namespace = extra["csharp_scopes"][0][0]
        for parent in ([""] if absolute else _parents(namespace)):
            prefix = _join(parent, first)
            if prefix in namespaces or prefix in types:
                target = _join(parent, raw)
                return target if target in namespaces or target in types else None
        return None

    def directives(file: str, scope: int) -> list[tuple[str, dict]]:
        local = imports.get((file, scope), [])
        if scope == -1:
            return local + global_imports.get(projects[file] or file, [])
        return local

    def type_targets(raw: str, owner: str, scopes: list, file: str) -> list[str]:
        absolute = raw.startswith("global::")
        # Nullable receivers use the underlying declaration for member lookup;
        # the original annotation remains in the call's stored evidence.
        raw = raw.removeprefix("global::").removesuffix("?")
        alias, separator, reference = raw.partition("::")
        if separator:
            if not alias.isidentifier() or not all(p.isidentifier() for p in reference.split(".")):
                return []
            # Unlike '.', '::' searches only namespace aliases, even when a
            # local, type, or namespace has the same name as the qualifier.
            for _, scope in scopes:
                aliases = [d for d in directives(file, scope) if d[1].get("csharp_alias") == alias]
                if not aliases:
                    continue
                if len(aliases) != 1:
                    return []
                target = import_target(*aliases[0])
                return types.get(_join(target, reference), []) if target in namespaces else []
            return []
        # Keep generic arguments lossless; erasure could select a different
        # declaration (I vs I<T>). Generic binding belongs to #943.
        if not all(part.isidentifier() for part in raw.split(".")):
            return []
        first, _, tail = raw.partition(".")
        if absolute:
            return types.get(raw, [])
        namespace = scopes[0][0]
        for enclosing in _parents(owner):
            if enclosing == namespace:
                break
            if _join(enclosing, first) in types:
                return types.get(_join(enclosing, raw), [])
        scope_by_namespace = {name: scope for name, scope in scopes}
        for enclosing in _parents(namespace):
            visible = (
                directives(file, scope_by_namespace[enclosing])
                if enclosing in scope_by_namespace else []
            )
            aliases = [
                (target, extra) for target, extra in visible
                if extra.get("csharp_alias") == first
            ]
            prefix = _join(enclosing, first)
            if prefix in namespaces or prefix in types:
                return [] if aliases else types.get(_join(enclosing, raw), [])
            if aliases:
                if len(aliases) != 1:
                    return []
                alias = import_target(*aliases[0])
                return types.get(_join(alias, tail) if tail else alias, []) if alias else []
            imported = set()
            for target, extra in visible:
                kind = extra.get("csharp_using_kind")
                if kind not in ("namespace", "static"):
                    continue
                imported_scope = import_target(target, extra)
                if imported_scope is None:
                    continue
                # Using a namespace imports its types, never child namespaces.
                if kind == "namespace" and imported_scope not in namespaces:
                    continue
                if kind == "static" and imported_scope not in types:
                    continue
                candidate = _join(imported_scope, first)
                if candidate in types:
                    imported.add(candidate)
            if imported:
                if len(imported) != 1:
                    return []
                prefix = next(iter(imported))
                return types.get(_join(prefix, tail) if tail else prefix, [])
        return []

    def targets(row, extra: dict) -> list[str]:
        scopes = extra.get("csharp_scopes")
        if not scopes or extra.get("receiver_resolution") == "shadowed_callable":
            return []
        # Initializers have a File caller but still belong to a lexical type.
        owner = extra.get("csharp_containing_type") or parents.get(
            row["source_qualified"], scopes[0][0],
        )
        kind = extra.get("csharp_call_kind")
        method = extra["csharp_raw_target"].rsplit("::", 1)[-1]
        receiver = extra.get("receiver_scope", "")
        for enclosing in (_parents(owner) if kind == "unqualified" else (owner,)):
            if kind == "unqualified" and enclosing == scopes[0][0]:
                break  # Only containing types, never namespaces or imported static members.
            if extra.get("receiver") == "this" or kind == "unqualified":
                own_type = f"{row['file_path']}::{enclosing}"
                candidates = [own_type] if own_type in parents else []
                if own_type in partial_types:
                    candidates = [
                        c for c in types.get(enclosing, []) if c in partial_types
                        and projects[type_files[c]] == projects[row["file_path"]]
                    ]
            else:
                candidates = type_targets(receiver, owner, scopes, row["file_path"])
            if len(candidates) > 1:
                owners = {projects[type_files[c]] for c in candidates}
                if (
                    not all(c in partial_types for c in candidates)
                    or len(owners) != 1 or None in owners
                ):
                    return []
            if kind == "constructor":
                return candidates
            found = [qn for c in candidates for qn in methods.get((c, method), [])]
            if found:
                if enclosing != owner and (len(found) != 1 or found[0] not in static_methods):
                    return []
                return found
        return []

    resolved = 0
    changed = False
    for row in conn.execute(
        f"SELECT * FROM edges WHERE kind = 'CALLS' AND file_path IN ({csharp_files_sql})",
    ).fetchall():
        extra = _extra(row["extra"])
        raw = extra.get("csharp_raw_target")
        if row["file_path"] not in files or not isinstance(raw, str):
            continue
        candidates = targets(row, extra)
        target = candidates[0] if len(candidates) == 1 else raw
        for key in ("unresolved_targets", "scoped_resolved", "scoped_via"):
            extra.pop(key, None)
        if len(candidates) == 1:
            extra.update(scoped_resolved=True, scoped_via="csharp_namespace")
        else:
            extra["unresolved_targets"] = []
        if _set_call(store, row, target, extra):
            resolved += len(candidates) == 1
            changed = True
    if changed:
        conn.commit()
        store._invalidate_cache()
    return {"files_indexed": len(files), "calls_resolved": resolved}


def _set_call(store: GraphStore, row, target: str, extra: dict) -> bool:
    if target == row["target_qualified"] and extra == _extra(row["extra"]):
        return False
    serialized = json.dumps(extra, sort_keys=True)
    tier = "INFERRED" if extra.get("scoped_resolved") else "EXTRACTED"
    store._conn.execute(
        "UPDATE edges SET target_qualified = ?, extra = ?, confidence_tier = ? WHERE id = ?",
        (target, serialized, tier, row["id"]),
    )
    # Match old endpoint and evidence: two calls on one line can have mirrors.
    store._conn.execute(
        "UPDATE edges SET source_qualified = ?, extra = ?, confidence_tier = ? "
        "WHERE kind = 'TESTED_BY' AND source_qualified = ? AND target_qualified = ? "
        "AND file_path = ? AND line = ? AND extra = ?",
        (target, serialized, tier, row["target_qualified"], row["source_qualified"],
         row["file_path"], row["line"], row["extra"]),
    )
    if target != row["target_qualified"]:
        # Persist alongside the edge: callers and entry points outside the
        # parsed files may change, including when restoring a deleted target.
        store._conn.execute(
            "INSERT OR IGNORE INTO metadata (key, value) VALUES ('csharp_flows_dirty', '1')",
        )
    return True


def restore_csharp_references(store: GraphStore, deleted_files: list[str]) -> None:
    """Restore raw incoming calls before permanent deletion removes their targets."""
    if not deleted_files:
        return
    deleted = set(deleted_files)
    rows = store._conn.execute(
        "SELECT e.*, n.file_path AS target_file FROM edges e "
        "JOIN nodes n ON e.target_qualified = n.qualified_name "
        "WHERE e.kind = 'CALLS' AND n.language = 'csharp'",
    ).fetchall()
    changed = False
    for row in rows:
        extra = _extra(row["extra"])
        raw = extra.get("csharp_raw_target")
        if row["target_file"] not in deleted or not isinstance(raw, str):
            continue
        extra.pop("scoped_resolved", None)
        extra.pop("scoped_via", None)
        extra["unresolved_targets"] = []
        changed = _set_call(store, row, raw, extra) or changed
    if changed:
        store.commit()
        store._invalidate_cache()
