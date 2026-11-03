import time

import pytest

# Hermetic: stub time.sleep
_original_sleep = time.sleep
time.sleep = lambda *_a, **_k: None


@pytest.fixture
def real_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", _original_sleep)
