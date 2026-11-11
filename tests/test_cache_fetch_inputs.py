"""Successful fallback content must not be reused for different fetch inputs."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
import citation_resolver as cr

@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(cr,'_source_cache',{})

@pytest.mark.parametrize('identifier,value',[('doi','10.1000/fixture'),('arxiv_id','1234.56789'),('pmid','12345')])
@pytest.mark.parametrize('fallback',['url','title','raw','other_fetch_field'])
def test_different_successful_fallback_does_not_reuse_other_reference(monkeypatch,identifier,value,fallback):
    field = 'pmid' if identifier != 'pmid' else 'url'
    if fallback != 'other_fetch_field':field=fallback
    a={identifier:value,field:'fixtureA'}; b={identifier:value,field:'fixtureB'}
    if field == 'url':
        a[field]='https://example.test/A';b[field]='https://example.test/B'
    active={'1':a};calls=[]
    monkeypatch.setattr(cr,'resolve_references',lambda _:active)
    monkeypatch.setattr(cr,'_fetch_with_policy',lambda info,p:(calls.append(dict(info)) or info[field],False))
    first=cr.resolve_and_fetch_all('one',['1'])
    active['1']=b
    second=cr.resolve_and_fetch_all('two',['1'])
    assert first['1']==a[field]
    assert second['1']==b[field]
    assert len(calls)==2
    # Each variant can still reuse its own content.
    active['1']=a
    assert cr.resolve_and_fetch_all('three',['1'])['1']==a[field]
    assert len(calls)==2

@pytest.mark.parametrize('identifier,value',[('doi','10.1000/fixture'),('arxiv_id','1234.56789'),('pmid','12345'),('url','https://example.test/A')])
def test_identical_fetch_inputs_still_reuse_success(monkeypatch,identifier,value):
    info={identifier:value,'title':'fixture title','raw':'fixture raw'}
    active={'1':info};calls=[]
    monkeypatch.setattr(cr,'resolve_references',lambda _:active)
    monkeypatch.setattr(cr,'_fetch_with_policy',lambda *_:(calls.append(1) or 'content',False))
    assert cr.resolve_and_fetch_all('one',['1'])=={'1':'content'}
    active.clear();active['2']=dict(reversed(list(info.items())))
    assert cr.resolve_and_fetch_all('two',['2'])=={'2':'content'}
    assert len(calls)==1

@pytest.mark.parametrize('identifier,value',[('doi','10.1000/fixture'),('arxiv_id','1234.56789'),('pmid','12345')])
def test_actual_fetch_fallback_path_remains_scoped(monkeypatch,identifier,value):
    active={'1':{identifier:value,'url':'https://example.test/A'}};calls=[]
    monkeypatch.setattr(cr,'resolve_references',lambda _:active)
    monkeypatch.setattr(cr,'_fetch_doi',lambda _:None)
    monkeypatch.setattr(cr,'_fetch_arxiv',lambda _:None)
    monkeypatch.setattr(cr,'_fetch_pubmed_abstract',lambda _:None)
    monkeypatch.setattr(cr,'_fetch_url_safe',lambda url:calls.append(url) or ('source:'+url))
    assert cr.resolve_and_fetch_all('one',['1'])['1']=='source:https://example.test/A'
    active['1']={identifier:value,'url':'https://example.test/B'}
    assert cr.resolve_and_fetch_all('two',['1'])['1']=='source:https://example.test/B'
    assert calls==['https://example.test/A','https://example.test/B']


def test_changed_fetch_inputs_do_not_share_negative_outcomes(monkeypatch):
    active={'1':{'doi':'10.1000/fixture','title':'one'}};calls=[]
    monkeypatch.setattr(cr,'resolve_references',lambda _:active)
    monkeypatch.setattr(cr,'_fetch_with_policy',lambda *_:(calls.append(1) or None,True))
    first=cr.resolve_and_fetch_all('one',['1'])
    active['1']['title']='two'
    second=cr.resolve_and_fetch_all('two',['1'])
    assert first['1'] is None and second['1'] is None
    assert first.throttled_refs==second.throttled_refs==['1']
    assert len(calls)==2 and not cr._source_cache
