"""Sampled article availability of an NZB on every news server nzbget knows, via vendored cyclops.

An article counts as missing only when every active server answers 430 to STAT, so a donor is
"dead" only if its articles are gone everywhere nzbget could fetch them from.
"""
import asyncio
import io
import math
import os
import random
import re
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
from cyclops.verify_nzb import ServerConfig, _Verifier  # noqa: E402  (vendored Appz4Fun/cyclops)


class Server(ServerConfig):
    def __repr__(self):  # never print credentials
        return "Server(%s:%d ssl=%s)" % (self.host, self.port, self.ssl)


def servers_from_nzbget_config(entries, connections=2, timeout=15.0):
    """Active ServerN.* entries of nzbget's `config` JSON-RPC result -> cyclops server list.

    `connections` caps connections per server so nzbget's own downloads keep their slots."""
    opts = {e["Name"]: str(e.get("Value", "")) for e in entries}
    numbers = sorted({int(m.group(1)) for m in (re.match(r"Server(\d+)\.Host$", k) for k in opts) if m})
    out = []
    for n in numbers:
        get = lambda key, default="": opts.get("Server%d.%s" % (n, key), default)  # noqa: E731
        if get("Active", "yes").lower() != "yes" or not get("Host"):
            continue
        user = get("Username") or None
        out.append(Server(name="server%d" % n, host=get("Host"), port=int(get("Port") or 119),
                          ssl=get("Encryption").lower() == "yes", username=user,
                          password=get("Password") if user else None,
                          max_connections=max(1, min(connections, int(get("Connections") or connections))),
                          timeout=timeout))
    return out


def sample(ids, percent, minimum=20, maximum=300, seed=0):
    """`percent` of the ids, at least `minimum`, at most `maximum` (deterministic)."""
    ids = sorted(ids)
    k = min(len(ids), max(minimum, min(maximum, math.ceil(len(ids) * percent / 100))))
    return random.Random(seed).sample(ids, k)


@dataclass
class Health:
    checked: int
    present: int
    missing: int
    error: int

    @property
    def alive(self):
        """Present share of the definite answers; None if every check was indeterminate."""
        known = self.present + self.missing
        return self.present / known if known else None


def availability(servers, message_ids, percent=2.0):
    """STAT a sample of `message_ids` (without <>) on all servers, falling back server by server."""
    verifier = _Verifier(list(servers), retries=1, progress_stream=io.StringIO())
    s = asyncio.run(verifier.run(sample(message_ids, percent)))
    return Health(s.total_checked, s.present, s.missing, s.error)
