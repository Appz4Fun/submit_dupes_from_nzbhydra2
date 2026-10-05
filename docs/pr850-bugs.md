# PR 850 and DupeSearch bug tracker

This tracker lists bugs found by reviewing and testing the nzbget `dupe-article-fallback` branch (PR 850 plus
DupeSearch), and their fix status. Each bug is reported to the "update PR850" session, which reports back the
fixing commit. Fixed bugs are pushed to [PR 850](https://github.com/nzbgetcom/nzbget/pull/850) at `58ed2bb3` or later.

| ID | Severity | Area | Summary | Reported | Status | Fix commit |
|---|---|---|---|---|---|---|
| B1 | High | DupeSearch health | Message IDs are sent in `STAT` and `BODY` without angle brackets (`NzbReader.cpp:108`, `NntpHealthServer.cpp:135`). Confirmed live in a sandbox: 7 of 7 postings were called dead, including two that are 100% alive. | 2026-10-05 | Fixed | `35d70589` |
| B2 | High (grouped servers) | ArticleFetcher | The eligible-server count ignores server groups, so each missing article waits out `ArticleTimeout` (`ArticleFetcher.cpp:91`). | 2026-10-05 | Fixed | `58ed2bb3` |
| B3 | Medium | Article fallback | Expected segment offsets assume list neighbours are neighbouring parts, so borrowing fails next to a gap in the NZB (`DupeArticleFallback.cpp:790`). | 2026-10-05 | Open | |
| B4 | Medium | Dead-pick probe | A probe can start after `StopAll` and use the server pool after it is freed (`QueueCoordinator.cpp:217/246`). Also: `Run()` never checks `IsStopped()` before `Abandon()`, and `Measure` leaves `Finished=true` when stopped. | 2026-10-05 | Open | |
| B5 | Medium | DupeSearch health | Detached `Walk` threads outlive the check and can touch the freed server pool (`DonorHealth.cpp:342`). | 2026-10-05 | Open | |
| B6 | Low | Article downloader | Content rejected from one server ends the article without trying the other servers (`ArticleDownloader.cpp:180`). | 2026-10-05 | Open | |
| B7 | Low | Article fallback | The tiling check can demote the correct article after a short one (`DupeArticleFallback.cpp:864`). | 2026-10-05 | Open | |
| B8 | Low | RarReader | A RAR5 variable-length integer can shift by 64 bits or more, which is undefined behaviour (`ReadVLimited`, `ReadV`). | 2026-10-05 | Fixed | `138403e7` |
| B9 | High | ReleaseName | A ranged multi-episode title (`S01E01-E02`) parses as episode 1 only, so double-episode files pair with single-episode postings (`ReleaseName.cpp:298/315`). | 2026-10-05 | Fixed | `95e21105` |
| B10 | High | ReleaseName | `REPACK2` and `PROPER2` aren't seen as repacks, so an original posting becomes a donor for a repack (`ReleaseName.cpp:413`). | 2026-10-05 | Fixed | `95e21105` |
| B11 | High | ReleaseName | `DolbyVision`, `Dolby-Vision`, and `Dolby_Vision` aren't parsed as DV, so the HDR check misses donors or matches SDR (`ReleaseName.cpp:267`). | 2026-10-05 | Fixed | `95e21105` |
| B12 | High | ReleaseName | A title starting with a year-like number (2001, 1917, 2012) gets an empty title, so unrelated movies match (`ReleaseName.cpp:331/425`). | 2026-10-05 | Fixed | `95e21105` |
| B13 | Medium | ReleaseName | `1080i` is folded into `1080p`, so interlaced and progressive encodes match (`ReleaseName.cpp:354`). | 2026-10-05 | Fixed | `95e21105` |
| B14 | Low | Newznab | Named timezones other than UTC (EST, PST) read as UTC; 2-digit years and ISO 8601 dates give 0 (`Newznab.cpp:201/239`). | 2026-10-05 | Open | |
| B15 | Low | DeadPostings | A future-dated entry never expires, and corrupt or unsorted hashes go straight into the sketch (`DeadPostings.cpp:58/68/106`). | 2026-10-05 | Fixed | `13a95b4e` |
| B16 | High | DupeSearch flow | Donors are still added after the user deleted the pick, so an unwanted donor downloads (`DupeSearch.cpp:607/828/1007`). | 2026-10-05 | Fixed | `8a385f0d` |
| B17 | High | DupeSearch flow | A DupeKey change during a search orphans the donors, which then download beside the pick (`DupeSearch.cpp:310/860`). | 2026-10-05 | Fixed | `8a385f0d` |
| B18 | High | DupeSearch dry run | A dry run writes the searched state, so the real search is blocked for 6 hours (`DupeSearch.cpp:264`). | 2026-10-05 | Fixed | `13a95b4e` |
| B19 | Medium | DupeSearch dry run | A dry run writes dead records to disk, which later real searches trust (`DupeSearch.cpp:536/573/676/695/758`). | 2026-10-05 | Fixed | `13a95b4e` |
| B20 | Medium | DupeSearch ranking | Every history dupe under a broad key is rescored and tagged `DupeAlive`, even other releases (`DupeSearch.cpp:343/727`). | 2026-10-05 | Fixed | `c8e01c45` |
| B21 | Medium | DupeSearch resume | Donors deleted from history (kept as `hkDup`) are re-added on resume, and their rescore counts as a success (`DupeSearch.cpp:337/872`). | 2026-10-05 | Fixed | `c8e01c45` |
| B22 | High | History retry | `HistoryRetry` clears every stream-repair job, so "Download remaining" or "Post-process again" loses the saved holes of finished files (`HistoryCoordinator.cpp:697`). | 2026-10-05 | Fixed | `43a9583b` |
| B23 | Medium | Article fallback | A borrowed donor message ID replaces the article's own ID in the saved file state, so a retry never tries the original ID (`DupeArticleFallback.cpp:191`, `QueueCoordinator.cpp:1956`). | 2026-10-05 | Open | |
| B24 | Medium | DiskState | Queue format 66 and file format 9 are always written, so downgrading to upstream nzbget refuses the files and then loses the queue and history (`DiskState.cpp:31`). | 2026-10-05 | Open | |
| B25 | Medium | HealthCheck=dupe | Failover never triggers when the fallback can't apply (RawArticle, par2-only failures, DupeMode force), because the attempt count stays at 0 (`QueueCoordinator.cpp:1474`). | 2026-10-05 | Fixed | `7a6a1cb7` |
| B26 | Low | History return | `MoveToQueue` doesn't reset `DupeAttemptedArticles` or `DupeUnsourcedArticles`, so the failover gate uses the previous attempt's ratio (`HistoryCoordinator.cpp:428`). | 2026-10-05 | Fixed | `7a6a1cb7` |
| B27 | Low | Failover | A backup with a negative score never qualifies when the required score is 0, against the docs (`DupeCoordinator.cpp:432`). | 2026-10-05 | Fixed | `5e88428e` |
| B28 | Low (future) | DiskState | Format 65 is reused for a pre-release layout, so a future upstream format 65 would be misread (`DiskState.cpp:708`). | 2026-10-05 | Open | |
| B29 | Medium | History retry | A file parked mid-download is recorded under its DirectWrite temp name (`<id>.out.tmp`), so on retry its whole-file repair can't pair with the duplicate (`QueueCoordinator.cpp:1040`, `HistoryCoordinator.cpp:193/644`). Found by the PR850 session. | 2026-10-05 | Fixed | `e718ac6f` |
