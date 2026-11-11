"""Controlled real-thread interleavings of the process-local source cache.

Pause only the reader's ordinary dict iteration. The writer enters the real
resolver rather than touching the dict directly. No network or model calls.
"""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
import citation_resolver as cr

@pytest.mark.parametrize('size', [2, 4, 16, 32])
@pytest.mark.parametrize('mode', ['insert', 'expire', 'mixed'])
def test_cache_gc_survives_concurrent_resolution(monkeypatch, size, mode):
    iterating = threading.Event()
    writer_started = threading.Event()
    class PausedDict(dict):
        def items(self):
            iterator = iter(super().items())
            for position, item in enumerate(iterator):
                yield item
                if position == 0 and threading.current_thread().name == 'gc-reader':
                    iterating.set()
                    assert writer_started.wait(2)
                    # Give the competing resolver a chance to mutate. A fixed
                    # writer must wait for the cache lock rather than mutate.
                    time.sleep(0.05)
    now = time.time()
    cache = PausedDict({('seed', i): (now - (100 if mode == 'expire' or (mode == 'mixed' and i % 2) else 0), 'seed') for i in range(size)})
    monkeypatch.setattr(cr, '_source_cache', cache)
    monkeypatch.setenv('HVE_CACHE_TTL', '50')
    monkeypatch.setattr(cr, 'resolve_references', lambda text: {'1': {'doi': 'fixture:' + text}})
    calls = []
    calls_lock = threading.Lock()
    def fetch(info, patient):
        with calls_lock:
            calls.append(info['doi'])
        return 'content:' + info['doi'], False
    monkeypatch.setattr(cr, '_fetch_with_policy', fetch)
    def reader():
        threading.current_thread().name = 'gc-reader'
        return cr.resolve_and_fetch_all('reader', ['1'])
    def writer():
        assert iterating.wait(2)
        writer_started.set()
        return cr.resolve_and_fetch_all('writer', ['1'])
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(reader)
        second = executor.submit(writer)
        actual = [first.result(timeout=5), second.result(timeout=5)]
    assert actual == [{'1': 'content:fixture:reader'}, {'1': 'content:fixture:writer'}]
    assert sorted(calls) == ['fixture:reader', 'fixture:writer']
    assert all(key[0] != 'seed' or cache[key][0] > now - 50 for key in cache)
    assert cr.resolve_and_fetch_all('reader', ['1']) == actual[0]
    assert cr.resolve_and_fetch_all('writer', ['1']) == actual[1]
    assert len(calls) == 2

@pytest.mark.parametrize('outcome', [('content', False), (None, True), (None, False), ('', False)])
def test_cache_lock_keeps_success_and_failure_policy(monkeypatch, outcome):
    monkeypatch.setattr(cr, '_source_cache', {})
    monkeypatch.setattr(cr, 'resolve_references', lambda _: {'1': {'doi': 'fixture'}})
    calls = []
    monkeypatch.setattr(cr, '_fetch_with_policy', lambda *_: calls.append(1) or outcome)
    first = cr.resolve_and_fetch_all('fixture', ['1'])
    second = cr.resolve_and_fetch_all('fixture', ['1'])
    assert first == second == {'1': outcome[0]}
    assert first.throttled_refs == (['1'] if outcome[1] else [])
    assert len(calls) == (1 if outcome[0] else 2)
    assert bool(cr._source_cache) == bool(outcome[0])

@pytest.mark.parametrize('patient', [False, True])
@pytest.mark.parametrize('same_input', [False, True])
def test_cache_lock_does_not_cover_fetch_wait(monkeypatch, patient, same_input):
    monkeypatch.setattr(cr, '_source_cache', {})
    monkeypatch.setattr(cr, 'resolve_references', lambda text: {'1': {'doi': 'fixture:' + ('same' if same_input else text)}})
    rendezvous = threading.Barrier(2)
    calls = []
    def fetch(info, actual_patient):
        assert actual_patient is patient
        rendezvous.wait(timeout=2)
        calls.append(info['doi'])
        return 'source:' + info['doi'], False
    monkeypatch.setattr(cr, '_fetch_with_policy', fetch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(cr.resolve_and_fetch_all, text, ['1'], patient) for text in ('A', 'B')]
        actual = [future.result(timeout=5) for future in futures]
    assert actual == [{'1': 'source:fixture:' + ('same' if same_input else text)} for text in ('A', 'B')]
    assert len(calls) == 2  # This change is not cross-request single-flight.
