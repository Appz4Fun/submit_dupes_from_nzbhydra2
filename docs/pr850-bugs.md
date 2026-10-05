# PR 850 and DupeSearch bug tracker

This tracker lists bugs found by reviewing and testing the nzbget `dupe-article-fallback` branch (PR 850 plus
DupeSearch), and their fix status. Each bug is reported to the "update PR850" session, which reports back the
fixing commit. Fixed bugs are pushed to [PR 850](https://github.com/nzbgetcom/nzbget/pull/850) at `58ed2bb3` or later.

| ID | Severity | Area | Summary | Reported | Status | Fix commit |
|---|---|---|---|---|---|---|
| B1 | High | DupeSearch health | Message IDs are sent in `STAT` and `BODY` without angle brackets (`NzbReader.cpp:108`, `NntpHealthServer.cpp:135`). Confirmed live in a sandbox: 7 of 7 postings were called dead, including two that are 100% alive. | 2026-10-05 | Fixed | `35d70589` |
| B2 | High (grouped servers) | ArticleFetcher | The eligible-server count ignores server groups, so each missing article waits out `ArticleTimeout` (`ArticleFetcher.cpp:91`). | 2026-10-05 | Fixed | `58ed2bb3` |
| B3 | Medium | Article fallback | Expected segment offsets assume list neighbours are neighbouring parts, so borrowing fails next to a gap in the NZB (`DupeArticleFallback.cpp:790`). | 2026-10-05 | Fixed | `` |
| B4 | Medium | Dead-pick probe | A probe can start after `StopAll` and use the server pool after it is freed (`QueueCoordinator.cpp:217/246`). Also: `Run()` never checks `IsStopped()` before `Abandon()`, and `Measure` leaves `Finished=true` when stopped. | 2026-10-05 | Fixed | `19a5649b` |
| B5 | Medium | DupeSearch health | Detached `Walk` threads outlive the check and can touch the freed server pool (`DonorHealth.cpp:342`). | 2026-10-05 | Fixed | `3a87580d` |
| B6 | Low | Article downloader | Content rejected from one server ends the article without trying the other servers (`ArticleDownloader.cpp:180`). | 2026-10-05 | Fixed | `73644a2f` |
| B7 | Low | Article fallback | The tiling check can demote the correct article after a short one (`DupeArticleFallback.cpp:864`). | 2026-10-05 | Fixed | `` |
| B8 | Low | RarReader | A RAR5 variable-length integer can shift by 64 bits or more, which is undefined behaviour (`ReadVLimited`, `ReadV`). | 2026-10-05 | Fixed | `138403e7` |
| B9 | High | ReleaseName | A ranged multi-episode title (`S01E01-E02`) parses as episode 1 only, so double-episode files pair with single-episode postings (`ReleaseName.cpp:298/315`). | 2026-10-05 | Fixed | `95e21105` |
| B10 | High | ReleaseName | `REPACK2` and `PROPER2` aren't seen as repacks, so an original posting becomes a donor for a repack (`ReleaseName.cpp:413`). | 2026-10-05 | Fixed | `95e21105` |
| B11 | High | ReleaseName | `DolbyVision`, `Dolby-Vision`, and `Dolby_Vision` aren't parsed as DV, so the HDR check misses donors or matches SDR (`ReleaseName.cpp:267`). | 2026-10-05 | Fixed | `95e21105` |
| B12 | High | ReleaseName | A title starting with a year-like number (2001, 1917, 2012) gets an empty title, so unrelated movies match (`ReleaseName.cpp:331/425`). | 2026-10-05 | Fixed | `95e21105` |
| B13 | Medium | ReleaseName | `1080i` is folded into `1080p`, so interlaced and progressive encodes match (`ReleaseName.cpp:354`). | 2026-10-05 | Fixed | `95e21105` |
| B14 | Low | Newznab | Named timezones other than UTC (EST, PST) read as UTC; 2-digit years and ISO 8601 dates give 0 (`Newznab.cpp:201/239`). | 2026-10-05 | Fixed | `4cddb683` |
| B15 | Low | DeadPostings | A future-dated entry never expires, and corrupt or unsorted hashes go straight into the sketch (`DeadPostings.cpp:58/68/106`). | 2026-10-05 | Fixed | `13a95b4e` |
| B16 | High | DupeSearch flow | Donors are still added after the user deleted the pick, so an unwanted donor downloads (`DupeSearch.cpp:607/828/1007`). | 2026-10-05 | Fixed | `8a385f0d` |
| B17 | High | DupeSearch flow | A DupeKey change during a search orphans the donors, which then download beside the pick (`DupeSearch.cpp:310/860`). | 2026-10-05 | Fixed | `8a385f0d` |
| B18 | High | DupeSearch dry run | A dry run writes the searched state, so the real search is blocked for 6 hours (`DupeSearch.cpp:264`). | 2026-10-05 | Fixed | `13a95b4e` |
| B19 | Medium | DupeSearch dry run | A dry run writes dead records to disk, which later real searches trust (`DupeSearch.cpp:536/573/676/695/758`). | 2026-10-05 | Fixed | `13a95b4e` |
| B20 | Medium | DupeSearch ranking | Every history dupe under a broad key is rescored and tagged `DupeAlive`, even other releases (`DupeSearch.cpp:343/727`). | 2026-10-05 | Fixed | `c8e01c45` |
| B21 | Medium | DupeSearch resume | Donors deleted from history (kept as `hkDup`) are re-added on resume, and their rescore counts as a success (`DupeSearch.cpp:337/872`). | 2026-10-05 | Fixed | `c8e01c45` |
| B22 | High | History retry | `HistoryRetry` clears every stream-repair job, so "Download remaining" or "Post-process again" loses the saved holes of finished files (`HistoryCoordinator.cpp:697`). | 2026-10-05 | Fixed | `43a9583b` |
| B23 | Medium | Article fallback | A borrowed donor message ID replaces the article's own ID in the saved file state, so a retry never tries the original ID (`DupeArticleFallback.cpp:191`, `QueueCoordinator.cpp:1956`). | 2026-10-05 | Fixed | `490702ac` |
| B24 | Medium | DiskState | Queue format 66 and file format 9 are always written, so downgrading to upstream nzbget refuses the files and then loses the queue and history (`DiskState.cpp:31`). | 2026-10-05 | Fixed | `d4ca8482` |
| B25 | Medium | HealthCheck=dupe | Failover never triggers when the fallback can't apply (RawArticle, par2-only failures, DupeMode force), because the attempt count stays at 0 (`QueueCoordinator.cpp:1474`). | 2026-10-05 | Fixed | `7a6a1cb7` |
| B26 | Low | History return | `MoveToQueue` doesn't reset `DupeAttemptedArticles` or `DupeUnsourcedArticles`, so the failover gate uses the previous attempt's ratio (`HistoryCoordinator.cpp:428`). | 2026-10-05 | Fixed | `7a6a1cb7` |
| B27 | Low | Failover | A backup with a negative score never qualifies when the required score is 0, against the docs (`DupeCoordinator.cpp:432`). | 2026-10-05 | Fixed | `5e88428e` |
| B28 | Low (future) | DiskState | Format 65 is reused for a pre-release layout, so a future upstream format 65 would be misread (`DiskState.cpp:708`). | 2026-10-05 | Fixed | `d4ca8482` |
| B29 | Medium | History retry | A file parked mid-download is recorded under its DirectWrite temp name (`<id>.out.tmp`), so on retry its whole-file repair can't pair with the duplicate (`QueueCoordinator.cpp:1040`, `HistoryCoordinator.cpp:193/644`). Found by the PR850 session. | 2026-10-05 | Fixed | `e718ac6f` |
| B30 | Medium | Article fallback | `MatchDonorFile` does not pair a twin when the pick's NZB is missing a segment, so borrowing never starts for files an indexer did not fully capture. Confirmed by the PR850 session. | 2026-10-05 | Fixed | `6b01e5e7` |
| B31 | High | DiskState | The `dupestate` marker makes an earlier branch build's format-66 queue unreadable after a downgrade and upgrade, so the queue and history are wiped (`DiskState.cpp:356`). Found by independent verification. | 2026-10-05 | Fixed | `ecfa233b` |
| B32 | Medium | Article fallback | The B30 gap pairing's total-size guard always passes, so a non-twin with up to 1/16 more same-size parts can pair, and its offsets line up (`DupeArticleFallback.cpp:778`). | 2026-10-05 | Fixed | `eb1d2904` |
| B33 | Low | DupeSearch flow | The B16/B17 check-then-add window: `PickGone` releases the lock before the append (`DupeSearch.cpp:876`). | 2026-10-05 | Fixed | `581a82f4` |
| B34 | Medium | DiskState | A donor article staged but not yet merged is saved as `aiFinished`, so upstream with DirectWrite treats its bytes as present, and the file completes with a hole (`DiskState.cpp:1470`). | 2026-10-05 | Fixed | `e3629410` |
| B35 | Low (future) | DiskState | File-state formats 8 and 9 are accepted without the marker gate (`DiskState.cpp:1331/1495/2226/2287`). | 2026-10-05 | Fixed | `ecfa233b` (an unreadable file is set aside, not lost) |
| B36 | Low | HealthCheck=dupe | Under par deferral, `attempted == 0` lets a failover fire before borrowing got a turn (`QueueCoordinator.cpp:1483`). | 2026-10-05 | Fixed | `8a89c094` |
| B37 | Low | DupeSearch health | Shutdown can wait the whole `DupeHealthBudget`, because walkers don't notify `state->cond` on stop (`DonorHealth.cpp:383`). | 2026-10-05 | Fixed | `5b0328f8` |
| B38 | Medium | ReleaseName | The B9 range rewrite removes `-NNNN` (a year or resolution) when the range is invalid, and a name without a group takes `e02` as its group (`ReleaseName.cpp:279-289`). | 2026-10-05 | Fixed | `e7cb4608` |
| B39 | High | Article fallback | The B32 rule (allow one trailing extra part) still paired a dead 29-part file with an unrelated 30-part file, and 4–6 wrong articles were borrowed, so `dupefailoverchain` is flaky. Root cause found by the PR850 session. Fix in progress: pair only when the declared part counts `(n/N)` agree. | 2026-10-05 | Fixed | `f94f8069` |
| B40 | Low (deferred) | Article fallback | Borrowed articles aren't checked against par2 slice checksums (IFSC) at download time. Releases with par2 are still caught by the par-check after download; releases without par2 rely on B39's declared-count rule. Deferred: it needs an IFSC parser, slice mapping and pairing state (a few hundred lines). | 2026-10-05 | Deferred | |
| B41 | Medium | DupeSearch flow | `m_sent` is keyed by DupeKey, lives in memory and is never cleared, so a re-submitted pick's search silently refuses its donors as "already sent". Known postings also counted user-deleted items. Found live on Lanterns S01E04 and confirmed by the PR850 session. | 2026-10-05 | Open | |
| B42 | Medium | Queue (live, 27.1) | Lanterns S01E04 (nzbid 2425) is stuck QUEUED after the 27.1 restart: all 45 files are complete (0 remaining, 0 active, health 1000), but it never moves to post-processing, and its log is empty. It had been removed from the queue and returned before that. | 2026-10-05 | Open | |

B1–B30 were reported fixed by `d4ca8482`. An independent review rates B9, B16, B17, B24 and B30 as partial fixes, with follow-ups B31–B38. Known limit, by design: a history item's completed-file state is rewritten in the upstream format only when it is touched, so after a downgrade, upstream nzbget can't retry such an item. The queue and history themselves load fine.
