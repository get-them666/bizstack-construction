"""Crew login lockout: 5 fails locks the phone and the IP; success clears the phone."""
import os

import pytest

import login_throttle as lt

pytestmark = pytest.mark.skipif(not os.getenv("DD_TEST_DATABASE_URL"), reason="set DD_TEST_DATABASE_URL to a scratch Postgres")


@pytest.fixture
def conn():
    import psycopg
    from psycopg.rows import dict_row
    c = psycopg.connect(os.environ["DD_TEST_DATABASE_URL"], row_factory=dict_row)
    with c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS login_throttle;")
    c.commit()
    lt.ensure_schema(c)
    yield c
    c.close()


def test_lockout_after_max_fails(conn):
    for _ in range(lt.MAX_FAILS - 1):
        assert lt.fail(conn, "phone:1", "ip:a") == 0
    assert lt.locked(conn, "phone:1") == 0
    engaged = lt.fail(conn, "phone:1", "ip:a")
    assert engaged == lt.LOCK_MINUTES * 60
    assert lt.locked(conn, "phone:1") > 0
    assert lt.locked(conn, "ip:a") > 0
    assert lt.locked(conn, "phone:other", "ip:a") > 0   # a different phone from the same IP is still blocked


def test_lock_doubles_and_success_clears(conn):
    for _ in range(lt.MAX_FAILS):
        lt.fail(conn, "phone:2")
    lt.clear(conn, "phone:2")   # clear removes lock entirely (fresh row next time)
    assert lt.locked(conn, "phone:2") == 0
    with conn.cursor() as cur:
        cur.execute("INSERT INTO login_throttle (key, fails, lockouts) VALUES ('phone:3', 0, 2);")
    conn.commit()
    for _ in range(lt.MAX_FAILS - 1):
        lt.fail(conn, "phone:3")
    assert lt.fail(conn, "phone:3") == lt.LOCK_MINUTES * 4 * 60


def test_human():
    assert lt.human(900) == "15 minutes"
    assert lt.human(59) == "1 minute"
