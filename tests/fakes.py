from __future__ import annotations

from sdb_identity.providers import Astrometry, Candidate, NameResolution, ProviderError


class FakeSimbad:
    name = "simbad"

    def __init__(self, resolutions=None, error: str | None = None):
        self.resolutions = resolutions or {}
        self.error = error

    def resolve_name(self, name: str):
        if self.error:
            raise ProviderError(self.error, transient=True)
        return self.resolutions.get(name)


class FakeGaia:
    name = "gaia_dr3"

    def __init__(self, candidates=None, error: str | None = None):
        self.candidates = candidates or []
        self.error = error

    def search(self, astrometry: Astrometry):
        if self.error:
            raise ProviderError(self.error, transient=True)
        return list(self.candidates)


def astrometry(ra, dec, *, epoch=2000.0, pmra=None, pmdec=None, source="input", rv=None):
    return Astrometry(
        ra_deg=ra,
        dec_deg=dec,
        epoch=epoch,
        pm_ra_cosdec_masyr=pmra,
        pm_dec_masyr=pmdec,
        source=source,
        radial_velocity_kms=rv,
    )


def simbad_result(name, value, identifiers=()):
    return NameResolution(main_id=name, astrometry=value, identifiers=tuple(identifiers))


def gaia_candidate(source_id, value, identifiers=()):
    return Candidate(source_id=str(source_id), astrometry=value, identifiers=tuple(identifiers))

