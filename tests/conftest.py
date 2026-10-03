import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nzbget_dupe_proxy as ndp  # noqa: E402
from tests.fakes import FakeHydra, FakeNzbget  # noqa: E402


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(ndp, "FETCH_RETRY_DELAY", 0.01)
    monkeypatch.setattr(ndp.donor_health, "SERVER_RETRY_AFTER", 0.05)


@pytest.fixture
def nzbget():
    f = FakeNzbget()
    yield f
    f.server.shutdown()


@pytest.fixture
def hydra():
    f = FakeHydra()
    yield f
    f.server.shutdown()


@pytest.fixture
def make_proxy(nzbget, hydra, tmp_path):
    started = []

    def make(**overrides):
        env = {"LISTEN_PORT": "0", "NZBGET_URL": nzbget.url, "HYDRA_URL": hydra.url,
               "HYDRA_APIKEY": "KEY", "STATE_DIR": str(tmp_path / "state")}
        env.update({k.upper(): str(v) for k, v in overrides.items()})
        p = ndp.Proxy(ndp.Config.from_env(env))
        p.start(host="127.0.0.1")
        started.append(p)
        return p

    yield make
    for p in started:
        p.stop()


@pytest.fixture
def proxy(make_proxy):
    return make_proxy()
