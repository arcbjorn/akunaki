"""Model SDK absence and dependency boundary."""

from __future__ import annotations

import ast
import importlib
import re
import sys
import tomllib
from pathlib import Path

import pytest

# Exact top-level / known package import names that must never appear in core.
FORBIDDEN_MODULES = (
    "openai",
    "anthropic",
    "google.generativeai",
    "google.genai",
    "xai",
    "xai_sdk",
    "openrouter",
    "litellm",
    "ollama",
    "llama_cpp",
    "transformers",
    "torch",
)

# Root package names that are always forbidden (whole tree).
# Ordinary ``google`` namespace imports are intentionally not listed here.
FORBIDDEN_ROOTS = {
    "openai",
    "anthropic",
    "xai",
    "xai_sdk",
    "openrouter",
    "litellm",
    "ollama",
    "llama_cpp",
    "transformers",
    "torch",
}

# Subtrees under an otherwise-allowed namespace that are still model SDKs.
FORBIDDEN_PREFIXES = (
    "google.generativeai",
    "google.genai",
)

# ``from google import <name>`` form for Gemini SDKs.
FORBIDDEN_GOOGLE_FROM_NAMES = frozenset({"generativeai", "genai"})

CORE_IMPORTS = (
    "akunaki",
    "akunaki.config",
    "akunaki.api",
    "akunaki.api.app",
    "akunaki.worker",
    "akunaki.adapters.db",
    "akunaki.domain",
    "akunaki.application",
    "akunaki.ports",
)


def _module_is_forbidden(module: str) -> bool:
    if module in FORBIDDEN_MODULES:
        return True
    root = module.split(".")[0]
    if root in FORBIDDEN_ROOTS:
        return True
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in FORBIDDEN_PREFIXES)


def test_forbidden_sdks_not_importable_as_installed_deps() -> None:
    """Core install must not ship model SDKs (ImportError or missing dist)."""
    import importlib.metadata as metadata

    dist_names = {d.metadata["Name"].lower().replace("-", "_") for d in metadata.distributions()}
    banned_dists = {
        "openai",
        "anthropic",
        "xai",
        "xai_sdk",
        "openrouter",
        "litellm",
        "ollama",
        "llama_cpp",
        "transformers",
        "torch",
        "google_generativeai",
        "google_genai",
    }
    present = banned_dists.intersection(dist_names)
    assert not present, f"model SDKs unexpectedly installed: {present}"


@pytest.mark.parametrize("module_name", FORBIDDEN_MODULES)
def test_forbidden_module_not_in_sys_modules_after_core_import(module_name: str) -> None:
    for name in CORE_IMPORTS:
        importlib.import_module(name)
    assert module_name not in sys.modules


def test_source_tree_has_no_model_sdk_imports() -> None:
    src_root = Path(__file__).resolve().parents[1] / "src" / "akunaki"
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _module_is_forbidden(alias.name):
                        offenders.append(f"{path}:{alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if _module_is_forbidden(node.module):
                    offenders.append(f"{path}:{node.module}")
                # Catch ``from google import generativeai`` / ``genai`` without forbidding
                # the rest of the google namespace.
                if node.module == "google":
                    for alias in node.names:
                        if alias.name in FORBIDDEN_GOOGLE_FROM_NAMES:
                            offenders.append(f"{path}:google.{alias.name}")
    assert not offenders, f"model SDK imports found: {offenders}"


def test_pyproject_has_no_model_sdk_dependencies() -> None:
    """Direct project dependencies (not import-linter deny-lists) exclude model SDKs."""
    data = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    deps: list[str] = list(data.get("project", {}).get("dependencies", []))
    for group in data.get("dependency-groups", {}).values():
        deps.extend(group)
    joined = "\n".join(deps).lower()
    for name in (
        "openai",
        "anthropic",
        "gemini",
        "google-generativeai",
        "google-genai",
        "openrouter",
        "litellm",
        "ollama",
        "transformers",
        "torch",
        "xai-sdk",
        "xai_sdk",
    ):
        # Match dependency names, not substring false positives.
        assert not re.search(rf"(?m)^\s*{re.escape(name)}(\s|[=<>!]|$)", joined), name
