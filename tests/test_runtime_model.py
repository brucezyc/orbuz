"""Transport failure tests use httpx MockTransport, never call a paid API."""
import httpx
import pytest
from orbuz.runtime.model import ChatModel


def model_with(monkeypatch, handler):
    monkeypatch.setenv('ORBUZ_TEST_KEY', 'test-only-not-a-real-key')
    model = ChatModel('test', 'https://example.invalid', 'ORBUZ_TEST_KEY')
    model.client.close()
    model.client = httpx.Client(transport=httpx.MockTransport(handler), base_url='https://example.invalid/')
    return model


def test_transport_error_not_mock_success(monkeypatch):
    m = model_with(monkeypatch, lambda r: httpx.Response(503, text='private server details'))
    with pytest.raises(RuntimeError, match='Model HTTP status 503'):
        m.complete([], [], 100)
    m.close()


def test_truncated_tool_call_not_executed(monkeypatch):
    m = model_with(monkeypatch, lambda r: httpx.Response(200, json={
        'choices': [{'finish_reason': 'length', 'message': {'role': 'assistant', 'tool_calls': []}}]}))
    with pytest.raises(RuntimeError, match='truncated'):
        m.complete([], [], 100)
    m.close()


def test_model_usage_is_reported_without_fabrication(monkeypatch):
    m = model_with(monkeypatch, lambda r: httpx.Response(200, json={
        'model': 'actual-server-model', 'usage': {'prompt_tokens': 7, 'completion_tokens': 2},
        'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'hello'}}]}))
    result = m.complete([], [], 100)
    assert result['usage'] == {'prompt_tokens': 7, 'completion_tokens': 2}
    assert result['model'] == 'actual-server-model'
    m.close()
