import os, sys, time, subprocess, threading, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
# In-process ASGI server with only the external metadata transport stubbed.
os.environ['HVE_ALLOWED_ORIGINS'] = 'http://127.0.0.1:8769'
from web import app as wa
from web import citation_existence as ce
import uvicorn
from unittest.mock import Mock
FEED=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1706.03762v7</id><title>Existing wrong source</title></entry></feed>'
r=Mock(status_code=200);r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False);r.iter_content.return_value=[FEED]
ce.requests.get=Mock(return_value=r)
wa._rate_limit_requests=100
os.environ['OPENAI_API_KEY']=''
server=uvicorn.Server(uvicorn.Config(wa.app,host='127.0.0.1',port=8768,log_level='error'))
t=threading.Thread(target=server.run);t.start()
static=subprocess.Popen([sys.executable,'-m','http.server','8769','--bind','127.0.0.1','--directory',str(root/'frontend')],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
try:
 for _ in range(100):
  if server.started:break
  time.sleep(.05)
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,executable_path=os.environ.get('CHROMIUM_EXECUTABLE') or str(next(Path.home().glob('.cache/ms-playwright/chromium-*/chrome-linux*/chrome'))),args=['--no-sandbox'])
  for kind,url in [('server','http://127.0.0.1:8768'),('static','http://127.0.0.1:8769')]:
   page=browser.new_page(viewport={'width':1440,'height':1050});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   if kind=='static':
    page.route('**/env.js',lambda route: route.fulfill(body='window.env = {API_URL: "http://127.0.0.1:8768"};',content_type='application/javascript'))
   page.goto(url)
   assert page.locator('#verificationMode').input_value()=='claim_support'
   page.select_option('#verificationMode','citation_existence')
   page.set_input_files('#fileInput',{'name':'paper.txt','mimeType':'text/plain','buffer':b'Wrong claim [1].\nReferences\n[1] Wrong arXiv:1706.03762v7\n[2] Title only 2020.\n'})
   requests=[];page.on('request',lambda request: requests.append(request.url) if '/verify' in request.url else None)
   page.click('#submitBtn');page.wait_for_selector('#existenceResults:not(.hidden)')
   text=page.locator('#existenceResults').inner_text()
   assert '1 identifier occurrences exist; 1 unknown' in text and 'Claim support: not checked' in text
   assert not page.locator('#summaryBar').is_visible()
   page.evaluate('window.scrollTo(0, 0)')
   page.screenshot(path=str(Path(os.environ.get('HN_SCREENSHOT_DIR', '/tmp')) / f'HN-ID-first-{kind}-desktop.png'),full_page=True)
   count=len(requests);page.get_by_role('button',name='Choose a separate claim-support check').click();page.wait_for_timeout(150)
   assert len(requests)==count and page.locator('#verificationMode').input_value()=='claim_support'
   page.set_viewport_size({'width':390,'height':844})
   page.evaluate('window.scrollTo(0, 0)')
   page.screenshot(path=str(Path(os.environ.get('HN_SCREENSHOT_DIR', '/tmp')) / f'HN-ID-first-{kind}-mobile.png'),full_page=True)
   assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
   assert not errors,errors
   print(kind,': real UI submit -> real ASGI route -> stubbed metadata, result separation, explicit escalation/no request, default unchanged, mobile width: PASS')
   page.close()
  browser.close()
finally:
 server.should_exit=True;t.join(timeout=10);static.terminate();static.communicate(timeout=10)
