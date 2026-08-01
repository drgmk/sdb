"""ALMA ObsCore transport with bounded TAP jobs and mirror failover."""

from __future__ import annotations

from typing import Protocol

from ..providers import ProviderError


ALMA_COLUMNS = (
    "obs_publisher_did",
    "obs_id",
    "group_ous_uid",
    "member_ous_uid",
    "asdm_uid",
    "proposal_id",
    "target_name",
    "s_ra",
    "s_dec",
    "s_fov",
    "s_region",
    "t_min",
    "t_max",
    "obs_release_date",
    "data_rights",
    "band_list",
    "lastModified",
)


class AlmaArchiveProvider(Protocol):
    archive_url: str

    def bootstrap_chunk(self, start_mjd: float, end_mjd: float): ...

    def bootstrap_undated(self): ...

    def modified_since(self, watermark: str): ...


class AstroqueryAlmaArchive:
    """Thin ALMA ObsCore client; TAP returns public and proprietary metadata."""

    mirrors = (
        "https://almascience.org",
        "https://almascience.eso.org",
        "https://almascience.nrao.edu",
        "https://almascience.nao.ac.jp",
    )

    def __init__(
        self,
        archive_url: str | None = None,
        timeout_seconds: float = 300,
    ):
        from astroquery.alma import Alma

        self.client = Alma()
        if archive_url:
            self.client.archive_url = archive_url
        self.archive_url = str(self.client.archive_url)
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _projection():
        return ", ".join(ALMA_COLUMNS)

    def bootstrap_chunk(self, start_mjd: float, end_mjd: float):
        query = (
            f"SELECT {self._projection()} FROM ivoa.obscore "
            f"WHERE t_min >= {start_mjd:.8f} AND t_min < {end_mjd:.8f}"
        )
        return self._query(query)

    def bootstrap_undated(self):
        query = (
            f"SELECT {self._projection()} FROM ivoa.obscore WHERE t_min IS NULL"
        )
        return self._query(query)

    def modified_since(self, watermark: str):
        safe = watermark.replace("'", "''")
        query = (
            f"SELECT {self._projection()} FROM ivoa.obscore "
            f"WHERE lastModified > '{safe}'"
        )
        return self._query(query)

    def _query(self, query):
        from astroquery.alma import Alma

        urls = tuple(dict.fromkeys((self.archive_url, *self.mirrors)))
        failures = []
        for url in urls:
            client = self.client
            if str(client.archive_url) != url:
                client = Alma()
                client.archive_url = url
            try:
                job = client.tap.submit_job(query)
                try:
                    job.run()
                    job.wait(
                        phases={"COMPLETED", "ERROR", "ABORTED"},
                        timeout=self.timeout_seconds,
                    )
                    if job.phase != "COMPLETED":
                        raise RuntimeError(
                            f"ALMA TAP job ended in phase {job.phase}"
                        )
                    result = job.fetch_result().to_table()
                except BaseException:
                    try:
                        job.abort()
                    except Exception:
                        pass
                    raise
                finally:
                    try:
                        job.delete()
                    except Exception:
                        pass
                self.client = client
                self.archive_url = url
                return result
            except Exception as error:
                failures.append(f"{url}: {error}")
        raise ProviderError(
            "ALMA TAP query failed on every mirror: " + " | ".join(failures),
            transient=True,
        )
