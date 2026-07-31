from __future__ import annotations

import json

import pytest

from sdb_identity.catalogs import CatalogCandidate, CatalogService
from sdb_identity.cli import main
from sdb_identity.identifier_audit import audit_catalog_identifiers
from sdb_identity.models import ExternalIdentifier
from sdb_identity.identifiers import normalize_identifier
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog


def _target(session_factory, ra):
    return IdentityService(session_factory).add(AddRequest(ra_deg=ra, dec_deg=0))


def _refresh(session_factory, target, source_id, ra):
    candidate = CatalogCandidate(
        source_id,
        ra,
        0.0,
        1983.5,
        {"IRAS": source_id, "Major": 10, "Minor": 4, "PosAng": 0},
    )
    CatalogService(session_factory, {
        "iras_psc": FakeCatalog([candidate], name="iras_psc")
    }).refresh(target.sdbid, "iras_psc")


def _simbad_alias(session_factory, target_id, value):
    with session_factory() as session, session.begin():
        session.add(ExternalIdentifier(
            target_id=target_id,
            value=value,
            normalized_value=normalize_identifier(value),
            source="simbad",
        ))


def test_generic_catalog_identifier_audit_distinguishes_outcomes(
    session_factory,
):
    agree = _target(session_factory, 10)
    conflict = _target(session_factory, 20)
    catalog_only = _target(session_factory, 30)
    simbad_only = _target(session_factory, 40)
    _refresh(session_factory, agree, "00001+0001", 10)
    _refresh(session_factory, conflict, "00002+0002", 20)
    _refresh(session_factory, catalog_only, "00003+0003", 30)
    _simbad_alias(session_factory, agree.target_id, "IRAS 00001+0001")
    _simbad_alias(session_factory, conflict.target_id, "IRAS 99999+9999")
    _simbad_alias(session_factory, simbad_only.target_id, "IRAS 00004+0004")

    values = audit_catalog_identifiers(
        session_factory, "iras_psc", include_unmatched=True
    )
    statuses = {value.target_id: value.status for value in values}
    assert statuses == {
        agree.target_id: "agree",
        conflict.target_id: "conflict",
        catalog_only.target_id: "catalog_only",
        simbad_only.target_id: "simbad_only",
    }


def test_identifier_audit_cli_can_show_only_problems(
    session_factory, db_path, tmp_path, capsys
):
    target = _target(session_factory, 10)
    _refresh(session_factory, target, "00001+0001", 10)
    _simbad_alias(session_factory, target.target_id, "IRAS 99999+9999")
    assert main([
        "--database", str(db_path),
        "--reference-database", str(tmp_path / "reference.sqlite"),
        "reference", "audit-identifiers", "iras_psc", "--problems-only",
    ]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "conflict"
    assert value["catalog_identifiers"] == ["IRAS 00001+0001"]
    assert value["simbad_identifiers"] == ["IRAS 99999+9999"]


def test_catalog_without_policy_is_not_audited(session_factory):
    with pytest.raises(ValueError, match="no SIMBAD identifier-audit policy"):
        audit_catalog_identifiers(session_factory, "gaspar13")
