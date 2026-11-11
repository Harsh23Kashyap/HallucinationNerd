"""Coalesce identical fetch inputs within one resolution, not cache-key collisions."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
import citation_resolver as cr

@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    monkeypatch.setattr(cr,'_source_cache',{})

@pytest.mark.parametrize('patient',[False,True])
@pytest.mark.parametrize('kind,value',[('arxiv_id','1234.56789'),('doi','10.1000/fixture'),('pmid','12345'),('url','https://example.test/paper')])
def test_identical_inputs_fetch_once_and_keep_each_reference(monkeypatch,patient,kind,value):
    info={kind:value,'title':'A shared fixture paper'}
    monkeypatch.setattr(cr,'resolve_references',lambda _: {'1':dict(info),'2':dict(info)})
    calls=[]
    monkeypatch.setattr(cr,'_fetch_with_policy',lambda info,p: calls.append(p) or ('source content',False))
    result=cr.resolve_and_fetch_all('fixture',['1','2'],patient=patient)
    assert result=={'1':'source content','2':'source content'}
    assert calls==[patient]
    assert result.throttled_refs==[]

@pytest.mark.parametrize('outcome',[(None,True),(None,False),('',False)])
def test_failed_identical_inputs_share_outcome_but_are_not_cached(monkeypatch,outcome):
    info={'doi':'10.1000/fixture'}
    monkeypatch.setattr(cr,'resolve_references',lambda _: {'1':dict(info),'2':dict(info)})
    calls=[]
    monkeypatch.setattr(cr,'_fetch_with_policy',lambda *_:calls.append(1) or outcome)
    result=cr.resolve_and_fetch_all('fixture',['1','2'])
    assert result=={'1':outcome[0],'2':outcome[0]}
    assert sorted(result.throttled_refs)==(['1','2'] if outcome[1] else [])
    assert len(calls)==1
    assert cr._source_cache=={}
    cr.resolve_and_fetch_all('fixture',['1','2'])
    assert len(calls)==2


def test_exception_is_shared_without_negative_caching(monkeypatch):
    monkeypatch.setattr(cr,'resolve_references',lambda _: {'1':{'doi':'same'},'2':{'doi':'same'}})
    calls=[]
    def fail(*_):
        calls.append(1)
        raise RuntimeError('fixture unavailable')
    monkeypatch.setattr(cr,'_fetch_with_policy',fail)
    assert cr.resolve_and_fetch_all('fixture',['1','2'])=={'1':None,'2':None}
    assert len(calls)==1
    assert not cr._source_cache


def test_different_fallback_inputs_are_not_coalesced_by_same_doi(monkeypatch):
    monkeypatch.setattr(cr,'resolve_references',lambda _: {'1':{'doi':'same','url':'https://example.test/A'},'2':{'doi':'same','url':'https://example.test/B'}})
    calls=[]
    monkeypatch.setattr(cr,'_fetch_with_policy',lambda info,_:calls.append(info['url']) or (info['url'],False))
    result=cr.resolve_and_fetch_all('fixture',['1','2'])
    assert len(calls)==2
    assert result['1']!=result['2']


def test_success_cache_still_reused_in_later_resolution(monkeypatch):
    info={'pmid':'fixture'}
    monkeypatch.setattr(cr,'resolve_references',lambda _: {'1':info,'2':dict(info)})
    calls=[]
    monkeypatch.setattr(cr,'_fetch_with_policy',lambda *_:calls.append(1) or ('content',False))
    cr.resolve_and_fetch_all('fixture',['1','2'])
    result=cr.resolve_and_fetch_all('fixture',['1','2'])
    assert result=={'1':'content','2':'content'}
    assert len(calls)==1


def test_unresolved_key_is_not_filled_from_another_input(monkeypatch):
    monkeypatch.setattr(cr,'resolve_references',lambda _: {'1':{'pmid':'fixture'},'2':{'pmid':'fixture'}})
    calls=[]
    monkeypatch.setattr(cr,'_fetch_with_policy',lambda *_:calls.append(1) or ('content',False))
    result=cr.resolve_and_fetch_all('fixture',['1','2','99'])
    assert result=={'1':'content','2':'content','99':None}
    assert len(calls)==1


def test_distinct_identical_inputs_can_use_author_year_alias(monkeypatch):
    monkeypatch.setattr(cr,'resolve_references',lambda _: {'author2020':{'pmid':'fixture'}})
    calls=[]
    monkeypatch.setattr(cr,'_fetch_with_policy',lambda *_:calls.append(1) or ('content',False))
    assert cr.resolve_and_fetch_all('fixture',['author2020','author2020a'])=={'author2020':'content','author2020a':'content'}
    assert len(calls)==1
