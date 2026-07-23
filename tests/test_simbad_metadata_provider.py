from __future__ import annotations

from sdb_identity.metadata import MetadataQueryContext
from sdb_identity.providers import Astrometry
from sdb_identity.simbad_metadata import AstroquerySimbadMetadata


def test_core_row_parsing_uses_current_simbad_fields():
    value = AstroquerySimbadMetadata.parse_core_row({
        "oid": 123,
        "main_id": "HD 1",
        "ra": 10.0,
        "dec": -20.0,
        "sp_type": "F5V",
        "sp_bibcode": "2000A&A...000....1A",
        "plx_value": 12.3,
        "plx_err": 0.2,
        "plx_bibcode": "2020A&A...000....2B",
        "pmra": 123.4,
        "pmdec": -56.7,
        "pm_bibcode": "2021A&A...000....6P",
        "rvz_radvel": 22.0,
        "rvz_err": 1.0,
        "rvz_bibcode": "2010A&A...000....3C",
        "otype": "Star",
        "otypes": "Star|PM*",
    })
    assert value["oid"] == 123
    assert value["spectral_type"] == "F5V"
    assert value["pm_ra_cosdec_masyr"] == 123.4
    assert value["pm_dec_masyr"] == -56.7
    assert value["proper_motion_bibcode"] == "2021A&A...000....6P"
    assert value["object_type_codes"] == ("Star", "PM*")


def test_relationship_parsing_calculates_direction_and_separation():
    values = AstroquerySimbadMetadata.parse_relationship_rows(
        10.0,
        -20.0,
        [{
            "direction": "child",
            "related_oid": 789,
            "related_main_id": "HD 1 B",
            "related_ra": 10.0001,
            "related_dec": -20.0,
            "related_otype": "Star",
            "related_otypes": "Star|PM*",
            "related_sp_type": "K0V",
            "related_sp_bibcode": "2001A&A...000....5E",
            "membership": None,
            "link_bibcode": None,
        }],
    )
    assert values[0].direction == "child"
    assert values[0].separation_arcsec > 0
    assert values[0].related_object_type == "Star"
    assert values[0].related_object_types == ("Star", "PM*")
    assert values[0].related_spectral_type == "K0V"
    assert values[0].related_spectral_type_bibcode == "2001A&A...000....5E"


def test_position_query_uses_supported_simbad_cone_adql():
    provider = object.__new__(AstroquerySimbadMetadata)
    queries = []
    provider._query_rows = lambda query: queries.append(query) or []
    context = MetadataQueryContext(
        target_id=1,
        sdbid="sdbid-v3-test",
        identifiers=(),
        astrometry=Astrometry(10.0, -20.0, 2000.0, source="input"),
    )
    assert provider._query_core_position(context) == []
    assert "CONTAINS" in queries[0]
    assert "DISTANCE" not in queries[0]


def test_query_many_groups_identifier_rows_and_uses_position_fallback():
    provider = object.__new__(AstroquerySimbadMetadata)
    calls = []

    def query_rows(query):
        calls.append(query)
        if "WHERE i.id IN" in query:
            return [
                {
                    "input_id": "HD 1",
                    "oid": 1,
                    "main_id": "HD 1",
                    "ra": 10.0,
                    "dec": -20.0,
                    "otype": "Star",
                    "otypes": "Star",
                },
                {
                    "input_id": "HD 2",
                    "oid": 2,
                    "main_id": "HD 2 A",
                    "ra": 20.0,
                    "dec": -20.0,
                    "otype": "Star",
                    "otypes": "Star",
                },
                {
                    "input_id": "HD 2",
                    "oid": 3,
                    "main_id": "HD 2 B",
                    "ra": 20.0001,
                    "dec": -20.0,
                    "otype": "Star",
                    "otypes": "Star",
                },
            ]
        if "SELECT id FROM ident" in query:
            return [{"id": "HD 1"}]
        if "SELECT otype, label, description" in query:
            return [{"otype": "Star", "label": "*", "description": "Star"}]
        if "FROM h_link" in query:
            return []
        if "CONTAINS" in query:
            return [{
                "oid": 4,
                "main_id": "Positional",
                "ra": 30.0,
                "dec": -20.0,
                "otype": "Star",
                "otypes": "Star",
            }]
        return []

    provider._query_rows = query_rows
    contexts = (
        MetadataQueryContext(
            target_id=1,
            sdbid="sdbid-1",
            identifiers=("HD 1",),
            astrometry=Astrometry(10.0, -20.0, 2000.0, source="sdb"),
        ),
        MetadataQueryContext(
            target_id=2,
            sdbid="sdbid-2",
            identifiers=("HD 2",),
            astrometry=Astrometry(20.0, -20.0, 2000.0, source="sdb"),
        ),
        MetadataQueryContext(
            target_id=3,
            sdbid="sdbid-3",
            identifiers=(),
            astrometry=Astrometry(30.0, -20.0, 2000.0, source="sdb"),
        ),
    )

    results = provider.query_many(contexts)

    assert results[1].status == "match"
    assert results[1].candidates[0].identifiers == ("HD 1",)
    assert results[2].status == "ambiguous"
    assert [candidate.main_id for candidate in results[2].candidates] == ["HD 2 A", "HD 2 B"]
    assert results[3].status == "match"
    assert results[3].candidates[0].main_id == "Positional"
    assert sum("WHERE i.id IN" in query for query in calls) == 1
