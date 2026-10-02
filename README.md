# nzbget-dupe-proxy

You click **Send to downloader** once in NZBHydra2. nzbget then receives that NZB plus every other
verified posting of the same release, all linked under one DupeKey.

nzbget's DupeArticleFallback (PR 850 build) repairs missing articles by borrowing them from duplicates
it already knows about. Those are items with the same DupeKey, either in the queue or kept in history as
dupe backups. Hydra sends only one NZB and sets no DupeKey, so reposts and copies on other indexers never
reach nzbget. This proxy fills that gap.

## How it works

```
NZBHydra2 --JSON-RPC--> nzbget-dupe-proxy :6790 --verbatim--> nzbget :6789
                              |  (append only)
                              +--> Hydra newznab API: search, fetch candidate NZBs, verify
                              +--> nzbget append: donors, same DupeKey, DupeScore 90, 89, ...
```

- **Every request is forwarded unchanged**, including its path, `Authorization` header and body.
  nzbget's reply goes back unchanged too. Hydra's connection test, status, categories, queue and
  history calls work as before.
- **`append` (Hydra 9.0.4 always sends JSON-RPC to `/jsonrpc`).** The proxy sets three fields and
  forwards the call at once:
  - `DupeKey`: `dupes:<normalized title>`, or Hydra's own key if it sent one.
  - `DupeScore` 100.
  - `DupeMode` `SCORE`.

  Hydra gets nzbget's NZBID straight away.
- **Background donor discovery** has a 60 s budget per append:
  1. Parse the uploaded NZB: file count, total bytes, file names, and the set of message-IDs, which
     identifies the posting.
  2. Search Hydra by full title and by a short title (`lucifer s02e14 1080p`). If the NZB carries an
     imdb/tvdb meta tag, also search by that id. Each search uses `limit=100`.
  3. Keep results that are the **same release by name**. Both names are parsed with
     [PTT](https://github.com/dreulavelle/PTT), which is vendored under `vendor/ptt`. Extensions,
     `.partNN.rar`/`.7z.001` suffixes and junk tags such as `-xpost` are stripped first. To match:
     - title, season/episode and REPACK/PROPER must be equal;
     - the **release group** must be equal;
     - resolution, source, codec, bit depth, HDR, audio, channels, network and edition must not
       conflict. A value missing on one side is fine.

     Size is **not** a filter: reposts of one release can differ by GBs, mostly because of par2 and
     packaging. `SIZE_TOLERANCE` can add a cap.
  4. Fetch candidates, at most `3 x MAX_DONORS`, 4 at a time. The closest size goes first, then more
     grabs, and one posting per distinct size comes before repeats.
  5. Reject a candidate if either holds:
     - it shares at least half its message-IDs with the primary or with a donor already accepted (the same posting
       from another indexer, sometimes re-listed with a re-uploaded segment);
     - its largest file has a readable name that PTT says is **another release** (for example 720p
       inside a "1080p" listing). Obfuscated inner names are accepted on the strength of the title.
  6. Append up to `MAX_DONORS` donors, ranked closest size first, then grabs, then age. Each gets the
     same DupeKey, score 90, 89, …, the same category, and `AddPaused=false`. nzbget has
     `DupeCheck=yes`, so it moves them straight to history as dupe backups. That makes them donors,
     and also re-download candidates if the primary fails.
- **Donor health ([cyclops](https://github.com/Appz4Fun/cyclops), vendored).** The proxy reads nzbget's
  news servers through JSON-RPC `config`, using Hydra's credentials. Server passwords are never logged. For
  each verified donor (and the primary, for the log) it first `STAT`s a 10-article probe. An NZB with none of
  those on any server is dead, and its full sample is skipped. Every other NZB then gets a 2% sample (at least
  20 articles, at most 300). Each article is tried on every active server in turn, and it counts as missing
  only when all of them answer 430. A donor whose sample is mostly gone is dropped as `dead`. To get donors
  into nzbget quickly, every candidate is probed together in one connection pool. The best `FAST_DONORS` (5)
  that pass the probe are appended at once. Each remaining candidate then gets its full sample and is appended
  as soon as it passes, until `MAX_DONORS`.
- **The real content check is nzbget's.** Before DupeArticleFallback uses any donor bytes, it fetches
  probe articles and compares at least 16 KiB of data. A donor whose name matches but whose content
  differs only costs a few probe articles; it cannot corrupt the download.
- **Idempotency.** `STATE_DIR/state.json` remembers which postings (message-ID fingerprints) went
  out under each key, so the same posting is never sent twice. With Hydra's "send selected", each
  ticked row arrives as its own append within 10 minutes:
  - A matching release joins the first row's DupeKey.
  - A posting that was already sent gets its existing NZBID back.
- **Failure isolation.** If Hydra is down, a candidate fetch returns 403/404, an NZB is malformed, or
  nzbget refuses a donor, only a log line records it. The primary is still added and Hydra still
  sees success.
- **Logging.** Logs go to journald, with one INFO summary line per append:

  ```
  append key=dupes:... nzbid=123 title=... results=100 candidates=9 verified=3 added=3 rejected={'fetch': 2, 'other-release': 1, 'same-posting': 1} time=6.2s
  ```

  apikeys and passwords are masked.

## Configuration

`/etc/nzbget-dupe-proxy.env` (mode 600, owner root):

| Variable | Default | Meaning |
|---|---|---|
| `LISTEN_PORT` | `6790` | proxy port (listens on 0.0.0.0) |
| `NZBGET_URL` | `http://127.0.0.1:6789` | real nzbget |
| `HYDRA_URL` | (none) | NZBHydra2 base URL, e.g. `http://127.0.0.1:5076` |
| `HYDRA_APIKEY` | (none) | Hydra API key |
| `MAX_DONORS` | `8` | max donors per append |
| `SIZE_TOLERANCE` | `0` | `0` = size is not a filter; for example `0.2` skips Hydra results more than 20% off |
| `HEALTH_PERCENT` | `2` | STAT this % of each NZB's articles (min 20, max 300) on **every** active news server from nzbget's config; `0` = off |
| `DONOR_MIN_ALIVE` | `0.5` | drop a donor (`rejected: dead`) when less than this share of its sampled articles exists on any server |
| `HEALTH_CONNECTIONS` | `8` | connections per news server for the health check, capped at half of nzbget's own `Connections` for that server |
| `HEALTH_BUDGET` | `120` | seconds per health pass: the quick probe of all donors, or the full sample of one donor. Articles still unanswered count as unknown and the donor is kept |
| `FAST_DONORS` | `5` | donors appended right after the quick probe. The rest are appended one at a time, each after its full sample |
| `STATE_DIR` | `/var/lib/nzbget-dupe-proxy` | state file location |
| `ENABLED` | `true` | `false` = pure pass-through (kill switch) |
| `DRY_RUN` | `0` | `1` = discover and log donors, append only the primary (also `--dry-run`) |
| `DEADLINE` / `TIMEOUT` | `60` / `30` | discovery budget and per-request timeout, in seconds |

The proxy uses the credentials Hydra sends for its own nzbget calls. It needs no nzbget password of
its own.

## Install (server)

```bash
git clone git@github.com:Appz4Fun/submit_dupes_from_nzbhydra2.git && cd submit_dupes_from_nzbhydra2
sudo sh deploy/install.sh        # /opt/nzbget-dupe-proxy, systemd unit, env template if missing
sudoedit /etc/nzbget-dupe-proxy.env   # set HYDRA_APIKEY
sudo systemctl restart nzbget-dupe-proxy
journalctl -u nzbget-dupe-proxy -f
```

## Hydra setup

Leave the existing **NZBGet** downloader as it is.

1. Open **Config → Downloading → Add new downloader → NZBGet**.
2. Enter these values:
   - Name: `NZBGet + dupes`
   - URL: `http://192.168.1.93:6790`
   - Username and password: the same as the existing NZBGet entry.
   - NZB adding type: **Upload**. Hydra must upload the NZB content; in link mode the proxy only sets
     the DupeKey.
3. Click **Test connection**, then **Save**.

To send a release with donors, choose **NZBGet + dupes** in Hydra. Choose **NZBGet** to send it the
old way.

## Try it without touching nzbget

```bash
python3 tools/replay_append.py --title "Lucifer S02E14 1080p" --pick "FraMeST"
```

The script searches Hydra and downloads the picked NZB. It then sends exactly the `append` that Hydra
would send, to an in-process DRY_RUN proxy backed by a fake nzbget. Finally it prints the donors that
would be added.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/pytest -q
```

The tests run against in-process fakes of nzbget (JSON-RPC) and Hydra (newznab XML plus NZBs).

## Rollback

```bash
sudo systemctl disable --now nzbget-dupe-proxy
```

Then delete the **NZBGet + dupes** downloader in Hydra. To remove the proxy completely:

```bash
sudo rm -rf /opt/nzbget-dupe-proxy /etc/systemd/system/nzbget-dupe-proxy.service /etc/nzbget-dupe-proxy.env /var/lib/nzbget-dupe-proxy
```

## Notes and limits

- Each candidate fetch counts as a grab at that indexer, so fetches are capped at `3 x MAX_DONORS`.
  Indexers that are over their grab limit return 403, which is counted as `rejected: fetch`.
- Release identity is name-based. If an indexer lists a posting under a renamed or obfuscated title,
  it cannot be matched, because Hydra's title is the only signal available before fetching.
- Possible phase 2: probe the first article of each candidate over NNTP and read the container
  header, the inner file name and the size.
- Donor `.nzb` files must stay on disk for nzbget to use them. That needs `NzbCleanupDisk=no` and
  `KeepHistory` greater than 0; both are already set on this server.
