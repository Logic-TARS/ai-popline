from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_main():
    spec = importlib.util.spec_from_file_location("clean_cache", Path("scripts/clean_cache.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.main


main = _load_main()

CACHE_FILES = (
    "src/__pycache__/app.cpython-311.pyc",
    "specs/__pycache__/tool.pyc",
    ".pytest_cache/v/cache/lastfailed",
    ".ruff_cache/0.8.0/cache",
    ".mypy_cache/3.11/module.json",
    ".hypothesis/examples/case",
    ".ipynb_checkpoints/notebook.ipynb",
    "src/loose.pyo",
    ".coverage",
    ".coverage.host.1234",
)

BUILD_FILES = (
    "build/lib/pkg/module.py",
    "dist/ai_pipeline-0.1.0.tar.gz",
    "pkg.egg-info/PKG-INFO",
    "htmlcov/index.html",
)

KEPT_FILES = (
    "src/app.py",
    "README.md",
    "notes/notes.pyc.txt",
    ".venv/lib/site-packages/pkg/__pycache__/cached.pyc",
    "output/job/__pycache__/job.pyc",
    "data/logs/app.log",
    "logs/run.log",
    ".git/objects/ab/cdef",
    ".codegraph/index.bin",
    "node_modules/pkg/cache.db",
    # a second environment that is nested outside .venv/
    "dist/check-venv/pyvenv.cfg",
    "dist/check-venv/Lib/site-packages/pkg/__pycache__/vendored.pyc",
)


def _build_tree(root: Path) -> None:
    for relative in CACHE_FILES + BUILD_FILES + KEPT_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("payload", encoding="utf-8")


def _absent(root: Path, relatives) -> list[str]:
    return [relative for relative in relatives if not (root / relative).exists()]


def test_dry_run_lists_candidates_and_deletes_nothing(tmp_path: Path, capsys) -> None:
    _build_tree(tmp_path)

    assert main(["--root", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    assert "dry run" in captured.out
    assert "src/__pycache__" in captured.out
    assert ".coverage" in captured.out
    assert _absent(tmp_path, CACHE_FILES) == []
    assert _absent(tmp_path, BUILD_FILES) == []


def test_apply_removes_caches_and_keeps_protected_trees(tmp_path: Path) -> None:
    _build_tree(tmp_path)

    assert main(["--root", str(tmp_path), "--apply"]) == 0

    assert _absent(tmp_path, CACHE_FILES) == list(CACHE_FILES)
    assert _absent(tmp_path, KEPT_FILES) == []
    # build outputs are opt-in, so they survive a plain --apply
    assert _absent(tmp_path, BUILD_FILES) == []
    assert sorted(path.name for path in (tmp_path / "src").iterdir()) == ["app.py"]


def test_include_build_removes_build_outputs(tmp_path: Path) -> None:
    _build_tree(tmp_path)

    assert main(["--root", str(tmp_path), "--apply", "--include-build"]) == 0

    assert _absent(tmp_path, BUILD_FILES) == list(BUILD_FILES)
    # dist/ goes as a whole, so the environment nested inside it goes too
    kept_outside_build = [relative for relative in KEPT_FILES if not relative.startswith("dist/")]
    assert _absent(tmp_path, kept_outside_build) == []
    assert (tmp_path / "src" / "app.py").exists()


def test_extra_extends_the_cache_directory_names(tmp_path: Path) -> None:
    _build_tree(tmp_path)

    assert main(["--root", str(tmp_path), "--apply"]) == 0
    assert (tmp_path / ".codegraph" / "index.bin").exists()

    # a bare name does not match the dotted directory
    assert main(["--root", str(tmp_path), "--apply", "--extra", "codegraph"]) == 0
    assert (tmp_path / ".codegraph" / "index.bin").exists()

    assert main(["--root", str(tmp_path), "--apply", "--extra", ".codegraph"]) == 0
    assert not (tmp_path / ".codegraph").exists()


def test_json_output_reports_categories_and_items(tmp_path: Path, capsys) -> None:
    _build_tree(tmp_path)

    assert main(["--root", str(tmp_path), "--json", "--include-build"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert payload["total"] == len(CACHE_FILES) + 4
    categories = {entry["category"] for entry in payload["categories"]}
    assert {"python-cache", "tool-cache", "coverage", "build-output"} <= categories
    assert {item["path"] for item in payload["items"]} >= {"src/__pycache__", "build", "dist", "pkg.egg-info"}


def test_refuses_protected_and_unsafe_roots(tmp_path: Path, capsys) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    nested_venv = tmp_path / "dist" / "check-venv"
    nested_venv.mkdir(parents=True)
    (nested_venv / "pyvenv.cfg").write_text("home = python\n", encoding="utf-8")

    assert main(["--root", str(venv)]) == 2
    assert main(["--root", str(nested_venv)]) == 2
    assert main(["--root", str(tmp_path / "missing")]) == 2
    assert main(["--root", str(Path(tmp_path.anchor))]) == 2
    assert "refusing" in capsys.readouterr().err


def test_nested_virtualenv_is_never_scanned(tmp_path: Path, capsys) -> None:
    _build_tree(tmp_path)

    assert main(["--root", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "dist/check-venv" not in out
    assert "site-packages/pkg/__pycache__" not in out


def test_reports_nothing_to_do_for_empty_tree(tmp_path: Path, capsys) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")

    assert main(["--root", str(tmp_path)]) == 0
    assert "would remove 0 item(s)" in capsys.readouterr().out
