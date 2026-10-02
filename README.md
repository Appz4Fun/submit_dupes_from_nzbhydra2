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
  3. Keep results whose size is within `SIZE_TOLERANCE` (2%) and that have the same episode/year. The
     title must also match normally or share at least 50% of its tokens.
  4. Fetch candidates, at most `3 x MAX_DONORS`, 4 at a time. One posting of each distinct size goes
     first, then the rest by grabs.
  5. Accept a candidate if either holds:
     - total bytes within 1% **and** file count within ±10%;
     - at least 50% of file names are shared.
  6. Reject a candidate whose message-ID set equals the primary's, or that of a donor already
     accepted. Such a candidate is the same posting from another indexer.
  7. Append up to `MAX_DONORS` donors, ranked by grabs then age. Each gets the same DupeKey, score
     90, 89, …, the same category, and `AddPaused=false`. nzbget has `DupeCheck=yes`, so it moves
     them straight to history as dupe backups. That makes them donors, and also re-download
     candidates if the primary fails.
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
  append key=dupes:... nzbid=123 title=... results=100 candidates=9 verified=3 added=3 rejected={'fetch': 2, 'mismatch': 3, 'same-posting': 1} time=6.2s
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
| `SIZE_TOLERANCE` | `0.02` | Hydra size pre-filter (fraction) |
| `VERIFY_COUNT` | `true` | `false` drops the ±10% file-count clause, so a same-size repost with different packaging is accepted |
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
- The verify rule is strict. A repost of the same release that is packaged differently has about the
  same bytes but a different file count, for example 7z versus rar, or 20 versus 8 volumes. Unless it
  shares file names, the rule rejects it as `mismatch`. nzbget's cross-pack repair could use some of
  those postings.
- Donor `.nzb` files must stay on disk for nzbget to use them. That needs `NzbCleanupDisk=no` and
  `KeepHistory` greater than 0; both are already set on this server.
