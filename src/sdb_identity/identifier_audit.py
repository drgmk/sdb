from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .catalog_results import effective_catalog_results
from .models import ExternalIdentifier, Target
from .reference_definitions import SNAPSHOT_CATALOGS
from .identifiers import normalize_identifier
from .vocabulary import ProviderRunStatus


@dataclass(frozen=True)
class IdentifierAuditResult:
    target_id: int
    sdbid: str
    provider: str
    status: str
    selected_source_id: str | None
    catalog_identifiers: tuple[str, ...]
    simbad_identifiers: tuple[str, ...]
    message: str


def audit_catalog_identifiers(
    session_factory: sessionmaker[Session],
    provider: str,
    *,
    include_unmatched: bool = False,
) -> list[IdentifierAuditResult]:
    definition = SNAPSHOT_CATALOGS[provider]
    policy = definition.identifier_audit
    if policy is None:
        raise ValueError(
            f"{provider} has no SIMBAD identifier-audit policy"
        )
    with session_factory() as session:
        targets = list(session.scalars(select(Target).order_by(Target.id)))
        aliases: dict[int, list[str]] = {}
        for target_id, value in session.execute(select(
            ExternalIdentifier.target_id,
            ExternalIdentifier.value,
        ).where(ExternalIdentifier.source == "simbad")):
            aliases.setdefault(target_id, []).append(value)
        runs = effective_catalog_results(
            session,
            (target.id for target in targets),
            providers=(provider,),
        )
        results = []
        for target in targets:
            run = runs.get((target.id, provider))
            if run is not None and run.status != ProviderRunStatus.MATCH:
                run = None
            simbad_values = tuple(sorted({
                value for value in aliases.get(target.id, [])
                if policy.relevant(value)
            }))
            if run is None:
                if include_unmatched and simbad_values:
                    results.append(IdentifierAuditResult(
                        target.id,
                        target.sdbid,
                        provider,
                        "simbad_only",
                        None,
                        (),
                        simbad_values,
                        "SIMBAD has a relevant identifier but there is no current positional match",
                    ))
                continue
            raw = run.selected_raw_row
            payload = {} if raw is None else json.loads(raw.payload_json)
            catalog_values = tuple(sorted({
                value for value in definition.identifiers(payload)
                if policy.relevant(value)
            }))
            catalog_normalized = {
                normalize_identifier(value) for value in catalog_values
            }
            simbad_normalized = {
                normalize_identifier(value) for value in simbad_values
            }
            if not simbad_values:
                status = "catalog_only"
                message = "position-selected row has an identifier that SIMBAD did not supply"
            elif catalog_normalized & simbad_normalized:
                status = "agree"
                message = "position-selected catalog and SIMBAD identifiers agree"
            else:
                status = "conflict"
                message = "position-selected catalog and SIMBAD identifiers disagree"
            results.append(IdentifierAuditResult(
                target.id,
                target.sdbid,
                provider,
                status,
                run.selected_source_id,
                catalog_values,
                simbad_values,
                message,
            ))
        return results
