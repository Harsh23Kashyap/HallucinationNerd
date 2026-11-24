"""Offline regressions for the real screening resolver and ASGI upload route."""
import json
import threading
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
import verify_hallucinations as vh
from web import app as wa
from web import citation_existence as ce

FEED = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1706.03762v7</id><title>Existing paper</title></entry></feed>'
PAPER = 'Claim [1].\nReferences\n[1] Wrong source arXiv:1706.03762v7\n[2] Title only 2020.\n'

@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    ce._cache.clear(); ce._next_request = 0
    wa._rate_buckets.clear()
    monkeypatch.setattr(ce.requests, 'get', Mock(side_effect=AssertionError('Unexpected network')))
    monkeypatch.delenv('HVE_OFFLINE', raising=False)


def response(status=200, body=FEED):
    r = Mock(status_code=status)
    r.__enter__ = Mock(return_value=r); r.__exit__ = Mock(return_value=False)
    r.iter_content.return_value = [body]
    return r


def file(tmp_path, text=PAPER, suffix='.txt'):
    p = tmp_path / ('paper' + suffix); p.write_text(text); return str(p)


def test_exact_version():
    assert not ce.parse_metadata(FEED, ['1706.03762v1'])


def test_unversioned_matches():
    assert ce.parse_metadata(FEED, ['1706.03762'])['1706.03762']['resolved_id'].endswith('v7')


def test_no_title_unknown():
    assert ce.parse_metadata(FEED.replace(b'Existing paper', b''), ['1706.03762']) == {}


def test_entity_rejected():
    with pytest.raises(ValueError): ce.parse_metadata(b'<!DOCTYPE x>' + FEED, [])


def test_foreign_url_rejected():
    assert ce.normalize('https://attacker.invalid/1706.03762') is None


def test_parser_keeps_unsupported(tmp_path):
    rows, warnings = ce.extract_rows(file(tmp_path), '.txt')
    assert len(rows) == 2 and rows[0]['arxiv_id'] == '1706.03762v7'
    assert rows[1]['reason'] == 'missing_or_unsupported_identifier' and warnings


def test_no_bib_no_search(tmp_path):
    rows, warnings = ce.extract_rows(file(tmp_path, 'arXiv:1706.03762'), '.txt')
    assert rows == [] and 'incomplete' in warnings[0]


def test_duplicate_marker_ambiguous(tmp_path):
    rows, _ = ce.extract_rows(file(tmp_path, PAPER + '[1] Other arXiv:1810.04805\n'), '.txt')
    assert rows[0]['arxiv_id'] is None and rows[-1]['arxiv_id'] is None


def test_multiple_ids_ambiguous(tmp_path):
    rows, _ = ce.extract_rows(file(tmp_path, PAPER.replace('1706.03762v7', '1706.03762v7 arXiv:1810.04805')), '.txt')
    assert rows[0]['reason'] == 'ambiguous_identifiers'


def test_structured_coverage(tmp_path):
    rows, warnings = ce.extract_rows(file(tmp_path, json.dumps([{'retrieved_articles': [{'id': '1706.03762'}, {}]}]), '.json'), '.json')
    assert len(rows) == 2 and 'supplied' in warnings[0]


def test_structured_invalid(tmp_path):
    with pytest.raises(ValueError): ce.extract_rows(file(tmp_path, '{}', '.json'), '.json')


def test_offline_does_not_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, 'resolve_ids', lambda *a, **k: pytest.fail('Offline lookup'))
    result = ce.screen_document(file(tmp_path), 'paper.txt', '.txt', offline=True)
    assert result['requests_made'] == 0 and result['summary']['unknown'] == 2


def test_fixed_ids_only_dedup(monkeypatch):
    get = Mock(return_value=response()); monkeypatch.setattr(ce.requests, 'get', get)
    found, _, count = ce.resolve_ids(['1706.03762', '1706.03762'])
    assert count == 1 and len(found) == 1
    args, kwargs = get.call_args
    assert args == (ce.ENDPOINT,) and kwargs['params']['id_list'] == '1706.03762'
    assert kwargs['allow_redirects'] is False and kwargs['stream'] is True


def test_throttle_stops_queue(monkeypatch):
    get = Mock(return_value=response(429)); monkeypatch.setattr(ce.requests, 'get', get)
    ids = [f'2601.{i:05}' for i in range(60)]
    found, reasons, count = ce.resolve_ids(ids)
    assert found == {} and len(reasons) == 60 and count == 1
    assert set(reasons.values()) == {'provider_throttled'}


def test_redirect_not_followed(monkeypatch):
    monkeypatch.setattr(ce.requests, 'get', Mock(return_value=response(302)))
    found, reasons, _ = ce.resolve_ids(['1706.03762'])
    assert not found and reasons['1706.03762'] == 'provider_http_error'


def test_timeout_unknown(monkeypatch):
    monkeypatch.setattr(ce.requests, 'get', Mock(side_effect=ce.requests.Timeout))
    found, reasons, _ = ce.resolve_ids(['1706.03762'])
    assert not found and reasons['1706.03762'] == 'lookup_failed'


def test_size_cap(monkeypatch):
    monkeypatch.setattr(ce, 'MAX_RESPONSE', 5)
    monkeypatch.setattr(ce.requests, 'get', Mock(return_value=response()))
    assert ce.resolve_ids(['1706.03762'])[1]['1706.03762'] == 'lookup_failed'


def test_cache_positive_only(monkeypatch):
    get = Mock(return_value=response()); monkeypatch.setattr(ce.requests, 'get', get)
    ce.resolve_ids(['1706.03762'])
    found, _, count = ce.resolve_ids(['1706.03762'])
    assert count == 0 and found['1706.03762']['cache_hit'] and get.call_count == 1


def test_expired_cache_not_used(monkeypatch):
    ce._cache['1706.03762'] = (ce.time.monotonic() - ce.CACHE_TTL - 1, {'title': 'old'})
    monkeypatch.setattr(ce.requests, 'get', Mock(return_value=response(503)))
    found, reasons, count = ce.resolve_ids(['1706.03762'])
    assert count == 1 and not found and reasons


def test_cancelled_no_network():
    cancel = threading.Event(); cancel.set()
    assert ce.resolve_ids(['1706.03762'], cancel=cancel)[2] == 0


def test_budget_no_network():
    assert ce.resolve_ids(['1706.03762'], budget=0)[2] == 0


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(vh, 'llm_config_error', lambda: 'missing model')
    monkeypatch.setattr(wa, '_run_verification', Mock(side_effect=AssertionError('Full verification reached')))
    return TestClient(wa.app)


def post(client, **fields):
    return client.post('/verify', data=fields, files={'file': ('paper.txt', PAPER.encode(), 'text/plain')})


def test_real_route_no_model_private(client, monkeypatch):
    monkeypatch.setenv('HVE_PRIVATE', '1')
    monkeypatch.setattr(ce.requests, 'get', Mock(return_value=response()))
    r = post(client, verification_mode='citation_existence')
    assert r.status_code == 200
    d = r.json(); assert d['summary']['identifier_exists'] == 1 and d['summary']['unknown'] == 1
    assert d['claim_support_status'] == 'NOT_CHECKED' and d['rows'][0]['bibliography_identity_status'] == 'NOT_CHECKED'
    assert 'claims' not in d
    wa._run_verification.assert_not_called()


def test_real_route_missing_mode_keeps_model_gate(client):
    assert post(client).status_code == 500


def test_real_route_legacy_runs_full(client, monkeypatch):
    monkeypatch.setattr(vh, 'llm_config_error', lambda: None)
    full = Mock(return_value={'legacy': True}); monkeypatch.setattr(wa, '_run_verification', full)
    assert post(client).json() == {'legacy': True} and full.call_count == 1


def test_real_route_unknown_mode(client):
    assert post(client, verification_mode='fast').status_code == 400


@pytest.mark.parametrize('field,value', [('databases', 'arxiv'), ('custom_database', 'https://example.invalid'), ('patient_wait', 'true')])
def test_real_route_no_escalation_parameters(client, field, value):
    assert post(client, verification_mode='citation_existence', **{field:value}).status_code == 400


def test_real_route_offline(client, monkeypatch):
    monkeypatch.setenv('HVE_OFFLINE', '1')
    assert post(client, verification_mode='citation_existence').json()['requests_made'] == 0


def test_real_route_upload_limit(client, monkeypatch):
    monkeypatch.setenv('HVE_MAX_UPLOAD_BYTES', '10')
    assert post(client, verification_mode='citation_existence').status_code == 413


def test_real_route_rate_limit(client, monkeypatch):
    monkeypatch.setattr(wa, '_check_rate_limit', lambda _: False)
    assert post(client, verification_mode='citation_existence').status_code == 429


def test_real_route_cleanup(client, monkeypatch):
    paths = []
    def screen(path, *a, **kw):
        from pathlib import Path
        assert Path(path).exists(); paths.append(path); return {'ok': True}
    monkeypatch.setattr(ce, 'screen_document', screen)
    assert post(client, verification_mode='citation_existence').status_code == 200
    from pathlib import Path
    assert paths and not Path(paths[0]).exists()


def test_real_route_rejects_reference_uploads(client):
    r = client.post('/verify', data={'verification_mode':'citation_existence'}, files=[
        ('file', ('paper.txt', PAPER.encode())), ('references', ('1.txt', b'private source'))])
    assert r.status_code == 400


def test_real_route_bad_json_400(client):
    r = client.post('/verify', data={'verification_mode':'citation_existence'}, files={'file':('paper.json',b'{}')})
    assert r.status_code == 400


def test_real_route_bad_suffix_400(client):
    r = client.post('/verify', data={'verification_mode':'citation_existence'}, files={'file':('paper.exe',b'data')})
    assert r.status_code == 400


def test_identifier_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, 'MAX_IDS', 1)
    calls=[]
    monkeypatch.setattr(ce, 'resolve_ids', lambda ids, **kwargs: (calls.append(ids) or {}, {}, 0))
    p=file(tmp_path, PAPER + '[3] Other arXiv:1810.04805\n')
    result=ce.screen_document(p, 'paper.txt', '.txt')
    assert len(calls[0]) == 1 and result['summary']['unknown'] == 3
    assert result['unknown_reasons']['identifier_limit'] == 2


def test_html_script_not_screened(tmp_path):
    p=file(tmp_path, '<script>References\n[1] arXiv:1706.03762</script><p>Content</p>', '.html')
    assert ce.extract_rows(p, '.html')[0] == []


def test_pdf_text_path(tmp_path):
    import fitz
    p=tmp_path/'paper.pdf'
    with fitz.open() as doc:
        page=doc.new_page();page.insert_text((40,50), PAPER);doc.save(p)
    rows, warnings=ce.extract_rows(str(p), '.pdf')
    assert len(rows)==2 and rows[0]['arxiv_id']=='1706.03762v7'


def test_partial_result_kept_on_later_failure(monkeypatch):
    monkeypatch.setattr(ce.requests, 'get', Mock(side_effect=[response(),response(503)]))
    # Use a fake monotonic clock that advances beyond the pacing interval.
    clock=iter(range(10000))
    monkeypatch.setattr(ce.time, 'monotonic', lambda: next(clock))
    ids=['1706.03762']+[f'2601.{i:05}' for i in range(55)]
    found, reasons, calls=ce.resolve_ids(ids,budget=1000)
    assert calls==2 and '1706.03762' in found and reasons
