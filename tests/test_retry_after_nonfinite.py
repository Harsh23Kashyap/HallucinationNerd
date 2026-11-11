"""Malformed provider headers cannot turn a bounded retry into invalid sleep."""
import math
import os
import sys
import pytest
sys.path.insert(0,os.path.join(os.path.dirname(os.path.dirname(__file__)),'web'))
import citation_resolver as cr

class Response:
    def __init__(self,header,status=429):self.headers={'Retry-After':header};self.status_code=status

@pytest.mark.parametrize('raw',['NaN','nan','+nan','-nan','Infinity','-Infinity','inf','-inf','1e400','-1e400'])
def test_nonfinite_retry_after_uses_existing_fallback(raw):
    delay=cr._retry_delay(Response(raw),1)
    assert math.isfinite(delay)
    assert delay==1.2

@pytest.mark.parametrize('raw,expected',[('0',0),('-2',0),('1.25',1.25),('10',5),('',1.2),(None,1.2)])
def test_valid_and_existing_malformed_headers_keep_bounds(raw,expected):
    assert cr._retry_delay(Response(raw),1)==expected


def test_nan_header_still_allows_last_permitted_attempt(monkeypatch):
    calls=[];sleeps=[]
    monkeypatch.setattr(cr._request_policy,'attempts',2,raising=False)
    monkeypatch.setattr(cr,'_rate_limit',lambda:None)
    def get(*args,**kwargs):
        calls.append(1)
        return Response('NaN',429 if len(calls)==1 else 200)
    def sleep(delay):
        if not math.isfinite(delay):raise ValueError('invalid sleep duration')
        sleeps.append(delay)
    monkeypatch.setattr(cr.requests,'get',get);monkeypatch.setattr(cr._time,'sleep',sleep)
    assert cr._get_with_backoff('https://fixture.invalid').status_code==200
    assert len(calls)==2
    assert sleeps==[0.6]
