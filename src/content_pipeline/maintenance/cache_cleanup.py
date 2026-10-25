"""Find and remove regenerable cache artifacts under a project tree.

Scanning is read-only unless ``apply`` is true. Version control data,
virtualenvs, runtime data (output/, data/, logs/) and agent state
(.codegraph/) are never touched, and build outputs are opt-in because they
are slower to rebuild.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Candidate",
    "CleanupReport",
    "cleanup_cache",
    "find_candidates",
    "remove_candidate",
    "resolve_root",
]

# Bytecode and tool state: rebuilt automatically on the next run.
CACHE_DIR_NAMES = (
    ".hypothesis",
    ".ipynb_checkpoints",
    ".mypy_cache",
    ".pytest_cache",
    ".pytype",
    ".ruff_cache",
    "__pycache__",
)
PYTHON_CACHE_DIR_NAMES = ("__pycache__",)
CACHE_FILE_SUFFIXES = (".pyc", ".pyo")
COVERAGE_FILE_NAME = ".coverage"
COVERAGE_FILE_PREFIX = ".coverage."

BUILD_DIR_NAMES = ("build", "dist", "htmlcov")
BUILD_DIR_SUFFIXES = (".egg-info",)

# Never touched, even when they contain something cache-shaped.
PROTECTED_DIR_NAMES = (
    ".codegraph",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "data",
    "logs",
    "node_modules",
    "output",
    "venv",
)
# Marks the root of a virtual environment, whatever directory it lives in.
VENV_MARKER_NAME = "pyvenv.cfg"


@dataclass(frozen=True)
class Candidate:
    path: Path
    category: str
    is_dir: bool
    size: int


@dataclass(frozen=True)
class CleanupReport:
    root: Path
    applied: bool
    candidates: tuple[Candidate, ...] = ()
    failures: tuple[tuple[Candidate, str], ...] = ()

    @property
    def total(self) -> int:
        return len(self.candidates)

    @property
    def total_size(self) -> int:
        return sum(candidate.size for candidate in self.candidates)

    def categories(self) -> list[dict[str, object]]:
        buckets: dict[str, list[int]] = {}
        for candidate in self.candidates:
            bucket = buckets.setdefault(candidate.category, [0, 0])
            bucket[0] += 1
            bucket[1] += candidate.size
        return [{"category": name, "count": counts[0], "size": counts[1]} for name, counts in sorted(buckets.items())]

    def items(self) -> list[dict[str, object]]:
        """Report entries with paths relative to the scanned root, safe to hand to a client."""
        return [
            {
                "path": candidate.path.relative_to(self.root).as_posix(),
                "category": candidate.category,
                "is_dir": candidate.is_dir,
                "size": candidate.size,
            }
            for candidate in self.candidates
        ]

    def warnings(self) -> list[dict[str, str]]:
        return [
            {"path": candidate.path.relative_to(self.root).as_posix(), "error": error}
            for candidate, error in self.failures
        ]

    def to_payload(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "total": self.total,
            "total_size": self.total_size,
            "categories": self.categories(),
            "items": self.items(),
            "failures": self.warnings(),
        }


def _directory_size(path: Path) -> int:
    total = 0
    for current, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += (Path(current) / name).stat().st_size
            except OSError:
                continue
    return total


def _directory_category(name: str, *, include_build: bool, extra_dirs: frozenset[str]) -> str | None:
    if name in extra_dirs:
        return "extra"
    if name in CACHE_DIR_NAMES:
        return "python-cache" if name in PYTHON_CACHE_DIR_NAMES else "tool-cache"
    if include_build and (name in BUILD_DIR_NAMES or name.endswith(BUILD_DIR_SUFFIXES)):
        return "build-output"
    return None


def _file_category(name: str) -> str | None:
    if name.endswith(CACHE_FILE_SUFFIXES):
        return "python-cache"
    if name == COVERAGE_FILE_NAME or name.startswith(COVERAGE_FILE_PREFIX):
        return "coverage"
    return None


def _is_virtualenv(path: Path) -> bool:
    return (path / VENV_MARKER_NAME).is_file()


def find_candidates(
    root: Path, *, include_build: bool = False, extra_dirs: frozenset[str] = frozenset()
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for current, dirnames, filenames in os.walk(root):
        here = Path(current)
        dirnames.sort()
        descend: list[str] = []
        for name in dirnames:
            child = here / name
            if child.is_symlink():
                continue  # never follow links out of the tree
            if name in PROTECTED_DIR_NAMES and name not in extra_dirs:
                continue
            if name not in extra_dirs and _is_virtualenv(child):
                continue  # environments nested outside .venv/ (e.g. dist/check-venv/)
            category = _directory_category(name, include_build=include_build, extra_dirs=extra_dirs)
            if category is None:
                descend.append(name)
                continue
            candidates.append(Candidate(child, category, True, _directory_size(child)))
        dirnames[:] = descend
        for name in sorted(filenames):
            category = _file_category(name)
            if category is None:
                continue
            child = here / name
            if child.is_symlink():
                continue
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            candidates.append(Candidate(child, category, False, size))
    return candidates


def remove_candidate(candidate: Candidate) -> str | None:
    """Delete one candidate; return an error message on failure."""
    try:
        if candidate.is_dir and not candidate.path.is_symlink():
            shutil.rmtree(candidate.path)
        else:
            candidate.path.unlink()
    except OSError as exc:
        return str(exc)
    return None


def resolve_root(raw: str | Path) -> Path:
    """Resolve a cleanup root, refusing locations that must never be swept."""
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("cleanup root is not an existing directory")
    if root == Path(root.anchor):
        raise ValueError("refusing to clean a filesystem root")
    if root == Path.home():
        raise ValueError("refusing to clean the home directory")
    if root.name in PROTECTED_DIR_NAMES:
        raise ValueError("refusing to clean a protected directory")
    if _is_virtualenv(root):
        raise ValueError("refusing to clean a virtual environment")
    return root


def cleanup_cache(
    root: str | Path,
    *,
    apply: bool = False,
    include_build: bool = False,
    extra_dirs: frozenset[str] = frozenset(),
) -> CleanupReport:
    """Scan ``root`` and, when ``apply`` is true, delete every cache candidate."""
    resolved = resolve_root(root)
    candidates = find_candidates(resolved, include_build=include_build, extra_dirs=extra_dirs)
    failures: list[tuple[Candidate, str]] = []
    if apply:
        for candidate in candidates:
            error = remove_candidate(candidate)
            if error is not None:
                failures.append((candidate, error))
    return CleanupReport(root=resolved, applied=apply, candidates=tuple(candidates), failures=tuple(failures))
