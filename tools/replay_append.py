#!/usr/bin/env python3
"""Replay a Hydra-identical `append` through a DRY_RUN proxy and print the donors it would add.

Searches Hydra, downloads the picked result's NZB, then starts an in-process fake nzbget and an
in-process proxy (DRY_RUN=1) in front of it and posts exactly what Hydra 9.0.4 would post.
Production nzbget is never contacted.

    python3 tools/replay_append.py --title "Lucifer S02E14 1080p" [--pick REGEX] [--max-donors 8] [--no-verify-count]

HYDRA_URL / HYDRA_APIKEY come from the environment or ./.env.
"""
import argparse
import logging
import os
import re
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nzbget_dupe_proxy as ndp  # noqa: E402
from tests.fakes import FakeNzbget, append_body, post  # noqa: E402


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def replay(title, hydra_url, apikey, pick=None, max_donors=8, verify_count=True):
    """Returns {primary, donors: [would-add lines], summary, fake_appends}."""
    with tempfile.TemporaryDirectory() as state_dir:
        cfg = ndp.Config.from_env({"LISTEN_PORT": "0", "HYDRA_URL": hydra_url, "HYDRA_APIKEY": apikey,
                                   "MAX_DONORS": str(max_donors), "DRY_RUN": "1", "STATE_DIR": state_dir,
                                   "VERIFY_COUNT": str(verify_count)})
        results = ndp.Proxy(cfg).hydra_search({"t": "search", "q": title})
        results = [r for r in results if not pick or re.search(pick, "%s @%s" % (r.title, r.indexer), re.I)]
        if not results:
            raise SystemExit("no Hydra result for %r (pick=%r)" % (title, pick))
        chosen = max(results, key=lambda r: r.grabs)
        with urllib.request.urlopen(chosen.link, timeout=60) as r:
            nzb = r.read()
        nzbget = FakeNzbget()
        cfg.nzbget_url = nzbget.url
        proxy = ndp.Proxy(cfg)
        proxy.start(host="127.0.0.1")
        cap, level = _Capture(), ndp.log.level
        ndp.log.addHandler(cap)
        ndp.log.setLevel(logging.INFO)
        try:
            post(proxy.url + "/jsonrpc", append_body(nzb, title=chosen.title, rid="replay"), auth=("hydra", "replay"))
            proxy.wait_idle(cfg.deadline + 30)
        finally:
            ndp.log.removeHandler(cap)
            ndp.log.setLevel(level)
            proxy.stop()
            nzbget.server.shutdown()
    return {"primary": chosen.title,
            "primary_info": "%s [%s, %d bytes, grabs=%d]" % (chosen.title, chosen.indexer, chosen.size, chosen.grabs),
            "donors": [ln for ln in cap.lines if ln.startswith("DRY-RUN")],
            "summary": next((ln for ln in cap.lines if ln.startswith("append key=")), ""),
            "fake_appends": len(nzbget.appends)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--title", required=True)
    ap.add_argument("--pick", help="regex on '<title> @<indexer>' choosing the primary (most-grabbed match wins)")
    ap.add_argument("--max-donors", type=int, default=8)
    ap.add_argument("--no-verify-count", action="store_true", help="VERIFY_COUNT=false (accept repackaged reposts)")
    a = ap.parse_args()
    if os.path.exists(".env"):
        for line in open(".env"):
            k, _, v = line.strip().partition("=")
            if k and not k.startswith("#"):
                os.environ.setdefault(k, v)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="  %(levelname)s %(message)s")
    out = replay(a.title, os.environ["HYDRA_URL"], os.environ["HYDRA_APIKEY"], a.pick, a.max_donors, not a.no_verify_count)
    print("\nPRIMARY: %s\nWOULD ADD %d DONOR(S):" % (out["primary_info"], len(out["donors"])))
    for d in out["donors"]:
        print("  " + d.replace("DRY-RUN would add donor ", ""))
    print("SUMMARY: " + out["summary"])


if __name__ == "__main__":
    main()
