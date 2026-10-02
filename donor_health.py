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
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
from cyclops.verify_nzb import ServerConfig, _Verifier  # noqa: E402  (vendored Appz4Fun/cyclops)


class Server(ServerConfig):
    def __repr__(self):  # never print credentials
        return "Server(%s:%d ssl=%s)" % (self.host, self.port, self.ssl)


def servers_from_nzbget_config(entries, connections=2, timeout=15.0):
    """Active ServerN.* entries of nzbget's `config` JSON-RPC result -> cyclops server list.

    Uses at most `connections`, and at most half of the server's own nzbget Connections, per server
    so nzbget's downloads keep their slots."""
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
                          max_connections=max(1, min(connections, int(get("Connections") or 2 * connections) // 2)),
                          timeout=timeout))
    return out


def sample(ids, percent, minimum=20, maximum=300, seed=0):
    """`percent` of the ids, at least `minimum`, at most `maximum` (deterministic)."""
    ids = sorted(ids)
    k = min(len(ids), max(minimum, min(maximum, math.ceil(len(ids) * percent / 100))))
    return random.Random(seed).sample(ids, k)


MIN_KNOWN = 5  # definite (present/missing) answers needed before judging an NZB


@dataclass
class Health:
    checked: int
    present: int
    missing: int
    error: int

    @property
    def alive(self):
        """Present share of the definite answers; None if too few answers (errors, budget) to judge."""
        known = self.present + self.missing
        return self.present / known if known >= MIN_KNOWN else None


def _stat_all(servers, ids, status, deadline):
    """STAT `ids` on all servers (one shared pool) until done or `deadline`; fills {id: final status}."""
    if not ids:
        return
    verifier = _Verifier(list(servers), retries=1, progress_stream=io.StringIO())

    async def run():
        task = asyncio.ensure_future(verifier.run(ids))
        done, _ = await asyncio.wait({task}, timeout=max(0.01, deadline - time.monotonic()))
        if not done:  # cyclops drains its queue on shutdown: drop the queue and cancel workers ourselves
            verifier.jobs.clear()
            for worker in verifier.workers:
                worker.cancel()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    status.update((mid, st.final_status) for mid, st in verifier.states.items() if st.final_status)


def _tally(ids, status):
    got = [status.get(i) for i in ids]
    return Health(len(ids), got.count("present"), got.count("missing"), got.count("error"))


def check_many(servers, groups, percent=2.0, probe=10, budget=120.0, full=True):
    """{key: message ids} -> {key: Health}. Each NZB first gets `probe` articles checked; one with none
    on any server is dead and skips its full `percent` sample (`full=False`: probe only).
    Unanswered ids after `budget` s: unknown."""
    deadline = time.monotonic() + budget
    samples = {k: sample(ids, percent) for k, ids in groups.items()}
    probes = {k: f[:probe] if len(f) >= probe else sample(groups[k], 0, probe, probe) for k, f in samples.items()}
    status = {}
    _stat_all(servers, list(dict.fromkeys(i for p in probes.values() for i in p)), status, deadline)
    if not full:
        return {k: _tally(p, status) for k, p in probes.items()}
    for k, p in probes.items():
        h = _tally(p, status)
        if h.present == 0 and h.missing >= MIN_KNOWN:
            samples[k] = p
    _stat_all(servers, [i for i in dict.fromkeys(i for f in samples.values() for i in f) if i not in status], status,
              deadline)
    return {k: _tally(f, status) for k, f in samples.items()}


def availability(servers, message_ids, percent=2.0):
    """Health of one NZB's articles (see check_many)."""
    return check_many(servers, {0: message_ids}, percent)[0]
