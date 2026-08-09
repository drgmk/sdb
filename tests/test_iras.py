from __future__ import annotations

from astropy.table import Table
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sdb_identity.catalogs.acquisition import CatalogAcquisitionService
from sdb_identity.catalogs.types import CatalogCandidate, MeasurementValue
from sdb_identity.cli import main
from sdb_identity.export import export_ipac
from sdb_identity.models.catalogs import (
    CatalogTargetAssociationAction,
    IrasBandSelection,
    IrasDetectionFamily,
    IrasFamilyTargetAssociationAction,
    IrasSourceFamily,
    NormalizedMeasurement,
)
from sdb_identity.review.actions import (
    review_catalog_target_association_decision,
    review_detection_decision,
)
from sdb_identity.review.app import create_review_app
from sdb_identity.photometry.review import (
    build_measurement_assignment_review,
    measurement_assignment_matrix,
)
from sdb_identity.service import AddRequest, IdentityService
from tests.test_catalog import FakeCatalog


def _candidate(source_id, provider, *, ra=10.0, quality="3", upper_limit=False):
    return CatalogCandidate(
        source_id=source_id,
        ra_deg=ra,
        dec_deg=0.0,
        epoch=1983.5,
        payload={
            "IRAS": source_id,
            "RA1950": "00 40 00",
            "DE1950": "+00 00 00",
            "Major": 10,
            "Minor": 4,
            "PosAng": 0,
        },
        measurements=(MeasurementValue(
            band="IRAS12",
            value=1.0 if provider == "iras_psc" else 2.0,
            error=0.1,
            unit="Jy",
            quality=quality,
            upper_limit=upper_limit,
            bibcode="test",
        ),),
    )


def _native_fsc_candidate(*psc_ids, ra=10.0):
    candidate = _candidate("F00000+0000", "iras_fsc", ra=ra)
    candidate.payload["_sdb_native_identifiers"] = [{
        "relationship": "iras_fsc_to_psc",
        "identifier": f"IRAS {psc_id}",
        "metadata": {"Dist": 9},
    } for psc_id in psc_ids]
    return candidate


def test_iras_family_selects_detection_quality_then_psc_and_export_retains_alternate(
    session_factory, tmp_path
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    psc = FakeCatalog(
        [_candidate("00000+0000", "iras_psc", quality="3")],
        name="iras_psc",
    )
    fsc = FakeCatalog(
        [_candidate("F00000+0000", "iras_fsc", quality="3")],
        name="iras_fsc",
    )
    CatalogAcquisitionService(session_factory, {"iras_psc": psc}).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(session_factory, {"iras_fsc": fsc}).refresh(target.sdbid, "iras_fsc")

    with session_factory() as session:
        family = session.scalar(select(IrasDetectionFamily))
        source_family = session.scalar(select(IrasSourceFamily))
        selection = session.scalar(select(IrasBandSelection))
        assert family.status == "associated"
        assert family.normalized_separation == pytest.approx(0.0, abs=1e-10)
        assert family.source_family_id == source_family.id
        assert selection.family_id == source_family.id
        assert selection.band == "IRAS12"
        assert selection.selected_measurement_id != selection.alternate_measurement_id

    output = export_ipac(session_factory, target.sdbid, tmp_path / "iras.txt")
    table = Table.read(output, format="ascii.ipac")
    by_phot = {float(row["Phot"]): row for row in table}
    assert int(by_phot[1.0]["exclude"]) == 0
    assert int(by_phot[2.0]["exclude"]) == 1
    assert "alternate PSC/FSC" in by_phot[2.0]["Note2"]


def test_iras_family_prefers_detection_over_higher_catalog_precedence(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    psc = FakeCatalog(
        [_candidate("00000+0000", "iras_psc", quality="1", upper_limit=True)],
        name="iras_psc",
    )
    fsc = FakeCatalog(
        [_candidate("F00000+0000", "iras_fsc", quality="2")],
        name="iras_fsc",
    )
    CatalogAcquisitionService(session_factory, {"iras_psc": psc}).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(session_factory, {"iras_fsc": fsc}).refresh(target.sdbid, "iras_fsc")
    with session_factory() as session:
        selection = session.scalar(select(IrasBandSelection))
        assert selection is not None
        selected = session.get(NormalizedMeasurement, selection.selected_measurement_id)
        assert selected.provider == "iras_fsc"
        assert selection.method == "quality_then_psc"


def test_iras_family_is_one_expandable_photometry_matrix_row(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    CatalogAcquisitionService(session_factory, {
        "iras_psc": FakeCatalog(
            [_candidate("00000+0000", "iras_psc")], name="iras_psc"
        )
    }).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(session_factory, {
        "iras_fsc": FakeCatalog(
            [_native_fsc_candidate("00000+0000")], name="iras_fsc"
        )
    }).refresh(target.sdbid, "iras_fsc")
    matrix = build_measurement_assignment_review(
        session_factory, target.sdbid
    ).matrix
    assert len(matrix["rows"]) == 1
    row = matrix["rows"][0]
    assert row["family_kind"] == "iras_psc_fsc"
    assert {value["provider"] for value in row["family_members"]} == {
        "iras_psc", "iras_fsc",
    }
    assert len(row["bands"]) == 1
    assert len(row["bands"][0]["catalog_entries"]) == 2
    assert sum(
        bool(value["selected"])
        for value in row["bands"][0]["catalog_entries"]
    ) == 1


def test_iras_matrix_status_uses_selected_band_entry_not_alternate_proposal():
    context = {
        "target": {"target_id": 1, "sdbid": "system"},
        "nearby_sdb_targets": [{"target_id": 1, "sdbid": "system"}],
        "system_memberships_by_target": {},
        "simbad_semantic_by_target": {},
        "target_lifecycle_by_target": {
            "system": {"role": "physical", "state": "active"},
        },
    }
    assignment = {
        "target_id": 1,
        "sdbid": "system",
        "role": "contributor",
    }
    common = {
        "origin_target_id": 1,
        "origin_sdbid": "system",
        "encounter_target_ids": [1],
        "encounter_sdbids": ["system"],
        "provenance": [],
        "band": "IRAS12",
        "error": 0.1,
        "systematic_error": 0.0,
        "unit": "Jy",
        "upper_limit": False,
        "resolution_major_arcsec": 30.0,
        "resolution_minor_arcsec": 15.0,
        "excluded": False,
        "predicted_scope": "system",
        "predicted_blend_state": "blended",
        "catalog_component": None,
        "proposal_confidence": "high",
        "proposal_reason": "selected family photometry",
        "current_assignments": [assignment],
        "candidate_targets": [],
    }
    proposals = [
        {
            **common,
            "measurement_id": 1,
            "detection_id": 10,
            "provider": "iras_psc",
            "source_id": "00000+0000",
            "source_display_name": "IRAS 00000+0000",
            "value": 1.0,
            "comparison_to_current": "agrees_with_current",
            "proposed_assignments": [assignment],
            "iras_family": {
                "key": "iras:10:11",
                "selected_for_band": True,
            },
        },
        {
            **common,
            "measurement_id": 2,
            "detection_id": 11,
            "provider": "iras_fsc",
            "source_id": "F00000+0000",
            "source_display_name": "IRAS F00000+0000",
            "value": 2.0,
            "comparison_to_current": "review_required",
            "proposed_assignments": [],
            "iras_family": {
                "key": "iras:10:11",
                "selected_for_band": False,
            },
        },
    ]

    matrix = measurement_assignment_matrix(context, proposals)

    row = matrix["rows"][0]
    assert row["comparison_to_current"] == "agrees_with_current"
    assert row["duplicate_proposal_conflict"] is False
    assert row["cells"][0]["status"] == "agrees"
    assert len(row["bands"][0]["catalog_entries"]) == 2


def test_iras_family_does_not_merge_ellipse_inconsistent_rows(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    offset = 1.5 / 3600.0
    psc_candidate = _candidate("00000+0000", "iras_psc", ra=10 - offset)
    fsc_candidate = _candidate("F00000+0000", "iras_fsc", ra=10 + offset)
    psc_candidate.payload["Major"] = psc_candidate.payload["Minor"] = 0.5
    fsc_candidate.payload["Major"] = fsc_candidate.payload["Minor"] = 0.5
    CatalogAcquisitionService(
        session_factory, {"iras_psc": FakeCatalog([psc_candidate], name="iras_psc")}
    ).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(
        session_factory, {"iras_fsc": FakeCatalog([fsc_candidate], name="iras_fsc")}
    ).refresh(target.sdbid, "iras_fsc")
    with session_factory() as session:
        family = session.scalar(select(IrasDetectionFamily))
        assert family.status == "review"
        assert session.scalar(select(IrasBandSelection)) is None


def test_iras_family_uses_native_association_outside_combined_ellipse(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    psc_candidate = _candidate("00000+0000", "iras_psc", ra=10 - 1.5 / 3600)
    fsc_candidate = _native_fsc_candidate("00000+0000", ra=10 + 1.5 / 3600)
    psc_candidate.payload["Major"] = psc_candidate.payload["Minor"] = 0.1
    fsc_candidate.payload["Major"] = fsc_candidate.payload["Minor"] = 0.1
    CatalogAcquisitionService(
        session_factory, {"iras_psc": FakeCatalog([psc_candidate], name="iras_psc")}
    ).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(
        session_factory, {"iras_fsc": FakeCatalog([fsc_candidate], name="iras_fsc")}
    ).refresh(target.sdbid, "iras_fsc")
    with session_factory() as session:
        family = session.scalar(select(IrasDetectionFamily))
        assert family.status == "associated"
        assert family.normalized_separation > 3
        assert "catalogue 42 explicitly links" in family.reason


def test_iras_family_keeps_one_to_many_native_association_for_review(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    CatalogAcquisitionService(session_factory, {
        "iras_psc": FakeCatalog(
            [_candidate("00000+0000", "iras_psc")], name="iras_psc"
        )
    }).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(session_factory, {
        "iras_fsc": FakeCatalog(
            [_native_fsc_candidate("00000+0000", "00000+0001")],
            name="iras_fsc",
        )
    }).refresh(target.sdbid, "iras_fsc")
    with session_factory() as session:
        family = session.scalar(select(IrasDetectionFamily))
        assert family.status == "review"
        assert "more than one published PSC association" in family.reason


def test_manual_iras_target_decision_applies_to_native_family(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    CatalogAcquisitionService(session_factory, {
        "iras_psc": FakeCatalog(
            [_candidate("00000+0000", "iras_psc")], name="iras_psc"
        )
    }).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(session_factory, {
        "iras_fsc": FakeCatalog(
            [_native_fsc_candidate("00000+0000")], name="iras_fsc"
        )
    }).refresh(target.sdbid, "iras_fsc")
    with session_factory() as session:
        psc = session.scalar(select(NormalizedMeasurement).where(
            NormalizedMeasurement.provider == "iras_psc"
        ))
        raw_id = psc.raw_row_id
        detection_id = psc.detection_id
    preview = review_catalog_target_association_decision(
        session_factory,
        target_reference=target.sdbid,
        detection_id=detection_id,
        reviewed_raw_row_id=raw_id,
        action="accept",
    )
    assert len(preview["detection"]["family_members"]) == 2
    assert "native IRAS PSC/FSC family" in preview["suggested_reason"]
    applied = review_catalog_target_association_decision(
        session_factory,
        target_reference=target.sdbid,
        detection_id=detection_id,
        reviewed_raw_row_id=raw_id,
        action="accept",
        apply=True,
        actor="test",
        expected_token=preview["state_token"],
    )
    assert applied["applied"]["actions_added"] == 2
    with session_factory() as session:
        actions = list(session.scalars(
            select(CatalogTargetAssociationAction).order_by(
                CatalogTargetAssociationAction.id
            )
        ))
        assert [value.method for value in actions] == [
            "iras_family_projection", "iras_family_projection",
        ]
        family_action = session.scalar(
            select(IrasFamilyTargetAssociationAction)
        )
        assert family_action is not None
        assert {value.family_action_id for value in actions} == {
            family_action.id
        }


def test_iras_photometry_attribution_and_drawer_are_family_level(session_factory):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    CatalogAcquisitionService(session_factory, {
        "iras_psc": FakeCatalog(
            [_candidate("00000+0000", "iras_psc")], name="iras_psc"
        )
    }).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(session_factory, {
        "iras_fsc": FakeCatalog(
            [_native_fsc_candidate("00000+0000")], name="iras_fsc"
        )
    }).refresh(target.sdbid, "iras_fsc")
    with session_factory() as session:
        measurements = list(session.scalars(
            select(NormalizedMeasurement).order_by(NormalizedMeasurement.id)
        ))
        psc_measurement = next(
            value for value in measurements if value.provider == "iras_psc"
        )

    preview = review_detection_decision(
        session_factory,
        detection_id=psc_measurement.detection_id,
        scope_target_reference=target.sdbid,
        contributor_references=[target.sdbid],
        include_composite_scope=False,
        measurement_ids=[psc_measurement.id],
    )
    assert {row["measurement_id"] for row in preview["measurements"]} == {
        value.id for value in measurements
    }
    assert preview["detection"]["family_id"] is not None
    assert "both IRAS family detections" in preview["notes"][0]

    page = TestClient(create_review_app(session_factory)).get(
        f"/target/{target.sdbid}"
    )
    assert page.status_code == 200
    assert page.text.count('<section class="detection"') == 1
    assert "IRAS family" in page.text
    assert "PSC selected" in page.text


def test_iras_family_review_cli_shows_sources_and_band_decisions(
    session_factory, db_path, capsys
):
    target = IdentityService(session_factory).add(AddRequest(ra_deg=10, dec_deg=0))
    CatalogAcquisitionService(session_factory, {
        "iras_psc": FakeCatalog(
            [_candidate("00000+0000", "iras_psc")], name="iras_psc"
        )
    }).refresh(target.sdbid, "iras_psc")
    CatalogAcquisitionService(session_factory, {
        "iras_fsc": FakeCatalog(
            [_candidate("F00000+0000", "iras_fsc")], name="iras_fsc"
        )
    }).refresh(target.sdbid, "iras_fsc")
    assert main([
        "--database", str(db_path), "review", "iras-families", "--all"
    ]) == 0
    value = __import__("json").loads(capsys.readouterr().out)
    assert value["status"] == "associated"
    assert value["psc_source_id"] == "00000+0000"
    assert value["fsc_source_id"] == "F00000+0000"
    assert value["band_selections"][0]["band"] == "IRAS12"
