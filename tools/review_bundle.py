#!/usr/bin/env python3
"""Build/check a local portable JSON citation-review bundle, without API calls."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

FORMAT = 'hn-review-bundle-1'
LIMITS = [
    'Hashes detect changes against supplied digests, not authenticity or provenance.',
    'Source metadata and verdicts are copied assertions, not verified retrieval or truth.',
    'No proof of actual fetched URL, fetched content, model execution or billing.',
    'Bundle includes private document text and source details. Review before sharing.',
]


class BundleError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def parse(raw):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj: raise BundleError('duplicate JSON key')
            obj[key] = value
        return obj
    def constant(_): raise BundleError('nonfinite JSON literal')
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    canonical(value)  # also reject exponent-overflow floats in unused fields
    return value


def label(value):
    if type(value) is int and value > 0: return str(value)
    if isinstance(value,str) and value.strip() and value == value.strip(): return value
    raise BundleError('invalid reference label')


def linkage(report):
    """Validate the supported /verify report shape and derive label relationships."""
    if not isinstance(report,dict) or not isinstance(report.get('claims'),list):
        raise BundleError('expected report with claims array')
    claims = report['claims']; sources = report.get('source_map')
    summary = report.get('summary')
    if not isinstance(sources,dict) or not isinstance(summary,dict):
        raise BundleError('expected source_map and summary objects')
    if type(summary.get('total_claims')) is not int or summary['total_claims'] != len(claims):
        raise BundleError('total_claims mismatch')
    for key, source in sources.items():
        label(key)
        if not isinstance(source,dict): raise BundleError('invalid source entry')
        if any(k in source and not isinstance(source[k],str) for k in ('title','url','doi')):
            raise BundleError('source metadata must be text')
    links = {key:[] for key in sources}; missing = {}; uncited = []
    for index, claim in enumerate(claims,1):
        if not isinstance(claim,dict) or not isinstance(claim.get('claim'),str) or not claim['claim'].strip():
            raise BundleError('invalid claim text')
        if not isinstance(claim.get('verdict'),str) or not claim['verdict'].strip():
            raise BundleError('invalid verdict')
        confidence = claim.get('confidence')
        if isinstance(confidence,bool) or not isinstance(confidence,(int,float)):
            raise BundleError('invalid confidence')
        try: finite = math.isfinite(confidence)
        except OverflowError: finite = False
        if not finite or not 0 <= confidence <= 1: raise BundleError('invalid confidence')
        refs = claim.get('cited_refs')
        if not isinstance(refs,list): raise BundleError('cited_refs must be array')
        refs = sorted({label(ref) for ref in refs})
        if not refs: uncited.append(index)
        for ref in refs:
            (links if ref in links else missing).setdefault(ref,[]).append(index)
    return {'source_to_claims':dict(sorted(links.items())),
            'labels_without_metadata':dict(sorted(missing.items())),
            'uncited_claims':uncited,
            'unused_source_labels':sorted(k for k,v in links.items() if not v),
            'note':'Labels are document-local; missing metadata is recorded, not invented.'}


def build(raw):
    report = parse(raw)
    relation = linkage(report)
    # JSON round-trip gives a detached copy with the exact accepted JSON values.
    payload = {'report':report, 'source_linkage':relation}
    return {'format':FORMAT,'payload':payload,
            'manifest':{'original_bytes_sha256':hashlib.sha256(raw).hexdigest(),
                        'canonical_report_sha256':digest(report),
                        'canonical_payload_sha256':digest(payload)},
            'limits':list(LIMITS)}


def check(bundle, original=None):
    if not isinstance(bundle,dict) or set(bundle) != {'format','payload','manifest','limits'} or bundle['format'] != FORMAT:
        raise BundleError('unsupported bundle structure')
    payload = bundle['payload']; manifest = bundle['manifest']
    if not isinstance(payload,dict) or set(payload) != {'report','source_linkage'}:
        raise BundleError('invalid payload')
    if not isinstance(manifest,dict) or set(manifest) != {'original_bytes_sha256','canonical_report_sha256','canonical_payload_sha256'}:
        raise BundleError('invalid manifest')
    if any(not isinstance(h,str) or len(h)!=64 or any(c not in '0123456789abcdef' for c in h) for h in manifest.values()):
        raise BundleError('invalid digest')
    if bundle['limits'] != LIMITS: raise BundleError('missing or changed limits')
    if linkage(payload['report']) != payload['source_linkage']: raise BundleError('linkage mismatch')
    if digest(payload['report']) != manifest['canonical_report_sha256'] or digest(payload) != manifest['canonical_payload_sha256']:
        raise BundleError('digest mismatch')
    if original is not None:
        if hashlib.sha256(original).hexdigest() != manifest['original_bytes_sha256']:
            raise BundleError('original byte hash mismatch')
        if canonical(parse(original)) != canonical(payload['report']): raise BundleError('original report mismatch')
    return {'integrity_consistent':True,'original_bytes_checked':original is not None,
            'authenticity_verified':False,'retrieval_verified':False,
            'claim_count':len(payload['report']['claims'])}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action',required=True)
    b = sub.add_parser('build'); b.add_argument('--report',type=Path,required=True);b.add_argument('--output',type=Path,required=True)
    c = sub.add_parser('check');c.add_argument('--bundle',type=Path,required=True);c.add_argument('--original-report',type=Path)
    args=p.parse_args(argv)
    try:
        if args.action=='build':
            if args.output.resolve()==args.report.resolve():raise BundleError('cannot overwrite report')
            bundle=build(args.report.read_bytes());raw=canonical(bundle)+b'\n'
            with tempfile.NamedTemporaryFile('wb',dir=args.output.parent,delete=False) as f:
                temporary=Path(f.name);f.write(raw)
            try:os.replace(temporary,args.output)
            finally:temporary.unlink(missing_ok=True)
            print('Bundle written. Includes private report text; review before sharing.')
        else:
            result=check(parse(args.bundle.read_bytes()),args.original_report.read_bytes() if args.original_report else None)
            print(json.dumps(result,sort_keys=True))
    except (OSError,ValueError,TypeError,UnicodeError):
        print('Invalid report, bundle integrity mismatch or I/O failure. No source was fetched.')
        return 2
    return 0


if __name__=='__main__':raise SystemExit(main())
