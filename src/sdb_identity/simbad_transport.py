"""Shared SIMBAD TAP transport and astroquery client configuration."""

from __future__ import annotations

from astroquery.simbad import Simbad
from requests.adapters import HTTPAdapter

from .astroquery_config import configured_simbad_client
from .providers import ProviderError


def row_value(row, *names: str):
    """Decode one case-insensitive, possibly masked astroquery row value."""
    columns = {
        str(name).lower(): name
        for name in getattr(row, "colnames", ())
    }
    if isinstance(row, dict):
        columns.update({str(name).lower(): name for name in row})
    for name in names:
        key = columns.get(name.lower())
        if key is None:
            continue
        value = row[key]
        if getattr(value, "mask", False) or value is None:
            return None
        return value.item() if hasattr(value, "item") else value
    return None


def text_value(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return str(value).strip()


def float_value(value) -> float | None:
    return None if value is None else float(value)


def adql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def identifier_key(value: str) -> str:
    return "".join(str(value).casefold().split())


class _TimeoutHTTPAdapter(HTTPAdapter):
    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds
        super().__init__()

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout_seconds
        return super().send(request, **kwargs)


def set_http_timeout(client, timeout_seconds: float) -> None:
    adapter = _TimeoutHTTPAdapter(timeout_seconds)
    sessions = {client._session, client.tap._session}
    for session in sessions:
        session.mount("https://", adapter)
        session.mount("http://", adapter)


class SimbadTapTransport:
    """Own one configured astroquery client and translate TAP failures."""

    def __init__(
        self,
        timeout_seconds: float = 30.0,
        *,
        client=None,
    ):
        self.client = configured_simbad_client(client or Simbad())
        set_http_timeout(self.client, timeout_seconds)

    def query(self, adql: str, *, operation: str):
        try:
            return self.client.query_tap(adql)
        except Exception as error:
            raise ProviderError(
                f"SIMBAD {operation} failed: {error}", transient=True,
            ) from error
