"""Offline asynchronous HTTP deadline and cancellation probes."""
import asyncio
import time
import httpx
import pytest
from orbuz.runtime.model import ChatModel


@pytest.mark.parametrize('cancelled', [False, True])
def test_http_request_stops_at_remaining_deadline_or_cancel(monkeypatch, cancelled):
    monkeypatch.setenv('ORBUZ_TEST_KEY','test-only')
    model=ChatModel('test','https://example.invalid','ORBUZ_TEST_KEY')
    stopped=[]
    async def handler(request):
        try:
            await asyncio.sleep(10)
        finally:
            stopped.append(True)
        return httpx.Response(200,json={})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw: original(transport=httpx.MockTransport(handler),**kw))
    start=time.monotonic()
    try:
        with pytest.raises(TimeoutError if not cancelled else InterruptedError):
            model.complete_bounded([],[],32,remaining=.15 if not cancelled else 5,
                                   cancel=lambda: cancelled and time.monotonic()-start>.1)
        assert time.monotonic()-start < .8
        assert stopped==[True]
    finally:
        model.close()


def test_actual_local_socket_request_is_aborted(monkeypatch):
    import socket
    import threading
    monkeypatch.setenv('ORBUZ_TEST_KEY','test-only')
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
    listener.settimeout(3)
    received=[];closed=[]
    def server():
        try:
            conn,_=listener.accept()
            with conn:
                conn.settimeout(3)
                data=b''
                while b'\r\n\r\n' not in data: data+=conn.recv(65536)
                received.append(True)
                # Do not respond; observe the client closing the socket on deadline.
                while conn.recv(65536): pass
                closed.append(True)
        finally:listener.close()
    worker=threading.Thread(target=server,daemon=True);worker.start()
    model=ChatModel('test','https://example.invalid','ORBUZ_TEST_KEY')
    model.client.base_url=f'http://127.0.0.1:{listener.getsockname()[1]}/'
    try:
        with pytest.raises(TimeoutError):
            model.complete_bounded([],[],32,remaining=.3,cancel=lambda:False)
    finally:model.close();worker.join(timeout=4)
    assert received==[True] and closed==[True]
    assert not worker.is_alive()


def test_bounded_request_retains_usage_and_schema_validation(monkeypatch):
    monkeypatch.setenv('ORBUZ_TEST_KEY','test-only')
    model=ChatModel('test','https://example.invalid','ORBUZ_TEST_KEY')
    requests=[]
    async def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'ok'}}],
                                       'usage':{'prompt_tokens':12},'model':'actual-model'})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw: original(transport=httpx.MockTransport(handler),**kw))
    try:
        result=model.complete_bounded([],[],32,remaining=2,cancel=lambda:False)
        assert result['usage']=={'prompt_tokens':12}
        assert result['model']=='actual-model'
        assert requests[0].url.path=='/chat/completions'
    finally:model.close()
