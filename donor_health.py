"""Article availability of NZBs on every news server nzbget knows, checked in parallel.

Each sampled article is asked of all servers at once: STAT, and for BODY_PERCENT of the articles also BODY
with yEnc validation, so "the server says it has it" is backed by real data now and then. The first real
hit marks the article found and every server skips it from then on; an article is missing only after
every server said no. Each server walks the article list at its own pace, so a slow server never holds
up the fast ones (a request already in flight for an article that was found meanwhile is left to finish
and its answer ignored: cancelling it would mean dropping and re-opening the connection). NZBs are checked NZBS_TO_CHECK_CONCURRENTLY at a time, each with
NNTP_SERVER_CONNECTION_PER_NZB connections per server, all within MAX_CONNS_PER_NNTP_SERVER (and the
server's own nzbget Connections). The NNTP client is vendored Appz4Fun/cyclops.
"""
import asyncio
import math
import os
import queue
import random
import re
import sys
import threading
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
from cyclops.verify_nzb import (AsyncNntpConnection, MissingArticleError, ServerConfig,  # noqa: E402
                                validate_yenc_body)

MIN_KNOWN = 5               # answered articles needed before judging an NZB
SERVER_GIVE_UP = 3          # consecutive errors after which a server pauses ...
SERVER_RETRY_AFTER = 30.0   # ... for this many seconds before it is asked again


class Server(ServerConfig):
    def __repr__(self):  # never print credentials
        return "Server(%s:%d ssl=%s)" % (self.host, self.port, self.ssl)


def servers_from_nzbget_config(entries, max_conns=20, timeout=15.0):
    """Active ServerN.* entries of nzbget's `config` JSON-RPC result -> server list, each allowed at most
    `max_conns` connections and at most half of its own nzbget Connections (nzbget keeps the rest; several
    nzbget servers can share one provider account)."""
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
                          max_connections=max(1, min(max_conns, int(get("Connections") or 2 * max_conns) // 2)),
                          timeout=timeout))
    return out


@dataclass
class Limits:
    nzbs: int = 10     # NZBS_TO_CHECK_CONCURRENTLY
    per_nzb: int = 1   # NNTP_SERVER_CONNECTION_PER_NZB


@dataclass
class Health:
    checked: int
    present: int
    missing: int
    error: int
    body_checked: int = 0  # articles that also got a BODY check
    body_bad: int = 0      # ... for which no server delivered valid yEnc data

    @property
    def answered(self):
        return self.present + self.missing + self.error

    @property
    def alive(self):
        """Present share of all answered articles; None if too few answers (budget) to judge.

        Errors count as not present: a live article still gets a hit from some other server, while a
        server that errors (e.g. a transient 451) would otherwise hide every dead article."""
        return self.present / self.answered if self.answered >= MIN_KNOWN else None

    def __add__(self, other):
        return Health(*(a + b for a, b in zip(vars(self).values(), vars(other).values())))


def sample(ids, percent, minimum=20, maximum=300, seed=0):
    """`percent` of the ids, at least `minimum`, at most `maximum` (deterministic)."""
    ids = sorted(ids)
    k = min(len(ids), max(minimum, min(maximum, math.ceil(len(ids) * percent / 100))))
    return random.Random(seed).sample(ids, k)


def plan(ids, percent, body_percent=20, minimum=20, maximum=300, seed=0, max_body=5):
    """Sampled (message-id, also_body) pairs: about `body_percent`% of them, at most `max_body`, also get a
    BODY check (every server downloads a body article, so bodies are kept to a few per NZB)."""
    rnd, bodies, out = random.Random(seed + 1), 0, []
    for m in sample(ids, percent, minimum, maximum, seed):
        body = bodies < max_body and rnd.random() * 100 < body_percent
        bodies += body
        out.append((m, body))
    return out


def _drop(conn):
    """Close a connection whose request was cancelled mid-flight (its protocol state is unknown)."""
    writer, conn._writer, conn._reader = conn._writer, None, None
    if writer is not None:
        writer.close()


class _Pool:
    """Connections to one server, opened on demand and shared by every NZB check of a run."""

    def __init__(self, server):
        self.server, self.idle, self.errors, self.down_until = server, [], 0, 0.0
        self.slots = asyncio.Semaphore(server.max_connections)

    async def ask(self, mid, body):
        """This server's answer for one article: 'present', 'missing', 'bodybad' or 'error'."""
        wait = self.down_until - asyncio.get_running_loop().time()
        if wait > 0:  # a server that kept failing gets a pause, then another try (found articles don't wait)
            await asyncio.sleep(wait)
        async with self.slots:
            conn = self.idle.pop() if self.idle else AsyncNntpConnection(self.server)
            try:
                answer = await self._ask(conn, mid, body)
            except asyncio.CancelledError:
                _drop(conn)
                raise
            except Exception:
                self.errors += 1
                if self.errors >= SERVER_GIVE_UP:
                    self.down_until = asyncio.get_running_loop().time() + SERVER_RETRY_AFTER
                await conn.close()
                answer = "error"
            else:
                self.errors = 0
            if self.errors >= SERVER_GIVE_UP:
                self.errors = 0
            self.idle.append(conn)
            return answer

    @staticmethod
    async def _ask(conn, mid, body):
        if await conn.stat(mid) != 223:
            return "missing"
        if not body:
            return "present"
        try:
            return "present" if validate_yenc_body(await conn.body(mid)).ok else "bodybad"
        except MissingArticleError:
            return "bodybad"

    async def close(self):
        for conn in self.idle:
            await conn.close()


async def _check_nzb(pools, items, per_nzb, deadline):
    """Check one NZB's sampled articles on every server at once -> Health."""
    n, loop = len(items), asyncio.get_running_loop()
    final, votes, soft = [None] * n, [dict() for _ in range(n)], [False] * n
    done, left = asyncio.Event(), [n]

    def settle(i, verdict):
        if final[i] is None:
            final[i] = verdict
            left[0] -= 1
            if not left[0]:
                done.set()

    async def walk(p, pool, cursor):  # one server's pass over the articles, skipping ones already found
        while cursor[0] < n:
            i = cursor[0]
            cursor[0] += 1
            if final[i] is not None:
                continue
            mid, body = items[i]
            answer = await pool.ask(mid, body)
            votes[i][p] = answer
            soft[i] = soft[i] or answer == "bodybad"
            if answer == "present":
                settle(i, "present")
            elif len(votes[i]) == len(pools):
                settle(i, "error" if "error" in votes[i].values() else "missing")

    walkers = [asyncio.ensure_future(walk(p, pool, cursor))
               for p, pool in enumerate(pools) for cursor in [[0]] for _ in range(per_nzb)]
    if n:
        try:
            await asyncio.wait_for(done.wait(), max(0.01, deadline - loop.time()))
        except asyncio.TimeoutError:
            pass
    for w in walkers:  # requests still in flight are for articles already settled (or the budget ran out)
        w.cancel()
    await asyncio.gather(*walkers, return_exceptions=True)
    return Health(n, final.count("present"), final.count("missing"), final.count("error"),
                  sum(body for _, body in items), sum(soft[i] and final[i] != "present" for i in range(n)))


async def _run(servers, groups, percent, probe, budget, full, limits, body_percent, max_body, emit):
    loop = asyncio.get_running_loop()
    pools, gate = [_Pool(s) for s in servers], asyncio.Semaphore(limits.nzbs)

    async def one(key, ids):
        items = plan(ids, percent, body_percent, max_body=max_body)
        first = items[:probe] if len(items) >= probe else plan(ids, 0, body_percent, probe, probe, max_body=max_body)
        async with gate:
            deadline = loop.time() + budget  # each NZB's own budget, counted from when its check starts
            h = await _check_nzb(pools, first, limits.per_nzb, deadline)
            if full and not (h.present == 0 and h.answered >= MIN_KNOWN):  # dead after the probe: done
                probed = {m for m, _ in first}
                h += await _check_nzb(pools, [it for it in items if it[0] not in probed], limits.per_nzb,
                                      deadline)
        emit(key, h)

    try:
        await asyncio.gather(*(one(k, ids) for k, ids in groups.items()))
    finally:
        for pool in pools:
            await pool.close()


def check_iter(servers, groups, percent=2.0, probe=10, budget=120.0, full=True, limits=None, body_percent=20,
               max_body=5):
    """{key: message ids} -> yields (key, Health) as each NZB finishes. Every NZB first gets `probe`
    articles; one with none found anywhere is dead and skips its full `percent` sample (`full=False`:
    probe only). Articles of an NZB still unanswered `budget` s after its check started are unknown."""
    results, end = queue.Queue(), object()

    def run():
        try:
            asyncio.run(_run(servers, groups, percent, probe, budget, full, limits or Limits(), body_percent,
                             max_body, lambda k, h: results.put((k, h))))
        finally:
            results.put(end)

    threading.Thread(target=run, daemon=True).start()
    while True:
        item = results.get()
        if item is end:
            return
        yield item


def check_many(servers, groups, percent=2.0, probe=10, budget=120.0, full=True, limits=None, body_percent=20,
               max_body=5):
    """{key: message ids} -> {key: Health} (see check_iter)."""
    return dict(check_iter(servers, groups, percent, probe, budget, full, limits, body_percent, max_body))


def availability(servers, message_ids, percent=2.0):
    """Health of one NZB's articles (see check_iter)."""
    return check_many(servers, {0: message_ids}, percent)[0]
