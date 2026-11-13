import pytest
import web.app as app


@pytest.fixture(autouse=True)
def limiter(monkeypatch):
    app._rate_buckets.clear()
    monkeypatch.setattr(app, '_rate_next_prune', 0.0)
    monkeypatch.setattr(app, '_rate_limit_requests', 5)
    monkeypatch.setattr(app, '_rate_limit_window', 60)
    yield
    app._rate_buckets.clear()


@pytest.mark.parametrize('count', [1, 2, 5, 10, 25, 100])
def test_expired_other_ips_are_removed(monkeypatch, count):
    now = [100.0]
    monkeypatch.setattr(app._time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(app._time, 'time', lambda: now[0])
    for n in range(count):
        assert app._check_rate_limit('old-' + str(n))
    now[0] = 160.0
    assert app._check_rate_limit('new')
    assert set(app._rate_buckets) == {'new'}


@pytest.mark.parametrize('wall', [-1000, 0, 1000, 100000])
def test_wall_clock_jumps_do_not_expire_current_limit(monkeypatch, wall):
    monotonic = [100.0]
    epoch = [100.0]
    monkeypatch.setattr(app._time, 'monotonic', lambda: monotonic[0])
    monkeypatch.setattr(app._time, 'time', lambda: epoch[0])
    for _ in range(5):
        assert app._check_rate_limit('current')
    epoch[0] = wall
    monotonic[0] = 101.0
    assert app._check_rate_limit('current') is False


def test_prune_preserves_current_other_ip(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(app._time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(app._time, 'time', lambda: now[0])
    app._check_rate_limit('old')
    now[0] = 140.0
    for _ in range(5):
        app._check_rate_limit('active')
    now[0] = 161.0
    app._check_rate_limit('new')
    assert 'old' not in app._rate_buckets
    assert app._check_rate_limit('active') is False


def test_no_ip_aliasing(monkeypatch):
    monkeypatch.setattr(app._time, 'monotonic', lambda: 100.0)
    monkeypatch.setattr(app._time, 'time', lambda: 100.0)
    for _ in range(5):
        app._check_rate_limit('a')
    assert app._check_rate_limit('b') is True
    assert app._check_rate_limit('a') is False
