"""Executed ONLY inside Docker. Observe executed assertions/tests, not model prose."""
import ast
import json
import os
from pathlib import Path
import sys
import traceback
import unittest


def main():
    sys.dont_write_bytecode = True
    sys.pycache_prefix = '/tmp/evotask-verification-cache'
    target = Path(sys.argv[1]).resolve()
    checks, reads, failures = [], set(), []
    token = sys.argv[2]
    tree = ast.parse(target.read_text(encoding='utf-8'), filename=str(target))
    def observed(line):
        checks.append({'kind': 'assert', 'line': line})
    class Observe(ast.NodeTransformer):
        def visit_Assert(self, node):
            self.generic_visit(node)
            return [ast.copy_location(ast.Expr(ast.Call(ast.Name('_evotask_observe_assert', ast.Load()),
                                                       [ast.Constant(node.lineno)], [])), node), node]
    tree = ast.fix_missing_locations(Observe().visit(tree))
    original_start = unittest.TestResult.startTest
    def start(result, test):
        checks.append({'kind':'unittest', 'id':test.id()})
        return original_start(result, test)
    unittest.TestResult.startTest = start
    unittest_results = []
    original_init = unittest.TestResult.__init__
    def initialize(result, *args, **kwargs):
        original_init(result, *args, **kwargs)
        unittest_results.append(result)
    unittest.TestResult.__init__ = initialize
    def audit(event, args):
        if event != 'open' or not isinstance(args[0], (str, bytes)):
            return
        try:
            path = Path(os.fsdecode(args[0])).resolve()
            if path.is_relative_to(Path('/work')) and not (isinstance(args[1], str) and any(c in args[1] for c in 'wax')):
                reads.add(str(path.relative_to('/work')))
        except (ValueError, OSError):
            pass
    sys.addaudithook(audit)
    # Imports resolve within the test directory. Inputs remain mounted read-only.
    sys.path.insert(0, str(target.parent))
    sys.argv = [str(target)]
    namespace = {'__name__':'__main__', '__file__':str(target), '_evotask_observe_assert':observed}
    exit_code = 0
    try:
        # unittest.main() discovers classes in __main__.
        import types
        module = types.ModuleType('__main__'); module.__dict__.update(namespace)
        sys.modules['__main__'] = module
        exec(compile(tree, str(target), 'exec'), module.__dict__)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else int(bool(exc.code))
    except BaseException as exc:
        exit_code = 1
        failures.append({'type':type(exc).__name__, 'message':str(exc)[:2000]})
        traceback.print_exc()
    skipped = sum(len(r.skipped) for r in unittest_results)
    errors = sum(len(r.errors) for r in unittest_results)
    failed = sum(len(r.failures) + len(r.unexpectedSuccesses) for r in unittest_results)
    if errors or failed:
        exit_code = 1
    executed = max(0, len(checks)-skipped)
    if not executed:
        exit_code = 1
        failures.append({'type':'NoTestsExecuted', 'message':'No assertion or unittest case executed. Define and call a standalone test entry point, or use unittest.main().'})
    failed += sum(f['type']=='AssertionError' for f in failures)
    errors += sum(f['type'] not in {'AssertionError','NoTestsExecuted'} for f in failures)
    record = {'executed':executed, 'skipped':skipped, 'errors':errors, 'failed':failed, 'assertions':sum(c['kind']=='assert' for c in checks),
              'test_cases':sum(c['kind']=='unittest' for c in checks), 'reads':sorted(reads),
              'failures':failures, 'exit_code':exit_code}
    print('\nEVOTASK_VERIFICATION_' + token + ':' + json.dumps(record, ensure_ascii=True), flush=True)
    raise SystemExit(exit_code)


if __name__ == '__main__':
    main()
