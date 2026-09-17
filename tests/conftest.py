"""Shared test fixtures.

Keeps code-review-graph's own per-user state out of the developer's real
home directory. Scoped deliberately: the editor-integration installers in
``skills.py`` write to other user-level locations (``~/.codex``,
``~/.cursor``, ``~/.config/opencode``) that are outside CRG state and are
not covered here — those tests patch ``Path.home()`` themselves.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep the ``packaging`` gate out of an ordinary ``pytest tests/`` run.

    ``tests/test_packaging.py`` builds a wheel and an sdist, creates three
    virtual environments and installs into each of them over the network. It
    is a release gate, not a per-commit check, so registering the marker is
    not enough -- it has to be off by default.

    It runs when the marker expression names it (``-m packaging``,
    ``-m "packaging or e2e"``) or when ``CRG_RUN_PACKAGING_TESTS=1`` is set.
    """
    if "packaging" in (config.option.markexpr or ""):
        return
    if os.environ.get("CRG_RUN_PACKAGING_TESTS") == "1":
        return
    skip = pytest.mark.skip(
        reason="packaging gate: run with `-m packaging` or CRG_RUN_PACKAGING_TESTS=1"
    )
    for item in items:
        if "packaging" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolated_crg_home(tmp_path_factory, monkeypatch):
    """Redirect the per-user state directory into a temporary directory.

    ``~/.code-review-graph`` holds ``registry.json``, ``watch.toml``,
    ``daemon.pid``, ``daemon-state.json`` and ``logs/``. Two paths reached
    the real one:

    * ``Registry()`` defaults there, and ``incremental.get_data_dir()``
      constructs one internally — so any test touching data-dir resolution
      both read and wrote the registry of whoever ran the suite. That put
      pytest tmp paths into a developer's home directory, and made those
      tests depend on machine state: a developer with a registered repo
      could get different results from one without.
    * ``daemon`` built its config/PID/state paths from ``Path.home()``.

    Autouse and unconditional: an opt-in fixture would silently stop
    protecting a test the day someone forgets to request it.
    """
    home = tmp_path_factory.mktemp("crg-home")
    monkeypatch.setenv("CRG_HOME", str(home))
    # The Hermes Agent installer resolves its config from ``HERMES_HOME``,
    # falling back to ``~/.hermes``. That fallback reaches the real user
    # config in any test that does not also patch ``Path.home()``, so pin
    # the variable to a temp directory instead of merely clearing it:
    # unset, a miss would be silently destructive; set, it cannot be.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path_factory.mktemp("hermes-home")))
    return home


# Opt-in markers: suites slow or invasive enough that the ordinary run must not
# pay for them. A suite listed here is collected but skipped unless the run's
# ``-m`` expression names its marker, so ``pytest tests/`` stays fast while
# ``pytest -m <marker>`` still finds the tests. Add a marker name to the set
# (and to the ``markers`` list in pyproject.toml) to gate another suite.
_OPT_IN_MARKERS = frozenset({"cli_surface", "corpus", "determinism", "platform_lifecycle"})


def pytest_collection_modifyitems(config, items):
    """Skip opt-in suites unless the run explicitly selects their marker."""
    expression = config.getoption("-m", default="") or ""
    gated = {name for name in _OPT_IN_MARKERS if name not in expression}
    if not gated:
        return
    for item in items:
        for name in gated:
            if item.get_closest_marker(name) is not None:
                item.add_marker(
                    pytest.mark.skip(reason=f"opt-in suite; run with -m {name}")
                )
                break
