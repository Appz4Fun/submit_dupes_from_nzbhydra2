# nzbget-dupe-proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline, one-shot) with
> superpowers:test-driven-development per task, superpowers:systematic-debugging on any failure,
> superpowers:verification-before-completion before claiming anything works. Steps use `- [ ]`.

**Goal:** When NZBHydra2 sends one NZB to nzbget, also send every other posting of the same release
as a linked duplicate (same DupeKey, lower DupeScore) so nzbget's DupeArticleFallback has donors.

**Architecture:** One stdlib-only Python 3 file, `nzbget_dupe_proxy.py`, runs a `ThreadingHTTPServer`
on :6790 that forwards every request verbatim to nzbget. JSON-RPC `append` is rewritten (DupeKey,
score 100), forwarded, and answered immediately; a background thread then searches Hydra's newznab
API, fetches and fingerprints candidate NZBs, and appends verified donors (score 90, 89, ...).
A small JSON state file gives idempotency and groups "send selected" appends.

**Tech Stack:** Python 3.12 (server) / 3.14 (local) stdlib (`http.server`, `urllib`, `xml.etree`,
`concurrent.futures`, `json`, `logging`); pytest for tests only; systemd.

**Spec:** the user's request in the session (summarised in "Requirements" below).

## Global Constraints

- Runtime: Python 3 stdlib only. Tests: pytest. Target < 400 lines for `nzbget_dupe_proxy.py`.
- Listen `0.0.0.0:6790` (`LISTEN_PORT`); upstream `NZBGET_URL` default `http://127.0.0.1:6789`.
- Config from env (`/etc/nzbget-dupe-proxy.env`, mode 600 root): `LISTEN_PORT, NZBGET_URL, HYDRA_URL,
  HYDRA_APIKEY, MAX_DONORS (8), SIZE_TOLERANCE (0.02), STATE_DIR (/var/lib/nzbget-dupe-proxy),
  ENABLED (true; false = pure pass-through), DRY_RUN (0)`.
- Primary: `DupeKey` = Hydra's if non-empty else `"dupes:" + normalized title`; `DupeScore 100`; `DupeMode "SCORE"`.
- Donors: same DupeKey, `DupeScore 90, 89, ...`, `"SCORE"`, `AddPaused false`, same Category; max `MAX_DONORS`.
- Candidate filter: Hydra size within `SIZE_TOLERANCE` of primary bytes AND (token overlap >= 50% OR same normalized title).
- Verify: (total bytes within 1% AND file count within ±10%) OR >= 50% filenames shared. Reject message-ID sets
  identical to the primary, an already-added donor, or anything already sent for the key.
- Fetch concurrency 4, HTTP timeout 30 s, discovery deadline ~60 s after the primary append.
- Grouping window 10 minutes. Never send the same posting (message-ID fingerprint) twice.
- Donor problems never reach Hydra. One INFO summary line per append. Mask apikey/passwords in logs.
- Do not restart/reconfigure nzbget or Hydra; do not edit `nzbhydra.yml`; ask before any real production append.
- Every commit authored and signed by xbmc4lyfe (AGENTS.md rule #1).

---

## Findings: how NZBHydra2 v9.0.4 talks to nzbget

Source: `core/src/main/java/org/nzbhydra/downloading/downloaders/nzbget/NzbGet.java` at tag `v9.0.4`.

| Item | Finding |
|---|---|
| Transport | `jsonrpc4j` `JsonRpcHttpClient`, **JSON-RPC only**. URL = configured URL + path `jsonrpc` (`UriComponentsBuilder.path("jsonrpc")`). No XML-RPC, so XML-RPC is passed through untouched and not intercepted. |
| Auth | Header `Authorization: Basic base64(user:pass)` when username+password are set; also `User-Agent: NZBHydra2`. |
| Test connection | `writelog("INFO", "NZBHydra 2 connected to test connection")` → expects `true`. |
| Other calls | `status []`, `config null` (categories from `CategoryN.Name`), `listgroups [0]`, `history [true]` (reads `Kind` NZB/DUP). |
| append | `append(nzbName, contentOrLink, category, 0, false, addPaused, "", 0, "SCORE", [])`. `nzbName` = result title + `.nzb` if missing. Content is base64 NZB (UPLOAD mode) or a URL (link mode). **DupeKey is always `""`, DupeScore `0`, DupeMode `"SCORE"`, PPParameters empty.** Hydra throws if result `<= 0`. |

Live confirmation (Task 1, step 6, done 2026-10-02): ran the proxy pass-through on the server on temp
port 6791 and called Hydra's own Test-connection endpoint (`POST /internalapi/downloader/checkConnection`,
needs the `XSRF-TOKEN` cookie echoed as `X-XSRF-TOKEN`; it builds a throwaway downloader and persists
nothing) with URL `http://192.168.1.93:6791`. Hydra answered `{"successful": true}`; the proxy captured:

```
POST /jsonrpc  Authorization: Basic <masked>  User-Agent: NZBHydra2  Content-Type: application/json-rpc
{"id":"406397322","jsonrpc":"2.0","method":"writelog","params":["INFO","NZBHydra 2 connected to test connection"]}
-> 200 {"version" : "1.1", "id" : "406397322", "result" : true}
```

Note the JSON-RPC `id` is a **string**; synthesized replies must echo it unchanged.

### Findings: what nzbget (PR 850 build) can use as a donor

From `~/git/nzbget` branch `dupe-article-fallback`:
- Donor = any queued `nzbNzb` item or **history `hkNzb` item of any status** (incl. dupe backups) with the
  **same DupeKey** (case-insensitive). Score only orders donors. Donor `.nzb` must stay on disk
  (server has `NzbCleanupDisk=no`, `KeepHistory=365`, `HealthCheck=none`: good).
- Byte-identical reposts (different message-IDs, same segmentation) work at article level; differently
  packaged store-mode archives / bare media with identical inner size work via stream/cross-pack repair;
  compressed donors only via DupeStreamDecompress and only if complete.
- Appending a lower-score item with `DupeMode SCORE` while the primary is queued sends it straight to
  history as a dupe backup (`dsDupe`) — the desired donor state.

### Findings: real Hydra data (Lucifer S02E14 1080p, 100 results)

Indexer sizes for one release (`...REMUX-FraMeST`) were 4796103342 / 4800889847 / 4801118079 /
4836897567 / 4991241081 — five different postings, packaged as 10, 8, 20 (7z, passworded), 96
(obfuscated) and 3 files. The same posting appears on several indexers with the same message-IDs
(e.g. Zurg and Drunken Slug: identical fingerprint) — the message-ID rule removes those. Some indexers
return 403 on NZB fetch (grab limits): counted as `rejected: fetch`. Each candidate fetch costs an indexer
grab, so fetches are capped at `MAX_FETCH = 3 * MAX_DONORS` and ordered so one of each distinct size is
fetched first.

---

## Design change (2026-10-02, after live dry run + user direction)

The live dry run showed that the spec's byte/count verify rule rejects real reposts. For example,
`...REMUX-FraMeSToR` was posted as 8 named files (4,800,889,847 bytes) and again as 20 obfuscated 7z
volumes (4,801,118,079 bytes). The rule also misses postings that carry extra par2 (+4%). Indexer
sizes are unreliable too: nzb.life and nzbplanet list 4.8 GB but serve an 8.98 GB posting.

The user's direction:
- "exact file name ±20% should be a potential dupe";
- "potential dupes can be off by a lot of GBs";
- "parse with PTT to find the same group, audio, res, HDR...".

New rules:
- **Candidate:** `same_release(primary_title, hydra_title)` using vendored PTT. Title, S/E and
  repack/proper must be equal, and the group must be equal. COMPAT fields must not conflict
  (token-set subset). Size is not a filter; `SIZE_TOLERANCE` defaults to 0 (off).
- **NZB check:** reject the same message-ID set. Reject when the largest inner file has a readable
  name and is not `same_release` with the primary title.
- **Ranking:** distance from the primary's bytes, then grabs, then date.
- **Grouping** ("send selected"): by `same_release` of the stored primary title.
- **Byte-level identity** is left to nzbget's DupeArticleFallback probe (≥16 KiB compare) before
  any donor data is used.

The `verify()` byte/count rule is removed.

## Design change (2026-10-03, after a live audit)

The user asked for many donors, ranked most whole first, and for more STAT and BODY checking. A live audit
on the server found the following.

| Finding | Evidence | Change |
|---|---|---|
| A missing article took 10+ s to settle. | super.newsgroupdirect.com answers `451`, not `430`. cyclops treated that as a protocol error and closed the connection, and the server paused for 30 s after three errors. A dead NZB's 10 probe articles took 100–260 s. | A reply line is an answer on a healthy connection. `451` counts as an error vote, and the connection stays open. A server in its error pause abstains. |
| Alive shares came out too high. | Present articles settle at the first hit, while missing ones wait for every server. When the budget ran out, the unanswered articles were mostly missing ones. An NZB that is 12% alive read as 57%. | When the budget runs out, an unsettled article counts as missing if at least half of the servers said so and none had it. |
| `HEALTH_PERCENT` above about 2.6% had no effect. | Samples were capped at 300 articles, and a 2160p episode has about 11,000. | The defaults are now 5% of the articles, from 50 to 1,000 (`HEALTH_MIN_ARTICLES`, `HEALTH_MAX_ARTICLES`), and 20 BODY checks per NZB. Each body is downloaded by one server at a time. |
| The check had 1 connection per server. | Since 2026-10-03, nzbget sets `Connections=2` for every server, and the check takes at most half. | `STAT` commands are pipelined, from 4 up to 16 per round trip, adapting to each server. Pipelining is about 10× faster on servers limited by network latency. |
| Indexer grab limits ran out, and donors were lost to `403`. | Sugar S01E07 had 67 candidates but only 17 postings, and the proxy fetched every listing: 36 fetches were `same-posting` and 15 failed with `403`. | Listings with the same size and posted within 2 minutes of each other are one posting, fetched once, with fallback to its next listing. An indexer that still answers `403` or `429` after the retry is skipped for 30 minutes, with one fetch per indexer at a time. |
| Donors scored about 98% alive downloaded nothing. | nzbget IDs 1488 and 2173 scored 89 under the code from before health checks existed, but nzbget fetched 2 of about 11,000 articles. Live `STAT` and `BODY` checks agree that they are 7–12% alive. | No new change is needed: the current full sample drops them. |
| Twins outranked more whole postings. | The bands were 50–90 for twins and 10–49 for other packagings, so a 60%-alive twin went before a 100%-alive repack. | `DupeScore = 9 + 80 × alive`, plus 1 for a twin. After every check, all donors are re-ranked into strictly falling scores. The PR 850 session was asked whether it depends on the old bands and hasn't answered yet. |

## File Structure

| File | Responsibility |
|---|---|
| `nzbget_dupe_proxy.py` | Whole runtime: config, title/NZB helpers, state, Hydra client, donor discovery, HTTP proxy, `main`. |
| `tests/fakes.py` | `FakeNzbget` (JSON-RPC recorder), `FakeHydra` (newznab XML + NZBs), `make_nzb()` builder, server helpers. |
| `tests/test_passthrough.py` | Forwarding, paths, auth, byte-exact responses, ENABLED=false, XML-RPC passthrough. |
| `tests/test_helpers.py` | `normalize_title`, `parse_nzb`, `verify`, `candidate_ok`, log masking. |
| `tests/test_donors.py` | End-to-end append interception with fakes: timing, accept/reject rules, cap, grouping, failures, dry run. |
| `tools/replay_append.py` | Replays a Hydra-identical `append` (real NZB from Hydra) against an in-process DRY_RUN proxy + fake nzbget, prints donors. Touches nothing in production. |
| `deploy/nzbget-dupe-proxy.service` | systemd unit. |
| `deploy/install.sh` | Copies code to `/opt/nzbget-dupe-proxy`, writes env file if absent, enables unit. |
| `README.md` | What/why, config, install, Hydra UI steps, rollback. |

## Interfaces (shared names)

```python
@dataclass
class Config:  # Config.from_env(env: dict) -> Config
    listen_port: int = 6790; nzbget_url: str = "http://127.0.0.1:6789"
    hydra_url: str = ""; hydra_apikey: str = ""; max_donors: int = 8
    size_tolerance: float = 0.02; state_dir: str = "/var/lib/nzbget-dupe-proxy"
    enabled: bool = True; dry_run: bool = False; deadline: float = 60.0; timeout: float = 30.0

@dataclass
class NzbInfo:  # parse_nzb(data: bytes) -> NzbInfo  (raises ValueError on malformed)
    files: int; total_bytes: int; filenames: frozenset; poster: str
    message_ids: frozenset; meta: dict   # fingerprint property: sha1 of sorted ids

normalize_title(name: str) -> str        # "lucifer.s02e14.1080p.bluray.x264-deflate"
title_tokens(name: str) -> set[str]
candidate_ok(primary_title, primary_bytes, result, tol) -> bool
verify(primary: NzbInfo, cand: NzbInfo) -> bool
mask(text: str) -> str
class State:  # State(path); group_for(info, now) -> key|None; seen(key, fp) -> nzbid|None; record(key, fp, nzbid, info, now)
class Proxy:  # Proxy(cfg); serve() / start() -> port; stop(); wait_idle(timeout)
```

---

### Task 1: Pass-through proxy (+ live Hydra protocol confirmation)

**Files:** Create `nzbget_dupe_proxy.py`, `tests/fakes.py`, `tests/test_passthrough.py`, `tests/conftest.py`.

- [ ] **Step 1: failing tests** — `FakeNzbget` records `(method, path, headers, body)` and answers JSON-RPC
  `version`→`"27.0"`, `writelog`→`true`, `append`→next id, anything else→`[]`; raw bytes for XML-RPC.

```python
def test_jsonrpc_forwarded_byte_exact(proxy, nzbget):
    body = b'{"id":7,"jsonrpc":"2.0","method":"status","params":[]}'
    status, hdrs, resp = post(proxy.url + "/jsonrpc", body, auth=("admin", "pw"))
    assert status == 200 and resp == nzbget.last_response
    req = nzbget.requests[-1]
    assert req.path == "/jsonrpc" and req.body == body
    assert req.headers["Authorization"] == basic("admin", "pw")

def test_userpass_path_and_xmlrpc_forwarded(proxy, nzbget):
    post(proxy.url + "/admin:pw/jsonrpc", b'{"method":"version","params":[]}')
    post(proxy.url + "/xmlrpc", b"<?xml version='1.0'?><methodCall><methodName>version</methodName></methodCall>")
    assert [r.path for r in nzbget.requests[-2:]] == ["/admin:pw/jsonrpc", "/xmlrpc"]

def test_upstream_401_passed_through(proxy, nzbget):
    nzbget.require_auth = ("admin", "pw")
    status, _, _ = post(proxy.url + "/jsonrpc", b'{"method":"version","params":[]}')
    assert status == 401

def test_disabled_is_pure_passthrough(make_proxy, nzbget):
    p = make_proxy(enabled=False)
    post(p.url + "/jsonrpc", append_body(title="X"))
    assert nzbget.appends[-1]["params"][6] == ""        # DupeKey untouched
```

- [ ] **Step 2:** `.venv/bin/pytest tests/test_passthrough.py -q` → FAIL (module missing).
- [ ] **Step 3:** implement `Config.from_env`, `mask`, `Handler.do_GET/do_POST` (read body, `urllib.request`
  to `nzbget_url + self.path` with Authorization/Content-Type/User-Agent, return status + body + content-type;
  `HTTPError` → pass its status/body through; connection error → 502), `Proxy.start/stop`, `main()`.
- [ ] **Step 4:** tests PASS.
- [ ] **Step 5:** commit `feat: pass-through nzbget proxy`.
- [ ] **Step 6 (live confirmation):** copy file to server `/tmp`, run
  `ENABLED=false LISTEN_PORT=6791 NZBGET_URL=http://127.0.0.1:6789 python3 -u nzbget_dupe_proxy.py` with a
  debug request log, then `curl -X POST http://192.168.1.93:5076/internalapi/downloader/checkConnection`
  with a downloader JSON pointing at `http://192.168.1.93:6791` (does not save Hydra config). Record the
  logged path/method/auth into this file's Findings. Stop the temp process.

### Task 2: Title and NZB helpers

**Files:** Modify `nzbget_dupe_proxy.py`; create `tests/test_helpers.py`.

- [ ] **Step 1: failing tests**

```python
def test_normalize_title():
    assert normalize_title("Lucifer S02E14 1080p BluRay x264-DEFLATE.nzb") == "lucifer.s02e14.1080p.bluray.x264.deflate"
    assert normalize_title("Some.Movie.2020.1080p.WEB-DL-GRP-xpost [nzbgeek].nzb") == "some.movie.2020.1080p.web.dl.grp"
    assert normalize_title("A_B--C.mkv") == "a.b.c"

def test_parse_nzb_counts_bytes_names_ids():
    data = make_nzb([("rel.part1.rar", [100, 100]), ("rel.par2", [50])], prefix="a", meta={"imdb": "tt123"})
    info = parse_nzb(data)
    assert (info.files, info.total_bytes) == (2, 250)
    assert info.filenames == {"rel.part1.rar", "rel.par2"}
    assert len(info.message_ids) == 3 and info.meta == {"imdb": "tt123"}

def test_parse_nzb_malformed():
    with pytest.raises(ValueError): parse_nzb(b"<nzb><file")

def test_verify_rules():
    p = info(files=10, total=1000, names={"a", "b"})
    assert verify(p, info(files=11, total=1005, names={"x"}))      # size+count
    assert not verify(p, info(files=20, total=1005, names={"x"}))  # count off, names differ
    assert verify(p, info(files=20, total=1500, names={"a", "b", "c"}))  # names shared
    assert not verify(p, info(files=10, total=1020, names={"x"}))  # 2% off

def test_candidate_ok():
    r = Result(title="Lucifer.S02E14.1080p.WEB.H264-OTHER", size=1010, ...)
    assert candidate_ok("Lucifer.S02E14.1080p.WEB.H264-GRP", 1000, r, 0.02)
    assert not candidate_ok("Lucifer.S02E14.1080p.WEB.H264-GRP", 1000, replace(r, size=1100), 0.02)
    assert not candidate_ok("Totally Different Show", 1000, r, 0.02)

def test_mask():
    assert mask("http://h/api?t=search&apikey=SECRET&q=x") == "http://h/api?t=search&apikey=***&q=x"
    assert mask("http://u:p@h/jsonrpc /admin:pw/jsonrpc") == "http://***@h/jsonrpc /***/jsonrpc"
```

- [ ] **Step 2:** run → FAIL. **Step 3:** implement `normalize_title` (strip `.nzb` and a trailing media/archive
  extension, `[...]`/`(...)` tags, trailing junk tokens `xpost postbot obfuscated scrambled asrequested rp
  rakuv* buymore`, separators `[\s._\-]+`→`.`), `title_tokens`, `parse_nzb` (namespace-agnostic, yEnc filename
  = first `"..."` in subject, else subject), `NzbInfo.fingerprint`, `verify`, `candidate_ok`.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: title normalization, NZB fingerprinting, match rules`.

### Task 3: Append interception, donor discovery, state

**Files:** Modify `nzbget_dupe_proxy.py`; create `tests/test_donors.py`; extend `tests/fakes.py` with `FakeHydra`
(`/api` → newznab XML from a list of `Item(title, size, grabs, usenetdate, nzb|status)`, `/getnzb/<n>` → NZB
bytes or 404/garbage, optional delay; records queries).

- [ ] **Step 1: failing tests** (each builds a primary NZB, posts a Hydra-identical append, `proxy.wait_idle()`):

```python
def test_primary_returned_before_discovery(make_proxy, nzbget, hydra):
    hydra.delay = 2.0
    t0 = time.time(); resp = append(proxy, primary_nzb, "Show.S01E01.1080p.WEB-GRP")
    assert time.time() - t0 < 1.0 and resp["result"] == nzbget.appends[0]["id"]
    a = nzbget.appends[0]["params"]
    assert a[6] == "dupes:show.s01e01.1080p.web.grp" and a[7] == 100 and a[8] == "SCORE"

def test_repost_other_title_same_size_accepted():         # different title text, same bytes/count
def test_same_title_wrong_size_rejected():                # 30% bigger -> never fetched
def test_identical_message_ids_rejected():               # same posting on two indexers -> 0 donors
def test_obfuscated_names_same_size_accepted():          # names random, bytes+count match
def test_max_donors_cap_and_scores():                     # 12 good candidates, MAX_DONORS=3 -> 90,89,88
def test_donor_params(): # same key, category, AddPaused False, "SCORE"
def test_hydra_dupekey_kept():                           # Hydra sent DupeKey "x" -> primary+donors use "x"
def test_send_selected_collapses_to_one_key():           # two appends of matching postings -> same key,
                                                         # second's fingerprint already sent as donor ->
                                                         # proxy returns existing NZBID, no second append
def test_hydra_down_primary_ok(caplog):                  # HYDRA_URL to closed port -> primary ok, error logged
def test_candidate_404_and_malformed_primary_ok():       # rejected reasons fetch/parse in summary
def test_malformed_primary_nzb_passthrough():            # primary content not parseable -> forwarded, no worker crash
def test_dry_run_logs_donors_without_appending():
def test_summary_line(caplog):                           # "append key=... nzbid=... candidates=N verified=V added=A rejected={...}"
```

- [ ] **Step 2:** run → FAIL. **Step 3:** implement:
  - `Handler`: if enabled, path ends with `/jsonrpc`, JSON body `method == "append"`, positional params
    of length 10, and content is base64 NZB (not URL) → `Proxy.handle_append`; otherwise pass through.
  - `handle_append`: parse NZB (failure → plain forward with key only); `state.group_for(info)` (recent key
    within 10 min whose stored size/count/names pass `verify`) else Hydra key else `dupes:`+title;
    if fingerprint already sent under key → synthesize `{"version":"1.1","id":..,"result":<nzbid>}`;
    else forward modified params, record fingerprint, start discovery thread if this key has no discovery yet.
  - `discover`: queries (`q=` normalized title with spaces; shortened title up to SxxEyy/year + resolution;
    `t=movie&imdbid=` / `t=tvsearch&tvdbid=` from NZB meta); merge by link; `candidate_ok`; order (one per distinct
    size first, then grabs desc, newer first); fetch ≤ `3*MAX_DONORS` with 4 workers until `MAX_DONORS` verified or
    deadline; verify + fingerprint dedupe (vs primary, added donors, state); append donors with the caller's
    Authorization + path; record state; INFO summary.
  - `State`: JSON `{key: {"t": ts, "files": n, "bytes": b, "names": [...], "fps": {fp: nzbid}}}`, lock,
    atomic write (tmp + `os.replace`), prune entries older than 30 days.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: intercept append and add verified donors`.

### Task 4: Replay tool, packaging, README

**Files:** Create `tools/replay_append.py`, `deploy/nzbget-dupe-proxy.service`, `deploy/install.sh`, `README.md`.

- [ ] `tools/replay_append.py --title "Lucifer S02E14 1080p" [--pick REGEX]`: loads `.env`/env, searches Hydra,
  downloads the picked result's NZB, starts `FakeNzbget` + `Proxy(dry_run=True)` in-process, posts the exact Hydra
  append JSON, waits, prints the summary and donor list. Never contacts production nzbget.
- [ ] Unit:

```ini
[Unit]
Description=nzbget-dupe-proxy (NZBHydra2 -> nzbget duplicate donor injector)
After=network-online.target nzbget.service
Wants=network-online.target
[Service]
User=nzbget
EnvironmentFile=/etc/nzbget-dupe-proxy.env
ExecStart=/usr/bin/python3 -u /opt/nzbget-dupe-proxy/nzbget_dupe_proxy.py
StateDirectory=nzbget-dupe-proxy
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
[Install]
WantedBy=multi-user.target
```

- [ ] `install.sh` (run with sudo on server): `install -d /opt/nzbget-dupe-proxy`, copy module, unit to
  `/etc/systemd/system/`, create `/etc/nzbget-dupe-proxy.env` from `.env.example` only if missing (`chmod 600`,
  `chown root:root`), `systemctl daemon-reload && systemctl enable --now nzbget-dupe-proxy`.
- [ ] README; `wc -l nzbget_dupe_proxy.py` < 400; commit `feat: replay tool, systemd unit, docs`.

### Task 5: Deploy and verify live

- [ ] Run tests on server: copy repo to `/tmp/ndp`, run pytest from pure-python wheels on `PYTHONPATH`
  (no pip install on the server).
- [ ] Install; write `/etc/nzbget-dupe-proxy.env` (HYDRA_URL `http://127.0.0.1:5076`, apikey, defaults).
- [ ] `systemctl status nzbget-dupe-proxy`; `curl` `version` via proxy == via nzbget (`27.0`).
- [ ] Dry run: `tools/replay_append.py --title "Lucifer S02E14 1080p"` on the server; show donor list + timing.
- [ ] **Ask the user** before a real end-to-end append (Category `test`); if approved verify `listgroups`/`history`
  share the DupeKey and donors are DUPE backups; clean up with `GroupFinalDelete` / `HistoryFinalDelete` unless told
  to keep.
- [ ] Push to `origin main`; report tests, dry-run output, Hydra UI steps.
