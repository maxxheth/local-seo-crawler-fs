"""Opt-in HTTP Basic Auth from a netrc file, scoped to the hosts the file names."""

from __future__ import annotations

from pathlib import Path
from typing import Generator

import httpx


class HostScopedNetRCAuth(httpx.NetRCAuth):
    """NetRCAuth that only signs requests to a machine named in the netrc file.

    Plain NetRCAuth falls back to a `default` entry for any other host. This class
    ignores `default`, so credentials are never sent to a third-party host
    (a redirect, an external link check, a sitemap on another domain).
    """

    def __init__(self, file: str) -> None:
        super().__init__(file)
        self.machines = frozenset(h.lower() for h in self._netrc_info.hosts if h != "default")

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if request.url.host.lower() in self.machines:
            yield from super().auth_flow(request)
        else:
            yield request


def load_netrc_auth(path: str) -> httpx.Auth | None:
    """Return a host-scoped auth object for --netrc PATH, or None when PATH is empty.

    Raises ValueError (never echoing file contents) when the file is missing or unusable.
    """
    if not path:
        return None
    file = Path(path).expanduser()
    if not file.is_file():
        raise ValueError(f"--netrc: '{path}' is not an existing file")
    try:
        auth = HostScopedNetRCAuth(str(file))
    except Exception as exc:  # netrc.NetrcParseError, OSError: message may quote file lines, so drop it
        raise ValueError(f"--netrc: cannot parse '{path}' ({type(exc).__name__})") from exc
    if not auth.machines:
        raise ValueError(f"--netrc: '{path}' has no machine entries")
    return auth
