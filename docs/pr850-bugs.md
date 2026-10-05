# PR 850 and DupeSearch bug tracker

This tracker lists bugs found by reviewing and testing the nzbget `dupe-article-fallback` branch (PR 850 plus
DupeSearch), and their fix status. Each bug is reported to the "update PR850" session, which reports back the
fixing commit.

| ID | Severity | Area | Summary | Reported | Status | Fix commit |
|---|---|---|---|---|---|---|
| B1 | High | DupeSearch health | Message IDs are sent in `STAT` and `BODY` without angle brackets (`NzbReader.cpp:108`, `NntpHealthServer.cpp:135`). Confirmed live in a sandbox: 7 of 7 postings were called dead, including two that are 100% alive. | 2026-10-05 | Open | |
| B2 | High (grouped servers) | ArticleFetcher | The eligible-server count ignores server groups, so each missing article waits out `ArticleTimeout` (`ArticleFetcher.cpp:91`). | 2026-10-05 | Open | |
| B3 | Medium | Article fallback | Expected segment offsets assume list neighbours are neighbouring parts, so borrowing fails next to a gap in the NZB (`DupeArticleFallback.cpp:790`). | 2026-10-05 | Open | |
| B4 | Medium | Dead-pick probe | A probe can start after `StopAll` and use the server pool after it is freed (`QueueCoordinator.cpp:217/246`). | 2026-10-05 | Open | |
| B5 | Medium | DupeSearch health | Detached `Walk` threads outlive the check and can touch the freed server pool (`DonorHealth.cpp:342`). | 2026-10-05 | Open | |
| B6 | Low | Article downloader | Content rejected from one server ends the article without trying the other servers (`ArticleDownloader.cpp:180`). | 2026-10-05 | Open | |
| B7 | Low | Article fallback | The tiling check can demote the correct article after a short one (`DupeArticleFallback.cpp:864`). | 2026-10-05 | Open | |
| B8 | Low | RarReader | A RAR5 variable-length integer can shift by 64 bits or more, which is undefined behaviour (`ReadVLimited`, `ReadV`). | 2026-10-05 | Open | |
| B9 | High | ReleaseName | A ranged multi-episode title (`S01E01-E02`) parses as episode 1 only, so double-episode files pair with single-episode postings (`ReleaseName.cpp:298/315`). | 2026-10-05 | Open | |
| B10 | High | ReleaseName | `REPACK2` and `PROPER2` aren't seen as repacks, so an original posting becomes a donor for a repack (`ReleaseName.cpp:413`). | 2026-10-05 | Open | |
| B11 | High | ReleaseName | `DolbyVision`, `Dolby-Vision`, and `Dolby_Vision` aren't parsed as DV, so the HDR check misses donors or matches SDR (`ReleaseName.cpp:267`). | 2026-10-05 | Open | |
| B12 | High | ReleaseName | A title starting with a year-like number (2001, 1917, 2012) gets an empty title, so unrelated movies match (`ReleaseName.cpp:331/425`). | 2026-10-05 | Open | |
| B13 | Medium | ReleaseName | `1080i` is folded into `1080p`, so interlaced and progressive encodes match (`ReleaseName.cpp:354`). | 2026-10-05 | Open | |
| B14 | Low | Newznab | Named timezones other than UTC (EST, PST) read as UTC; 2-digit years and ISO 8601 dates give 0 (`Newznab.cpp:201/239`). | 2026-10-05 | Open | |
| B15 | Low | DeadPostings | A future-dated entry never expires, and corrupt or unsorted hashes go straight into the sketch (`DeadPostings.cpp:58/68/106`). | 2026-10-05 | Open | |
| B16 | High | DupeSearch flow | Donors are still added after the user deleted the pick, so an unwanted donor downloads (`DupeSearch.cpp:607/828/1007`). | 2026-10-05 | Open | |
| B17 | High | DupeSearch flow | A DupeKey change during a search orphans the donors, which then download beside the pick (`DupeSearch.cpp:310/860`). | 2026-10-05 | Open | |
| B18 | High | DupeSearch dry run | A dry run writes the searched state, so the real search is blocked for 6 hours (`DupeSearch.cpp:264`). | 2026-10-05 | Open | |
| B19 | Medium | DupeSearch dry run | A dry run writes dead records to disk, which later real searches trust (`DupeSearch.cpp:536/573/676/695/758`). | 2026-10-05 | Open | |
| B20 | Medium | DupeSearch ranking | Every history dupe under a broad key is rescored and tagged `DupeAlive`, even other releases (`DupeSearch.cpp:343/727`). | 2026-10-05 | Open | |
| B21 | Medium | DupeSearch resume | Donors deleted from history (kept as `hkDup`) are re-added on resume, and their rescore counts as a success (`DupeSearch.cpp:337/872`). | 2026-10-05 | Open | |
