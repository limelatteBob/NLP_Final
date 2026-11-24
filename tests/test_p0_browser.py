import os
import tempfile
import unittest
from pathlib import Path
from evotask.browser_checks import validate_steps, verify_browser

class BrowserContractTests(unittest.TestCase):
    def test_no_assertions_and_arbitrary_evaluate_rejected(self):
        for steps in ([{'action':'click','selector':'#next'}],[{'action':'evaluate','selector':'body','value':'fetch(...)'}]):
            with self.assertRaises(ValueError):validate_steps(steps)

@unittest.skipUnless(os.environ.get('EVOTASK_BROWSER_TESTS')=='1','Opt-in local browser behavior checks')
class BrowserBehaviorTests(unittest.TestCase):
    def test_navigation_observed_and_bound_to_exact_html(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'slides.html').write_text('''<!doctype html><h1 id="page">1</h1>
<button id="next">Next</button><button id="prev">Previous</button><script>
let n=1;function show(){document.querySelector('#page').textContent=n}
document.querySelector('#next').onclick=()=>{n++;show()};
document.querySelector('#prev').onclick=()=>{n--;show()};
document.onkeydown=e=>{if(e.key==='ArrowRight'){n++;show()}};</script>''')
            steps=[{'action':'text','selector':'#page','value':'1'},{'action':'click','selector':'#next'},
                   {'action':'text','selector':'#page','value':'2'},{'action':'click','selector':'#prev'},
                   {'action':'text','selector':'#page','value':'1'},{'action':'press','selector':'body','value':'ArrowRight'},
                   {'action':'text','selector':'#page','value':'2'}]
            result=verify_browser(root,'slides.html',steps)
            self.assertTrue(result['success'],result);self.assertEqual(result['verification']['executed'],4)
            self.assertIn('slides.html',result['verification']['manifest'])
    def test_missing_external_dependency_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'index.html').write_text('<h1>Visible</h1><script src="https://example.com/not-allowed.js"></script>')
            result=verify_browser(root,'index.html',[{'action':'visible','selector':'h1'}])
            self.assertFalse(result['success']);self.assertTrue(result['verification']['blocked'])
