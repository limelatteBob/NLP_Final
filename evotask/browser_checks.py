"""Controller-owned declarative UI checks in a fresh browser with external requests blocked.

No generated Python or arbitrary evaluate() is executed on the controller host.
Only the selected document and its sibling static assets are served.
"""
from pathlib import Path
from urllib.parse import urlsplit, unquote, quote
import mimetypes
import time
import sys
from .storage import safe_path, digest

def validate_steps(steps):
    if not isinstance(steps,list) or not 1<=len(steps)<=24:
        raise ValueError('Browser checks need 1–24 declarative steps')
    assertions=0
    for step in steps:
        if not isinstance(step,dict) or step.get('action') not in {'click','fill','press','select','text','text_exact','value','visible','count'}:
            raise ValueError('Browser action must be click/fill/press/select/text/text_exact/value/visible/count')
        if not isinstance(step.get('selector'),str) or not 1<=len(step['selector'])<=300:
            raise ValueError('Browser step needs a bounded CSS/role selector')
        if step['action'] in {'fill','press','select','text','text_exact','value'} and (not isinstance(step.get('value'),str) or len(step['value'])>2000):
            raise ValueError('Browser step needs a bounded string value')
        if step['action']=='count' and (type(step.get('value')) is not int or not 0<=step['value']<=10000):
            raise ValueError('Browser count needs a nonnegative integer')
        assertions += step['action'] in {'text','text_exact','value','visible','count'}
    if steps[-1]['action'] not in {'text','text_exact','value','visible','count'}:
        raise ValueError('Browser interactions must end with an observable assertion of their effect')
    if not assertions:
        raise ValueError('Browser checks need an observable assertion, not just clicks')

def verify_browser(root, relative, steps, timeout=30):
    validate_steps(steps)
    target=safe_path(root,relative)
    if target.suffix.lower()!='.html' or not target.is_file():
        raise ValueError('Browser check requires an existing authorized HTML file')
    try:
        from playwright.sync_api import sync_playwright, expect
    except ImportError as exc:
        raise RuntimeError('Browser runtime unavailable: install evotask[browser] and Edge on Windows, or Playwright Chromium on other platforms') from exc
    manifest={}; errors=[]; blocked=[]; executed=0; outcomes=[]
    started=time.monotonic(); origin='https://evotask.invalid'
    # Serve only the HTML folder; no file:// or arbitrary local service access.
    assets=target.parent
    def serve(route):
        parsed=urlsplit(route.request.url)
        if parsed.scheme!='https' or parsed.netloc!='evotask.invalid':
            blocked.append(route.request.url[:300]);route.abort();return
        try:
            p=safe_path(assets,unquote(parsed.path).lstrip('/'))
            if p.suffix.lower() not in {'.html','.css','.js','.json','.svg','.png','.jpg','.jpeg'} or not p.is_file() or p.stat().st_size>262144:
                raise ValueError('Unavailable local asset')
            content=p.read_bytes();manifest[p.relative_to(root).as_posix()]=digest(content)
            route.fulfill(status=200,body=content,content_type=mimetypes.guess_type(p.name)[0] or 'application/octet-stream')
        except (OSError,ValueError):
            blocked.append(route.request.url[:300]);route.abort()
    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(**({'channel':'msedge'} if sys.platform=='win32' else {}),headless=True,chromium_sandbox=True)
            try:
                context=browser.new_context(service_workers='block',accept_downloads=False)
                context.route('**/*',serve)
                if hasattr(context,'route_web_socket'):
                    context.route_web_socket('**/*',lambda ws:ws.close())
                page=context.new_page();page.set_default_timeout(min(timeout,5)*1000)
                page.on('pageerror',lambda exc:errors.append(str(exc)[:1000]))
                page.goto(origin+'/'+quote(target.name),wait_until='load')
                for step in steps:
                    if time.monotonic()-started>timeout: raise TimeoutError('Browser check deadline exceeded')
                    locator=page.locator(step['selector']); action=step['action']; value=step.get('value')
                    if action in {'text','text_exact','value','visible','count'}:executed+=1
                    if action=='click':locator.click()
                    elif action=='fill':locator.fill(value)
                    elif action=='press':locator.press(value)
                    elif action=='select':locator.select_option(value)
                    elif action=='text':expect(locator).to_contain_text(value)
                    elif action=='text_exact':expect(locator).to_have_text(value)
                    elif action=='value':expect(locator).to_have_value(value)
                    elif action=='visible':expect(locator).to_be_visible()
                    elif action=='count':expect(locator).to_have_count(value)
                    outcomes.append(dict(step,passed=True))
            finally:browser.close()
    except Exception as exc:
        errors.append(type(exc).__name__+': '+str(exc)[:2000])
    changed=[p for p,h in manifest.items() if not safe_path(root,p).is_file() or digest(safe_path(root,p).read_bytes())!=h]
    return {'success':bool(executed and not errors and not blocked and not changed),'sandbox':'isolated-browser-context',
            'output':'\n'.join(errors+(['Blocked or missing resources: '+str(blocked)] if blocked else [])),
            'verification':{'executed':executed,'manifest':manifest,'steps':outcomes,'changed':changed,'blocked':blocked},
            'seconds':time.monotonic()-started}
