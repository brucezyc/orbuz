"""Request boundary integration: no API calls, real task lifecycle."""
import time
import pytest
from test_evidence_runtime import project, contract
from orbuz.runtime.engine import Runtime


def test_context_renewal_is_protocol_valid_and_bounded():
    from orbuz.runtime.request_budget import bounded_context, request_size
    history=[{'role':'assistant','tool_calls':[{'id':'old'}]},
             {'role':'tool','tool_call_id':'old','content':'中'*50000}]
    messages=bounded_context(history,[],lambda:[{'role':'system','content':'stable'}, {'role':'user','content':'contract'}])
    assert request_size(messages,[])<=100000
    assert not any(m['role']=='tool' for m in messages)
    assert messages[0]['content']=='stable'


def test_runtime_uses_remaining_deadline_and_preserves_cancel_status(project, tmp_path):
    rt=Runtime(tmp_path/'state')
    task_id=rt.create(contract(project))
    class Bounded:
        def complete(self,*args): raise AssertionError('unbounded adapter used')
        def complete_bounded(self,*args,remaining,cancel):
            assert 0 < remaining <= 180
            rt.cancel(task_id)
            assert cancel()
            raise InterruptedError('cancelled')
    result=rt.run(task_id,Bounded())
    assert result['status']=='cancelled'
    assert result['calls']==1


def test_runtime_http_deadline_becomes_exhausted(project,tmp_path):
    rt=Runtime(tmp_path/'state');task_id=rt.create(contract(project))
    class Bounded:
        def complete(self,*args): raise AssertionError('unbounded adapter used')
        def complete_bounded(self,*args,remaining,cancel): raise TimeoutError('deadline')
    result=rt.run(task_id,Bounded())
    assert result['status']=='exhausted'


def test_runtime_real_chat_transport_deadline(project,tmp_path,monkeypatch):
    import asyncio
    import httpx
    from orbuz.runtime.model import ChatModel
    monkeypatch.setenv('ORBUZ_TEST_KEY','test-only')
    rt=Runtime(tmp_path/'state');spec=contract(project);spec['max_seconds']=.3
    task_id=rt.create(spec)
    stopped=[]
    async def handler(request):
        try:await asyncio.sleep(10)
        finally:stopped.append(True)
        return httpx.Response(200,json={})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    model=ChatModel('test','https://example.invalid','ORBUZ_TEST_KEY')
    start=time.monotonic()
    try:result=rt.run(task_id,model)
    finally:model.close()
    assert result['status']=='exhausted'
    assert result['calls']==1 and not result['usage']
    assert time.monotonic()-start < 1
    assert stopped==[True]


def test_utf8_request_budget_counts_tools_and_non_ascii(project,tmp_path):
    import json
    rt=Runtime(tmp_path/'state');spec=contract(project)
    spec['goal']='调查'*8000
    task_id=rt.create(spec)
    class Inspect:
        def complete(self,messages,tools,max_tokens):
            assert len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False).encode()) <= 100000
            return {'message':{'role':'assistant','content':None,'tool_calls':[{
                'id':'stop','type':'function','function':{'name':'blocked','arguments':'{"reason":"checked"}'}}]}}
    result=rt.run(task_id,Inspect())
    assert result['status']=='blocked'


def test_oversized_initial_request_never_reaches_model(project,tmp_path):
    rt=Runtime(tmp_path/'state');spec=contract(project)
    spec['acceptance']=['/usr/bin/python3','-c','x=1\n'+'#'*150000]
    class NoRequest:
        calls=0
        def complete(self,*args):
            self.calls+=1
            raise AssertionError('oversized request reached model')
    model=NoRequest()
    try:
        task_id=rt.create(spec)
    except ValueError:
        return
    result=rt.run(task_id,model)
    assert result['status']=='failed'
    assert result['calls']==0
    assert model.calls==0
    assert 'budget' in result['error'].lower()
