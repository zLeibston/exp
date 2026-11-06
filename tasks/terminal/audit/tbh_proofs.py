"""Run original verifier functions against invalid artifacts in temporary directories.
Only absolute fixture paths are relocated; assertions and control flow are unchanged.
"""
import ast,tempfile,types,json,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]/'data/tbh/tasks'
class PytestStub:
 @staticmethod
 def fail(message):raise AssertionError(message)
results=[]
for name,artifacts in [
 ('tbh_task_4c14564e2ec33ce5',{'/home/user/duffing_mc.py':'# no simulation\n','/home/user/dominant_freq.txt':'0.500\n'}),
 ('tbh_task_7fee0e182f2ca1cd',{'/home/user/clean_results.txt':'fake_id: ACCEPT\n','/home/user/evil_results.txt':'fake_id: REJECT\n','/home/user/benchmark.sh':'#!/bin/sh\nexit 0\n'})]:
 with tempfile.TemporaryDirectory() as tmp:
  class Relocate(ast.NodeTransformer):
   def visit_Constant(self,node):
    if isinstance(node.value,str) and node.value.startswith('/home/user/'):
     return ast.copy_location(ast.Constant(tmp+node.value),node)
    return node
  tree=ast.parse((ROOT/name/'tests/test_final_state.py').read_text())
  tree.body=[n for n in tree.body if not isinstance(n,ast.Import) or all(a.name!='pytest' for a in n.names)]
  tree=ast.fix_missing_locations(Relocate().visit(tree));ns={'pytest':PytestStub};exec(compile(tree,name,'exec'),ns)
  for path,value in artifacts.items():
   p=Path(tmp+path);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
   if p.suffix=='.sh':p.chmod(0o755)
  tests=[v for k,v in ns.items() if k.startswith('test_') and callable(v)]
  for test in tests:test()
  results.append({'task':name,'passed_original_verifier_functions':len(tests),'artifacts':artifacts,'method':'absolute paths relocated; pytest.fail stubbed to raise; original assertions unchanged'})
print(json.dumps(results,indent=2));(Path(__file__).resolve().parent/'tbh-proof-results.json').write_text(json.dumps(results,indent=2))
