import os
import tempfile

import pytest

# 必须在导入 app.* 之前设置
_tmpdir = tempfile.mkdtemp(prefix="tide-test-")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["SEED_DEMO"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402


def seed_initial_revision(levels: dict, note: str = "测试初始修订") -> int:
    """测试辅助：直接写入第一版潮位（生产可通过导入脚本/更正链获得）。"""
    conn = connect()
    try:
        rev = conn.execute(
            "INSERT INTO tide_revisions(note) VALUES (?)", (note,)
        ).lastrowid
        conn.executemany(
            "INSERT INTO tide_values(revision, minute, level) VALUES (?, ?, ?)",
            [(rev, m, lvl) for m, lvl in sorted(levels.items())],
        )
        return rev
    finally:
        conn.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    from app import config
    monkeypatch.setattr(config, "DB_PATH", str(db_file))

    conn = connect()
    init_db(conn)
    conn.close()

    with TestClient(app) as c:
        yield c


def add_node(client, nid, name=None):
    r = client.post("/api/nodes", json={"node_id": nid, "name": name})
    assert r.status_code == 201, r.text
    return r.json()


def add_segment(client, origin, dest, travel_time, depth=99.0, clearance=None):
    r = client.post(
        "/api/segments",
        json={
            "origin": origin, "destination": dest,
            "travel_time": travel_time, "depth": depth, "clearance": clearance,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["seg_id"]


PLAN_BODY = {
    "origin": "A", "destination": "B",
    "start_minute": 0, "window_minutes": 20,
    "draft": 6.0, "height": 1.0, "margin": 0.5,
}
