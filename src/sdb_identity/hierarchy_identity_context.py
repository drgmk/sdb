"""SIMBAD semantic-identity projection for hierarchy review context."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .hierarchy_semantics import (
    component_label_from_identifier,
    simbad_component_relevance,
)
from .models import (
    ExternalIdentifier,
    MetadataRun,
    SimbadMetadata,
    SimbadRelationship,
    Target,
)


def target_semantic_identity(
    session: Session, target: Target,
) -> dict[str, object]:
    run = session.scalar(
        select(MetadataRun)
        .where(
            MetadataRun.target_id == target.id,
            MetadataRun.provider == "simbad",
            MetadataRun.is_current.is_(True),
        )
        .order_by(MetadataRun.id.desc())
        .limit(1)
    )
    if run is None:
        return _unknown_identity("no_current_simbad_metadata", "missing")
    if run.status != "match":
        return _unknown_identity("simbad_metadata_status", run.status)
    metadata = session.scalar(
        select(SimbadMetadata).where(SimbadMetadata.run_id == run.id).limit(1),
    )
    identifiers = tuple(session.scalars(
        select(ExternalIdentifier.value)
        .where(
            ExternalIdentifier.target_id == target.id,
            ExternalIdentifier.source.in_(("simbad_metadata", "simbad")),
        )
        .order_by(ExternalIdentifier.id),
    ))
    relationships = tuple(session.scalars(
        select(SimbadRelationship)
        .where(SimbadRelationship.run_id == run.id)
        .order_by(
            SimbadRelationship.direction,
            SimbadRelationship.separation_arcsec,
            SimbadRelationship.related_main_id,
        ),
    ))
    parents = [
        _semantic_relationship(row)
        for row in relationships
        if row.direction == "parent"
    ]
    children = [
        _semantic_relationship(row)
        for row in relationships
        if row.direction == "child"
    ]
    structural_parents = _structural_relationships(parents)
    structural_children = _structural_relationships(children)
    if structural_parents and structural_children:
        kind = "subsystem"
    elif structural_parents:
        kind = "component"
    elif structural_children:
        kind = "system_or_parent"
    else:
        kind = "single_or_no_known_hierarchy"
    main_id = None if metadata is None else metadata.main_id
    return {
        "kind": kind,
        "evidence": "simbad_relationships",
        "confidence": "high" if parents or children else "medium",
        "status": run.status,
        "run_id": run.id,
        "main_id": main_id,
        "oid": None if metadata is None else metadata.oid,
        "primary_object_type": (
            None if metadata is None else metadata.primary_object_type
        ),
        "component_label_candidates": _component_label_candidates(
            main_id, identifiers,
        ),
        "relationship_relevance_counts": _semantic_relevance_counts(
            [*parents, *children],
        ),
        "parents": parents,
        "children": children,
    }


def target_semantic_identity_summary(
    value: dict[str, object],
) -> dict[str, object]:
    return {
        "kind": value["kind"],
        "evidence": value["evidence"],
        "confidence": value["confidence"],
        "status": value["status"],
        "main_id": value["main_id"],
        "component_label_candidates": value.get("component_label_candidates", []),
        "parents": len(value["parents"]),
        "children": len(value["children"]),
        "relationship_relevance_counts": value.get(
            "relationship_relevance_counts", {},
        ),
    }


def _unknown_identity(evidence: str, status: str) -> dict[str, object]:
    return {
        "kind": "unknown",
        "evidence": evidence,
        "confidence": "none",
        "status": status,
        "main_id": None,
        "parents": [],
        "children": [],
    }


def _component_label_candidates(
    main_id: str | None,
    identifiers: tuple[str, ...],
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    values = []
    if main_id:
        values.append(("main_id", main_id, "medium"))
    values.extend(("identifier", value, "low") for value in identifiers)
    for source, value, confidence in values:
        label = component_label_from_identifier(value)
        if label is None or (label, value) in seen:
            continue
        seen.add((label, value))
        candidates.append({
            "label": label,
            "source": source,
            "value": value,
            "confidence": confidence,
        })
    return candidates


def _structural_relationships(
    relationships: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        value for value in relationships
        if value.get("component_relevance") == "stellar_or_substellar_component"
    ]


def _semantic_relevance_counts(
    relationships: list[dict[str, object]],
) -> dict[str, int]:
    counts = {
        "stellar_or_substellar_component": 0,
        "planetary_or_disk": 0,
        "contextual_group": 0,
        "unknown": 0,
    }
    for value in relationships:
        relevance = str(value.get("component_relevance") or "unknown")
        counts[relevance] = counts.get(relevance, 0) + 1
    return counts


def _semantic_relationship(value: SimbadRelationship) -> dict[str, object]:
    object_types = json.loads(value.related_object_types_json or "[]")
    return {
        "related_oid": value.related_oid,
        "main_id": value.related_main_id,
        "ra_deg": value.related_ra_deg,
        "dec_deg": value.related_dec_deg,
        "object_type": value.related_object_type,
        "object_types": object_types,
        "component_relevance": simbad_component_relevance(
            value.related_object_type, object_types,
        ),
        "spectral_type": value.related_spectral_type,
        "spectral_type_bibcode": value.related_spectral_type_bibcode,
        "membership_percent": value.membership_percent,
        "bibcode": value.link_bibcode,
        "separation_arcsec": value.separation_arcsec,
    }
