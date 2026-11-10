"""Mode activation must affect the actual client, not just the new configuration.

No model calls: fake hosted/local clients record the llm_call routing decision.
"""
from types import SimpleNamespace
import pytest
import verify_hallucinations as vh

class Client:
    def __init__(self, base_url):
        self.base_url = base_url
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='local-fixture'))])

def setup(monkeypatch, mode, value, cached_url):
    for key in ('HVE_PRIVATE', 'HVE_OFFLINE', 'HVE_LLM_BASE_URL', 'VERIFICATION_MODEL', 'OPENAI_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(mode, value)
    monkeypatch.setenv('HVE_LLM_BASE_URL', 'http://127.0.0.1:11434/v1')
    monkeypatch.setenv('VERIFICATION_MODEL', 'offline-fixture')
    hosted = Client(cached_url)
    created = []
    def factory(**kwargs):
        client = Client(kwargs['base_url'])
        created.append(client)
        return client
    monkeypatch.setattr(vh, 'client', hosted)
    monkeypatch.setattr(vh, 'OpenAI', factory)
    monkeypatch.setattr(vh.time, 'sleep', lambda _: None)
    return hosted, created

@pytest.mark.parametrize('value', ['1', 'true', 'yes'])
@pytest.mark.parametrize('cached_url', ['https://api.openai.com/v1', 'https://example.invalid/v1', 'http://192.0.2.1/v1', 'https://127.0.0.1.example.invalid/v1'])
def test_private_activation_routes_llm_call_to_local(monkeypatch, value, cached_url):
    hosted, created = setup(monkeypatch, 'HVE_PRIVATE', value, cached_url)
    assert vh.llm_config_error() is None
    assert vh.llm_call('fixture-system', 'fixture-document') == 'local-fixture'
    assert hosted.calls == []
    assert len(created) == 1
    assert created[0].calls[0]['messages'][1]['content'] == 'fixture-document'
    assert vh.get_client() is created[0]
    assert len(created) == 1

@pytest.mark.parametrize('value', ['1', 'true', 'yes'])
def test_offline_still_replaces_hosted_client(monkeypatch, value):
    hosted, created = setup(monkeypatch, 'HVE_OFFLINE', value, 'https://api.openai.com/v1')
    vh.llm_call('system', 'document')
    assert hosted.calls == [] and len(created) == 1

@pytest.mark.parametrize('mode', ['HVE_PRIVATE', 'HVE_OFFLINE'])
@pytest.mark.parametrize('url', ['http://127.0.0.1:11434/v1', 'http://localhost:11434/v1', 'http://[::1]:11434/v1'])
def test_local_cached_client_is_reused(monkeypatch, mode, url):
    cached, created = setup(monkeypatch, mode, '1', url)
    assert vh.get_client() is cached
    assert created == []

@pytest.mark.parametrize('mode', ['HVE_PRIVATE', 'HVE_OFFLINE'])
def test_invalid_private_settings_fail_before_cached_client_use(monkeypatch, mode):
    cached, created = setup(monkeypatch, mode, '1', 'https://api.openai.com/v1')
    monkeypatch.setenv('HVE_LLM_BASE_URL', 'https://example.invalid/v1')
    with pytest.raises(ValueError): vh.llm_call('system', 'document')
    assert cached.calls == [] and created == []
