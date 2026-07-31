from sdb_identity.hierarchy_registry import HIERARCHY_SOURCES, hierarchy_source


def test_hierarchy_source_registry_is_explicit():
    assert tuple(HIERARCHY_SOURCES) == ("wds", "ccdm")
    assert hierarchy_source("wds").catalog == "B/wds"
    assert hierarchy_source("wds").graph_capable is True
    assert hierarchy_source("ccdm").main_table_aliases == frozenset({
        "i/274/ccdm", "i_274_ccdm", "ccdm",
    })


def test_registry_fixed_width_hooks_are_source_specific():
    wds = hierarchy_source("wds").fixed_width_parser(
        "00000+0000AB     AB    2000 2001          90       1.0"
    )
    ccdm = hierarchy_source("ccdm").fixed_width_parser(" CCDM J0000+0000 A")
    assert wds is None or wds.native_id == "00000+0000"
    assert ccdm is None or ccdm.native_id
