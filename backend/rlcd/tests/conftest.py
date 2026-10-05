import os
import tempfile
import time

# The module-level app in rlcd.api must never touch the real artifacts dir or autotrain during tests.
os.environ["RLCD_HOME"] = tempfile.mkdtemp(prefix="rlcd-test-home-")
os.environ["RLCD_AUTOTRAIN"] = "0"
os.environ.pop("RLCD_API_KEY", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rlcd.api import create_app  # noqa: E402
from rlcd.quant import QuantClient  # noqa: E402
from tests.synth import SynthQuant  # noqa: E402

SETUP_TRAIN = {"heads": ["setup:15m"], "symbols": ["XAUUSD", "EURUSD", "GBPUSD"], "horizon": 8,
               "n_estimators": 120}


def make_client(tmp_path, synth: SynthQuant | None = None) -> TestClient:
    synth = synth or SynthQuant(up=False)
    quant = QuantClient("http://quant.test", transport=synth.client_transport())
    client = TestClient(create_app(home=tmp_path, quant=quant, autotrain=False))
    client.synth = synth
    return client


def train(client: TestClient, body: dict, timeout: float = 300.0) -> dict:
    """POST /v1/train and poll the job until it finishes."""
    r = client.post("/v1/train", json=body)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/v1/train/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError("training did not finish in time")


@pytest.fixture
def client(tmp_path):
    """A fresh, untrained service whose quant sidecar is down."""
    return make_client(tmp_path)


@pytest.fixture(scope="session")
def intent_client(tmp_path_factory):
    c = make_client(tmp_path_factory.mktemp("intent"))
    job = train(c, {"heads": ["intent", "trade_style"]})
    assert job["status"] == "succeeded", job
    c.job = job
    return c


@pytest.fixture(scope="session")
def planted(tmp_path_factory):
    """Setup head trained on synthetic rows with a planted edge."""
    c = make_client(tmp_path_factory.mktemp("planted"), SynthQuant(signal=2.0))
    job = train(c, SETUP_TRAIN)
    assert job["status"] == "succeeded", job
    c.job = job
    return c


@pytest.fixture(scope="session")
def noise(tmp_path_factory):
    """Setup head trained on synthetic rows whose outcomes are independent of the features."""
    c = make_client(tmp_path_factory.mktemp("noise"), SynthQuant(signal=0.0))
    job = train(c, SETUP_TRAIN)
    assert job["status"] == "succeeded", job
    c.job = job
    return c
