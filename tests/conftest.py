import pytest
from fastapi.testclient import TestClient

from app.main import app, get_store
from app.storage import Store


@pytest.fixture()
def store(tmp_path):
    """每个用例使用独立的临时数据库。"""
    return Store(str(tmp_path / "test.db"))


@pytest.fixture()
def client(store):
    app.dependency_overrides[get_store] = lambda: store
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
