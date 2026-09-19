"""Shared fixtures.

Note that importing corbelity.workbench.app runs load_dotenv(), because the app is an
application and reads its own .env. That means a developer's real .env is in play during
the test run, so anything that depends on the environment pins it explicitly rather than
assuming a clean one.
"""
import pytest

from corbelity.workbench import app as workbench


@pytest.fixture(autouse=True)
def builtin_catalog_only(monkeypatch):
    """Pin every test to the built-in model catalog.

    CORBELITY_MODEL_CATALOG merges a user's own file over the built-in one, so without
    this a developer with that variable set in .env would be running a different catalog
    than CI -- and the catalog tests below assert on specific model ids."""
    monkeypatch.setattr(workbench, "CONFIG", workbench.CONFIG.with_overrides(catalog_path=None))
