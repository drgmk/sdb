from __future__ import annotations

import pytest

from sdb_identity.review.sed import build_review_sed
from sdb_identity.review.sky_render import (
    _render_review_sed_html,
    render_review_sky_html,
)
from sdb_identity.review.sky_view import (
    ReviewSkyView,
    SkyPoint,
    SkySegment,
)


pytest.importorskip("sdf")


def test_review_sed_uses_sdf_and_current_component_assignments():
    matrix = {
        "columns": [
            {"sdbid": "a", "label": "A", "component_labels": ["A"]},
            {"sdbid": "b", "label": "B", "component_labels": ["B"]},
        ],
        "rows": [{
            "provider": "example",
            "bands": [
                {
                    "band": "2MJ",
                    "value": 7.0,
                    "error": 0.02,
                    "unit": "mag",
                    "measurement_ids": [1],
                    "catalog_entries": [],
                },
                {
                    "band": "WISE3P4",
                    "value": 6.0,
                    "error": 0.03,
                    "unit": "mag",
                    "measurement_ids": [2],
                    "catalog_entries": [],
                },
            ],
        }],
    }
    proposals = [
        {
            "measurement_id": 1,
            "provider": "2mass",
            "source_id": "one",
            "source_display_name": "2MASS one",
            "systematic_error": 0.01,
            "upper_limit": False,
            "excluded": False,
            "current_assignments": [{"sdbid": "a", "role": "contributor"}],
            "proposed_assignments": [{"sdbid": "a", "role": "contributor"}],
        },
        {
            "measurement_id": 2,
            "provider": "allwise",
            "source_id": "two",
            "source_display_name": "AllWISE two",
            "systematic_error": 0.0,
            "upper_limit": False,
            "excluded": False,
            "current_assignments": [],
            "proposed_assignments": [{"sdbid": "b", "role": "contributor"}],
        },
    ]

    sed = build_review_sed(matrix, proposals)

    assert sed["errors"] == []
    assert sed["symbols"]["A"] != sed["symbols"]["B"]
    assert sed["points"][0]["component"] == "A"
    assert sed["points"][0]["accepted"] is True
    assert sed["points"][0]["wavelength_micron"] == pytest.approx(1.23756936)
    assert sed["points"][0]["flux_jy"] == pytest.approx(2.45149463)
    assert sed["points"][1]["component"] == "B"
    assert sed["points"][1]["accepted"] is False


def test_review_sed_retains_colour_value_for_log_plot_filtering():
    matrix = {
        "columns": [{
            "sdbid": "target", "label": "star", "component_labels": [],
        }],
        "rows": [{
            "provider": "ubvmeans",
            "bands": [{
                "band": "UJ_BJ",
                "value": -0.1,
                "error": 0.02,
                "unit": "mag",
                "measurement_ids": [3],
                "catalog_entries": [],
            }],
        }],
    }
    proposals = [{
        "measurement_id": 3,
        "provider": "ubvmeans",
        "source_id": "three",
        "systematic_error": 0.0,
        "upper_limit": False,
        "excluded": False,
        "current_assignments": [{"sdbid": "target", "role": "contributor"}],
        "proposed_assignments": [],
    }]

    sed = build_review_sed(matrix, proposals)

    assert sed["points"][0]["wavelength_micron"] == pytest.approx(0.39925534)
    assert sed["points"][0]["flux_jy"] == pytest.approx(-0.1)


def test_review_sed_uses_composite_scope_instead_of_repeating_contributors():
    matrix = {
        "columns": [
            {"sdbid": "ab", "label": "AB", "component_labels": ["AB"]},
            {"sdbid": "a", "label": "A", "component_labels": ["A"]},
            {"sdbid": "b", "label": "B", "component_labels": ["B"]},
        ],
        "rows": [{
            "provider": "allwise",
            "bands": [{
                "band": "WISE22", "value": 1.0, "error": 0.1,
                "unit": "Jy", "measurement_ids": [4],
                "catalog_entries": [],
            }],
        }],
    }
    assignments = [
        {"sdbid": "ab", "role": "composite_scope"},
        {"sdbid": "a", "role": "contributor"},
        {"sdbid": "b", "role": "contributor"},
    ]
    proposals = [{
        "measurement_id": 4, "provider": "allwise", "source_id": "four",
        "systematic_error": 0.0, "upper_limit": False, "excluded": False,
        "current_assignments": assignments,
        "proposed_assignments": assignments,
    }]

    sed = build_review_sed(matrix, proposals)

    assert sed["points"][0]["component"] == "AB"


def test_review_sed_uses_current_assignment_despite_proposal_warning():
    matrix = {
        "columns": [
            {"sdbid": "a", "label": "A", "component_labels": ["A"]},
            {"sdbid": "b", "label": "B", "component_labels": ["B"]},
        ],
        "rows": [{
            "provider": "ubvmeans",
            "bands": [{
                "band": "BJ",
                "value": 8.0,
                "error": 0.02,
                "unit": "mag",
                "measurement_ids": [5],
                "catalog_entries": [],
            }],
        }],
    }
    proposals = [{
        "measurement_id": 5,
        "provider": "ubvmeans",
        "source_id": "five",
        "systematic_error": 0.0,
        "upper_limit": False,
        "excluded": False,
        "current_assignments": [{
            "sdbid": "a",
            "role": "contributor",
            "method": "catalog_association_default",
            "derived": True,
        }],
        "proposed_assignments": [],
        "comparison_to_current": "review_required",
    }]

    sed = build_review_sed(matrix, proposals)

    assert sed["points"][0]["component"] == "A"
    assert sed["points"][0]["ambiguous"] is False
    assert sed["points"][0]["accepted"] is True


def test_review_sed_retains_excluded_current_photometry_as_grey_point():
    matrix = {
        "columns": [{
            "sdbid": "target", "label": "star", "component_labels": [],
        }],
        "rows": [{
            "provider": "allwise",
            "bands": [{
                "band": "WISE22",
                "value": 0.5,
                "error": 0.05,
                "unit": "Jy",
                "measurement_ids": [6],
                "catalog_entries": [],
            }],
        }],
    }
    proposals = [{
        "measurement_id": 6,
        "provider": "allwise",
        "source_id": "excluded",
        "systematic_error": 0.0,
        "upper_limit": False,
        "excluded": True,
        "current_assignments": [{
            "sdbid": "target", "role": "contributor",
        }],
        "proposed_assignments": [],
    }]

    sed = build_review_sed(matrix, proposals)

    assert len(sed["points"]) == 1
    assert sed["points"][0]["component"] == "target"
    assert sed["points"][0]["excluded"] is True
    assert sed["points"][0]["accepted"] is False


def test_review_sed_adds_currently_ambiguous_catalog_candidates():
    matrix = {
        "columns": [{
            "sdbid": "target", "label": "star", "component_labels": [],
        }],
        "rows": [],
    }
    ambiguous = [{
        "measurement_id": 6,
        "provider": "ubvmeans",
        "source_id": "candidate",
        "band": "VJ",
        "value": 8.0,
        "error": 0.02,
        "systematic_error": 0.0,
        "unit": "mag",
        "upper_limit": False,
        "excluded": False,
    }]

    sed = build_review_sed(matrix, [], ambiguous)

    assert sed["errors"] == []
    assert sed["points"][0]["provider"] == "ubvmeans"
    assert sed["points"][0]["band"] == "VJ"
    assert sed["points"][0]["component"] == "ambiguous"
    assert sed["points"][0]["ambiguous"] is True
    assert sed["points"][0]["accepted"] is False
    assert sed["symbols"]["ambiguous"] == "x"


def test_sky_plot_uses_semantic_markers_and_only_line_legend_entries():
    points = (
        SkyPoint("target", "sdb", "target", "sdb", 10, -20, 0),
        SkyPoint("metadata", "simbad", "match", "sim", 10, -20, 0),
        SkyPoint("catalog", "2mass", "accepted", "cat", 10, -20, 0),
        SkyPoint("hierarchy", "wds", "accepted", "wds", 10, -20, 0),
    )
    view = ReviewSkyView(
        target_id=1,
        sdbid="sdb",
        center_ra_deg=10,
        center_dec_deg=-20,
        radius_arcsec=10,
        points=points,
        segments=(SkySegment(
            kind="hierarchy_component_link",
            provider="wds",
            status="accepted",
            source_id="wds",
            label="B",
            start_ra_deg=10,
            start_dec_deg=-20,
            end_ra_deg=10.0001,
            end_dec_deg=-20,
        ),),
    )

    html = render_review_sky_html(view)

    assert '"symbol":["cross"]' in html
    assert '"symbol":["x"]' in html
    assert '"symbol":["diamond"]' in html
    assert '"symbol":["circle"]' in html
    assert '"name":"hierarchy"' in html
    assert '"name":"sdb \\u002f target","showlegend":false' in html
    assert '"name":"simbad \\u002f match","showlegend":false' in html
    assert 'class="sed-frame"' in html


def test_sed_plot_uses_compact_header_hover_and_upper_limit_symbols():
    base = {
        "measurement_id": 1,
        "provider": "allwise",
        "source_id": "source",
        "band": "WISE22",
        "wavelength_micron": 22.0,
        "flux_jy": 0.5,
        "error_jy": 0.1,
        "component": "A",
        "accepted": True,
        "excluded": False,
        "measurement_value": 0.5,
        "measurement_error": 0.1,
        "measurement_unit": "Jy",
    }
    sed = {
        "points": [
            {**base, "upper_limit": False},
            {
                **base,
                "measurement_id": 2,
                "wavelength_micron": 60.0,
                "upper_limit": True,
            },
        ],
        "symbols": {"A": "circle"},
        "errors": [],
    }

    html = _render_review_sed_html(sed)

    assert '"text":"SED, Jy vs um"' in html
    assert '"symbol":["circle","triangle-down"]' in html
    assert '"array":[0.1,0.0]' in html
    assert '"customdata":[["WISE22"],["WISE22"]]' in html
    assert "%{customdata[0]} \\u00b7 A" in html
    assert "wavelength (\u03bcm)" not in html
    assert "flux density (Jy)" not in html
    assert '"x":0.38' in html
    assert '"y":1.18' in html
