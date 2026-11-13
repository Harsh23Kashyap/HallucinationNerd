"""Exercise limiter behavior through the real FastAPI upload route, before verification."""
import pytest
from fastapi.testclient import TestClient
import web.app as app


@pytest.mark.parametrize('wall_jump', [-10000, -10, 0, 10, 10000])
def test_http_limit_ignores_wall_clock_jump(monkeypatch, wall_jump):
    import verify_hallucinations
    monkeypatch.setattr(verify_hallucinations, 'llm_config_error', lambda: None)
    monkeypatch.setenv('HVE_MAX_UPLOAD_BYTES', '0')
    monkeypatch.setattr(app, '_rate_limit_requests', 2)
    monkeypatch.setattr(app, '_rate_limit_window', 60)
    monkeypatch.setattr(app, '_rate_next_prune', 0, raising=False)
    app._rate_buckets.clear()
    clock = {'mono': 100., 'wall': 100.}
    monkeypatch.setattr(app._time, 'monotonic', lambda: clock['mono'])
    monkeypatch.setattr(app._time, 'time', lambda: clock['wall'])
    with TestClient(app.app) as client:
        for _ in range(2):
            assert client.post('/verify', files={'file': ('paper.txt', b'x')}).status_code == 413
        clock.update(mono=101., wall=100.+wall_jump)
        assert client.post('/verify', files={'file': ('paper.txt', b'x')}).status_code == 429
    app._rate_buckets.clear()


@pytest.mark.parametrize('expired_count', [1, 2, 5, 20, 100])
def test_http_request_reclaims_other_expired_buckets(monkeypatch, expired_count):
    import verify_hallucinations
    monkeypatch.setattr(verify_hallucinations, 'llm_config_error', lambda: None)
    monkeypatch.setenv('HVE_MAX_UPLOAD_BYTES', '0')
    monkeypatch.setattr(app, '_rate_limit_requests', 2)
    monkeypatch.setattr(app, '_rate_limit_window', 60)
    monkeypatch.setattr(app, '_rate_next_prune', 0, raising=False)
    app._rate_buckets.clear()
    app._rate_buckets.update({f'old-{n}': [0.] for n in range(expired_count)})
    monkeypatch.setattr(app._time, 'monotonic', lambda: 120.)
    monkeypatch.setattr(app._time, 'time', lambda: 120.)
    with TestClient(app.app) as client:
        assert client.post('/verify', files={'file': ('paper.txt', b'x')}).status_code == 413
    assert set(app._rate_buckets) == {'testclient'}
    app._rate_buckets.clear()
