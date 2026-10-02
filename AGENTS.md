# AGENTS.md

## Rule #1: commit identity and signing

Every commit in this repo MUST be authored and committed as **xbmc4lyfe**
(`xbmc4lyfe <273732874+xbmc4lyfe@users.noreply.github.com>`) and MUST be
**signed with xbmc4lyfe's SSH signing key**. No exceptions, no other identities,
no unsigned commits. Before committing, check:

```bash
git config user.name        # xbmc4lyfe
git config commit.gpgsign   # true
git log -1 --show-signature # Good "git" signature for 273732874+xbmc4lyfe@...
```

Push to `git@github.com:Appz4Fun/submit_dupes_from_nzbhydra2.git` as xbmc4lyfe.

## Project

`nzbget-dupe-proxy`: a stdlib-only Python 3 HTTP proxy that sits between
NZBHydra2 and nzbget. It passes every request through to nzbget unchanged,
except JSON-RPC `append`, where it gives the NZB a DupeKey and, in the
background, finds other postings of the same release through Hydra and appends
them as lower-scored duplicates (donors for nzbget's DupeArticleFallback).
See README.md (usage) and PLAN.md (design, Hydra protocol findings).

## Rules

- Runtime code (`nzbget_dupe_proxy.py`) is Python 3 stdlib only. pytest is for tests only.
- TDD: write the failing test first (`tests/`), then the code. Run `.venv/bin/pytest -q` before every commit.
- Never commit secrets. `.env` is gitignored; `.env.example` holds names only.
  Logs must mask apikeys and passwords.
- Never restart or reconfigure nzbget or NZBHydra2 on the server, and never edit `nzbhydra.yml`.
- Real appends to production nzbget need explicit user approval each time.
