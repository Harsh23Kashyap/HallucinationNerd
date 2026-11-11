import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'tools/review_bundle.py'
spec=importlib.util.spec_from_file_location('bundle',SCRIPT);v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)


def report():
    return {'filename':'fictional.pdf','summary':{'total_claims':3},
            'source_map':{'1':{'title':'Fictional source','url':'https://example.invalid/paper','doi':''},'unused':{}},
            'claims':[{'claim':'One','verdict':'SUPPORTED','confidence':0.7,'cited_refs':[1,'1']},
                      {'claim':'Two','verdict':'INACCESSIBLE','confidence':0.0,'cited_refs':['Smith2020']},
                      {'claim':'Three','verdict':'NO_BACKUP_FOUND','confidence':0,'cited_refs':[]}]}


def raw():return json.dumps(report()).encode()


def test_roundtrip_detached():
    source=report();b=v.build(json.dumps(source).encode());before=copy.deepcopy(b)
    source['claims'][0]['claim']='Changed'
    assert b==before
    r=v.check(b)
    assert r=={'integrity_consistent':True,'original_bytes_checked':False,'authenticity_verified':False,'retrieval_verified':False,'claim_count':3}
    assert v.check(b,raw())['original_bytes_checked']


def test_links():
    b=v.build(raw());link=b['payload']['source_linkage']
    assert link['source_to_claims']=={'1':[1],'unused':[]}
    assert link['labels_without_metadata']=={'Smith2020':[2]}
    assert link['uncited_claims']==[3] and link['unused_source_labels']==['unused']


def test_original_whitespace_is_distinct():
    b=v.build(raw())
    with pytest.raises(v.BundleError):v.check(b,json.dumps(report(),indent=2).encode())


def test_canonical_order_and_preservation():
    r=report();r['claims'][0]['extra']={'evidence_quote':'private quote','unknown':['preserved']}
    b=v.build(json.dumps(r).encode())
    assert b['payload']['report']==r
    assert v.digest(r)==v.digest(dict(reversed(list(r.items()))))


@pytest.mark.parametrize('change',[
    lambda b:b['payload']['report']['claims'][0].update(claim='altered'),
    lambda b:b['payload']['source_linkage'].update(uncited_claims=[]),
    lambda b:b['manifest'].update(canonical_report_sha256='0'*64),
    lambda b:b['manifest'].update(canonical_payload_sha256='0'*64),
    lambda b:b.update(limits=[]),lambda b:b.update(format='future'),
    lambda b:b.update(extra=True),lambda b:b['payload'].update(extra=True),
    lambda b:b['manifest'].update(extra='0'*64),
    lambda b:b['manifest'].update(original_bytes_sha256='bad'),
])
def test_tamper_rejected(change):
    b=v.build(raw());change(b)
    with pytest.raises(v.BundleError):v.check(b)


def test_rehash_is_not_authentication():
    b=v.build(raw());b['payload']['report']['claims'][0]['claim']='Changed assertion'
    b['manifest']['canonical_report_sha256']=v.digest(b['payload']['report'])
    b['manifest']['canonical_payload_sha256']=v.digest(b['payload'])
    assert not v.check(b)['authenticity_verified']
    with pytest.raises(v.BundleError):v.check(b,raw())


@pytest.mark.parametrize('change',[
    lambda r:r.update(claims=None),lambda r:r.update(summary={}),
    lambda r:r['summary'].update(total_claims=True),lambda r:r['summary'].update(total_claims=4),
    lambda r:r.update(source_map=[]),lambda r:r['source_map'].update({'1':None}),
    lambda r:r['source_map']['1'].update(url=23),
    lambda r:r['claims'][0].update(claim=''),lambda r:r['claims'][0].update(verdict=None),
    lambda r:r['claims'][0].update(confidence=True),lambda r:r['claims'][0].update(confidence=-.1),
    lambda r:r['claims'][0].update(confidence=1.1),lambda r:r['claims'][0].update(confidence=10**400),
    lambda r:r['claims'][0].update(cited_refs='1'),lambda r:r['claims'][0].update(cited_refs=[False]),
    lambda r:r['claims'][0].update(cited_refs=[{}]),lambda r:r['claims'][0].update(cited_refs=[0]),
    lambda r:r['claims'][0].update(cited_refs=[' ']),
])
def test_bad_report(change):
    r=report();change(r)
    with pytest.raises(ValueError):v.build(json.dumps(r).encode())


@pytest.mark.parametrize('data',[b'{"claims":[],"claims":[]}',b'NaN',b'{"extra":1e999}',b'bad',b'[]'])
def test_bad_json(data):
    with pytest.raises(ValueError):v.build(data)


def test_empty_report():
    r={'claims':[],'source_map':{},'summary':{'total_claims':0}}
    assert v.check(v.build(json.dumps(r).encode()))['claim_count']==0


def test_cli_and_preserve_output(tmp_path):
    source=tmp_path/'report.json';bundle=tmp_path/'bundle.json';source.write_bytes(raw())
    args=[sys.executable,str(SCRIPT)]
    assert subprocess.run(args+['build','--report',str(source),'--output',str(bundle)],capture_output=True).returncode==0
    assert subprocess.run(args+['check','--bundle',str(bundle),'--original-report',str(source)],capture_output=True).returncode==0
    before=bundle.read_bytes();source.write_text('bad')
    assert subprocess.run(args+['build','--report',str(source),'--output',str(bundle)],capture_output=True).returncode==2
    assert bundle.read_bytes()==before
    assert subprocess.run(args+['check','--bundle',str(bundle),'--original-report',str(source)],capture_output=True).returncode==2
    assert subprocess.run(args+['build','--report',str(source),'--output',str(source)],capture_output=True).returncode==2
