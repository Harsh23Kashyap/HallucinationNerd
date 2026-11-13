"""Optional browser checks: pip install playwright; set CHROME_PATH if needed."""
import json
import os
import shutil
import socket
import threading
import time

import pytest

playwright = pytest.importorskip('playwright.sync_api')


@pytest.fixture(scope='module')
def browser_page():
    import uvicorn
    from web.app import app

    chrome = os.environ.get('CHROME_PATH') or shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Set CHROME_PATH to a local Chromium executable')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.02)
        assert server.started
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=chrome, args=['--no-sandbox'])
            page = browser.new_page()
            base = f'http://127.0.0.1:{port}'
            page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
            yield page, base
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.parametrize('status,payload,expected', [
    (429, {'detail': 'Rate limit exceeded. Try again later.'}, 'Rate limit exceeded. Try again later.'),
    (413, {'detail': 'File too large.'}, 'File too large.'),
    (400, {'detail': 'Backup search is disabled.'}, 'Backup search is disabled.'),
    (500, {'detail': 'Server misconfigured.'}, 'Server misconfigured.'),
    (422, {'detail': [{'loc': ['body', 'file']}]}, 'Verification failed (HTTP 422). Please try again.'),
    (503, {}, 'Verification failed (HTTP 503). Please try again.'),
    (500, {'detail': None}, 'Verification failed (HTTP 500). Please try again.'),
    (500, {'detail': ''}, 'Verification failed (HTTP 500). Please try again.'),
    (500, {'detail': '   '}, 'Verification failed (HTTP 500). Please try again.'),
    (429, {'detail': '<img src=x onerror=alert(1)>'}, '<img src=x onerror=alert(1)>'),
    (500, {'error': 'Existing verification error.'}, 'Existing verification error.'),
    (500, {'error': 'Existing error.', 'detail': 'Other detail.'}, 'Existing error.'),
])
def test_http_error_is_displayed_without_report_rendering(browser_page, status, payload, expected):
    page, base = browser_page
    page.route('**/verify', lambda route: route.fulfill(status=status, content_type='application/json', body=json.dumps(payload)))
    try:
        page.goto(base)
        page.locator('#fileInput').set_input_files({'name': 'fixture.txt', 'mimeType': 'text/plain', 'buffer': b'Offline fixture.'})
        page.locator('#submitBtn').click()
        page.wait_for_function('!document.querySelector("#submitBtn").disabled')
        assert page.locator('#errorMsg').inner_text() == expected
        assert page.locator('#errorState').is_visible()
        assert page.locator('#errorMsg img').count() == 0
        assert not page.locator('#reviewTools').is_visible()
    finally:
        page.unroute('**/verify')
