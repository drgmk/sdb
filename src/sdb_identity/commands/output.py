"""Shared structured-output behavior for CLI domain commands."""

from __future__ import annotations

from contextlib import contextmanager
import json
import sys


def format_json(args, value, **kwargs) -> str:
    kwargs.setdefault("sort_keys", True)
    compact = getattr(args, "compact_json", False) or is_json_record_stream(
        args
    )
    if compact:
        kwargs.pop("indent", None)
        kwargs.setdefault("separators", (",", ":"))
    else:
        kwargs["indent"] = kwargs.get("indent", 2)
    return json.dumps(value, **kwargs)


@contextmanager
def provider_output_to_stderr():
    """Keep unstructured dependency output out of the CLI JSON channel."""
    output = sys.stdout
    try:
        sys.stdout = sys.stderr
        yield
    finally:
        sys.stdout = output


def is_json_record_stream(args) -> bool:
    command = getattr(args, "command", None)
    if command in {
        "attributes",
        "catalog-status",
        "dirty",
        "metadata-status",
        "review",
    }:
        return True
    if command == "alma":
        return getattr(args, "alma_command", None) in {"projects", "status"}
    if command == "cache":
        return getattr(args, "cache_command", None) in {"status", "tables"}
    if command == "dataset":
        return getattr(args, "dataset_command", None) in {
            "pending",
            "review",
            "status",
        }
    if command == "hierarchy":
        hierarchy_command = getattr(args, "hierarchy_command", None)
        if hierarchy_command in {"photometry-review", "review-queue"}:
            return getattr(args, "format", None) == "jsonl"
        return hierarchy_command in {
            "graph",
            "graph-diagnostics",
            "review",
            "sources",
        }
    if command == "reference":
        return getattr(args, "reference_command", None) in {
            "application-status",
            "audit-identifiers",
            "describe",
            "pending",
            "references",
            "relationships",
            "review",
        }
    if command == "photometry":
        return (
            getattr(args, "photometry_command", None) == "review"
            or (
                getattr(args, "photometry_command", None) == "review-queue"
                and getattr(args, "format", None) == "jsonl"
            )
        )
    if command == "sample":
        return getattr(args, "sample_command", None) in {"list", "members"}
    return False
