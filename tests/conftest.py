import pytest

import copy

from paper import app as app_module, fetchers, planner

from tests import plan_fixture


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setenv("MORNING_PAPER_NO_SAMPLE", "1")
    monkeypatch.setattr(planner, "SEED_SLOTS", copy.deepcopy(plan_fixture.SLOTS))
    monkeypatch.setattr(app_module, "SEED_PROFILE", dict(plan_fixture.PROFILE))
    """Tests never touch the network: link checks pass, exchange rate fails over to the default."""
    monkeypatch.setattr(fetchers, "check_link", lambda url: ("ok", "200"))

    def boom(*a, **k):
        raise fetchers.FetchError("network disabled in tests")
    monkeypatch.setattr(fetchers, "get", boom)
    monkeypatch.setattr(fetchers, "decode_google_news_url", lambda url: None)
