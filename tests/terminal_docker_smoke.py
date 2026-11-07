"""Offline API fixture exercising the real task worker, tool loop, Docker and verifier."""
import json
from contextlib import nullcontext
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import re
import shlex
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tasks.terminal import runtime
from tasks.terminal.loader import task_digest
from pipeline.api_config import parse_api
from pipeline.lifecycle import run_scope
from pipeline.storage import snapshot
from pipeline.editing import check
ROOT=Path(__file__).resolve().parents[1]
import subprocess
from pipeline.docker_eval import docker, ensure_images, image_name
ensure_images()
gateway=subprocess.check_output(docker()+['network','inspect','bridge','--format','{{(index .IPAM.Config 0).Gateway}}'],text=True).strip()
requests=[]
trial_counts={}
meta_stages=[]
NETWORK_PROBE = r"""
import json, os, socket, urllib.request, urllib.error
report = {'only_loopback': set(os.listdir('/sys/class/net')) == {'lo'}}
for scheme in ('http', 'https'):
    try:
        urllib.request.urlopen(scheme + '://example.com/', timeout=3)
    except Exception as exc:
        report[scheme + '_proxy_blocked'] = '403' in str(exc)
    else:
        report[scheme + '_proxy_blocked'] = False
try:
    connection = socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    report['direct_socket_blocked'] = True
else:
    connection.close()
    report['direct_socket_blocked'] = False
assert all(report.values()), report
with open('/output/network_probe.json', 'w') as handle:
    json.dump(report, handle)
"""
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  data=b'network-ok';self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(body)
  if body['model']=='fixture-context-error':
   data=b'{"error":{"type":"invalid_request_error","code":"context_length_exceeded"}}'
   self.send_response(400);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
  if body['model'] in {'fixture-api-error','fixture-api-timeout'}:
   data=b'{"error":{"type":"rate_limit_error"}}'
   self.send_response(429)
   if body['model']=='fixture-api-timeout':self.send_header('Retry-After','10')
   self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
  if not any(m['role']=='tool' for m in body['messages']):
   command="test ! -e /tests/test.sh && printf 'ok' > /app/answer.txt; setsid python3 -m http.server 18123 --bind 127.0.0.1 --directory /app </dev/null >/tmp/task-server.log 2>&1 & sleep 0.5"
   if body['model']=='fixture-forged-output':
    command += "\nmkdir -p /output/verifier; printf '%s' '{\"passed\":true,\"error\":null}' > /output/verifier/result.json"
   if body['model']=='fixture-rsi':
    identifier=re.search(r'"task_id": "([^"]+)"',body['messages'][0]['content']).group(1)
    trial_counts[identifier]=trial_counts.get(identifier,0)+1
    trial=trial_counts[identifier]
    passed=(identifier=='a_forget' and (trial<=4 or (trial>8 and trial%2==1))) or (identifier=='b_stable' and (trial%2==1 or trial>8))
    if not passed:command=command.replace("printf 'ok'", "printf 'wrong'")
   if '"allow_internet": true' in body['messages'][0]['content']:
    public_probe = "import json,os,urllib.request; report={'has_network_interface':set(os.listdir('/sys/class/net'))!={'lo'},'direct_http_works':urllib.request.build_opener(urllib.request.ProxyHandler({})).open("+repr(f'http://{gateway}:{server.server_port}/network-probe')+",timeout=3).read()==b'network-ok'}; assert all(report.values()),report; open('/output/network_probe.json','w').write(json.dumps(report))"
    command='python3 -c '+shlex.quote(public_probe)+' && '+command
   else:
    command='python3 -c '+shlex.quote(NETWORK_PROBE)+' && '+command
   if body['model']=='fixture-meta':
    instruction=body['messages'][0]['content']
    candidate=re.search(r'Candidate harness to modify: `([^`]+)`',instruction).group(1)
    stage='stability_repair' if 'Improve stability in the task harness.' in instruction else 'forgetting_repair'
    meta_stages.append(stage)
    view=Path(re.search(r'Current material \(read-only\): `([^`]+)`',instruction).group(1)).parent
    allowed=['b_stable'] if stage=='stability_repair' else ['a_forget']
    other='forgetting_repair' if stage=='stability_repair' else 'stability_repair'
    probe="import json; from pathlib import Path; p=Path("+repr(str(view))+"); x=json.loads((p/'index.json').read_text()); assert sorted(i['task_id'] for i in x['tasks'])=="+repr(allowed)+"; assert not (p.parent/"+repr(other)+").exists(); assert not (p.parent.parent/'evaluation').exists(); assert not (p.parent.parent/'labels.json').exists()"
    assert 'Material directory guide:' in instruction
    assert str(view)=='/materials' and candidate=='/candidate/harness/task'
    assert '/home/ubuntu/RSI' not in instruction and 'round_0002' not in instruction
    probe += "; assert all(not Path(x).exists() for x in ['/candidate/harness/meta','/history','/unmounted-history','/var/run/docker.sock']); assert 'stage' not in x; assert all('round' not in item['current'] and 'harness_hash' not in item['current'] for item in x['tasks'])"

    probe += "; assert (p/'current_harness/task_agent.py').is_file(); assert (p/'comparison.md').is_file(); assert all(Path(t['trajectory']).joinpath('trajectory.md').is_file() for item in x['tasks'] for t in item['current']['trials'])"
    if stage=='forgetting_repair':
     probe += "; assert all(Path(item['previous']['harness']).joinpath('task_agent.py').is_file() and Path(item['harness_diff']).is_file() and len(item['previous']['trials'])==4 for item in x['tasks'])"
    command='python3 -c '+shlex.quote(probe)+" && printf '\\n# fixture candidate\\n' >> "+shlex.quote(candidate+'/prompt.py')
   msg=dict(role='assistant',content=None,tool_calls=[dict(id='call1',type='function',function=dict(name='bash',arguments=json.dumps(dict(command=command))))])
   if body['model']=='fixture-meta':
    msg['tool_calls'].append(dict(id='unrelated-read',type='function',function=dict(name='editor',arguments=json.dumps(dict(command='view',path='/unmounted-history/previous-round/result.json')))))
    msg['tool_calls'].append(dict(id='candidate-read',type='function',function=dict(name='editor',arguments=json.dumps(dict(command='view',path=candidate+'/prompt.py')))))

  else:msg=dict(role='assistant',content='{"response":"done"}')
  data=json.dumps(dict(choices=[dict(message=msg,finish_reason='stop')])).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
server=ThreadingHTTPServer(('0.0.0.0',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
try:
 with nullcontext(tempfile.mkdtemp(prefix='rsi-terminal-smoke-')) as tmp:
  root=Path(tmp);run=root/'run';run.mkdir();(run/'.run.lock').touch()
  model=dict(run_dir=str(run),profiles={'task_agent':parse_api(dict(model='fixture',api_key='fixture',provider='openai',url=f'http://{gateway}:{server.server_port}',retries=0),'task_agent')})
  workspace=snapshot(ROOT,run/'version')
  task=root/'fixture';(task/'environment').mkdir(parents=True);(task/'tests').mkdir()
  (task/'task.toml').write_text('[agent]\ntimeout_sec=40\n[verifier]\ntimeout_sec=15\n[environment]\ncpus=1\nmemory_mb=512\nallow_internet=false\nbuild_timeout_sec=120\n')
  (task/'environment/Dockerfile').write_text(f'FROM {image_name()}\nWORKDIR /app\nRUN mkdir -p /app\n')
  (task/'tests/test.sh').write_text('mkdir -p /logs/verifier\nif python3 /tests/check.py; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi\n')
  (task/'tests/check.py').write_text('from pathlib import Path\nimport urllib.request\nassert Path("/app/answer.txt").read_text()=="ok"\nassert urllib.request.urlopen("http://127.0.0.1:18123/answer.txt").read()==b"ok"\n')
  with run_scope(run,max_containers=2,cpus=1,memory=1024**3,pids_limit=128):
   out=run/'case';out.mkdir();inp=dict(domain='terminal',task_id='fixture',problem_statement='Create answer.txt containing ok and serve it.',_task_path=str(task),_data_hash=task_digest(task))
   
   try:
    response=runtime.invoke_task(workspace,out,model,40,inp)
   except BaseException:
    print('SMOKE ARTIFACTS',root,flush=True)
    print((out/'stderr.log').read_text() if (out/'stderr.log').exists() else 'no stderr',flush=True)
    raise
   grade=runtime.grade({},None,out)
   assert grade['passed'],grade
   assert response['prediction']=='done'
   assert (out/'chat.log').exists()
   assert not (out/'api.yaml').exists()
   assert not (out/'tests').exists()
   print('REAL DOCKER SMOKE PASS: worker native tool writes files; background HTTP service survives to hidden grading; reward=1; cleanup confirmed')
   print('Execution:',json.loads((out/'container.json').read_text())['cleanup_status'])
   summary=json.loads((out/'execution.json').read_text())
   assert summary['schema']==1 and summary['stop_reason']=='final_response'
   assert 'container' not in summary and 'source_harness' not in summary
   probe=json.loads((out/'agent_output/network_probe.json').read_text())
   assert all(probe.values()),probe
   print('NETWORK ISOLATION PASS:',probe,flush=True)
   originals={file:(task/file).read_text() for file in ['task.toml','tests/test.sh','tests/check.py']}
   for mode in ['dataset_internet_enabled', 'wrong_answer', 'missing_reward', 'verifier_timeout']:
    if mode == 'dataset_internet_enabled':
     cfg=task/'task.toml';cfg.write_text(cfg.read_text().replace('allow_internet=false','allow_internet=true'))
    elif mode == 'wrong_answer':
     (task/'tests/check.py').write_text('assert False, "fixture failure"\n')
    elif mode == 'missing_reward':
     (task/'tests/test.sh').write_text('echo checked_without_reward\n')
    else:
     cfg=task/'task.toml';cfg.write_text(cfg.read_text().replace('timeout_sec=15','timeout_sec=1'))
     (task/'tests/test.sh').write_text('sleep 10\n')
    extra=run/mode;extra.mkdir();extra_inputs=dict(inp,_data_hash=task_digest(task))
    try:
     case_model=model
     if mode=='wrong_answer':
      case_model=dict(model,profiles={'task_agent':parse_api(dict(model='fixture-forged-output',api_key='fixture',provider='openai',url=f'http://{gateway}:{server.server_port}',retries=0),'task_agent')})
     runtime.invoke_task(workspace,extra,case_model,40,extra_inputs)
     if mode=='dataset_internet_enabled':assert runtime.grade({},None,extra)['passed']
     elif mode=='wrong_answer':
      assert not runtime.grade({},None,extra)['passed']
      assert json.loads((extra/'agent_output/verifier/result.json').read_text())['passed'] is True
     else:raise AssertionError(f'{mode} should fail')
    except ValueError:
     assert mode=='missing_reward'
    except subprocess.TimeoutExpired:
     assert mode=='verifier_timeout'
    execution=json.loads((extra/'container.json').read_text())
    assert execution['cleanup_status']=='removed'
    assert execution['network_policy']=='public'
    assert json.loads((extra/'request.json').read_text())['inputs']['allow_internet'] is True
    assert all(json.loads((extra/'agent_output/network_probe.json').read_text()).values())
    print('DOCKER CASE PASS:',mode,flush=True)
   # Verify new controller image/check path, without task-domain source files.
   from pipeline.docker_eval import ensure_images
   ensure_images()
   check(workspace,60)
   print('New Meta/controller image and isolated harness contract probe PASS')
   # Agent failures are completed misses; API failures remain retryable errors.
   for file,text in originals.items():(task/file).write_text(text)
   for mode,statement in [('agent_crash', "raise RuntimeError('tool budget exhausted')"),
                          ('agent_exit', 'raise SystemExit(1)'),
                          ('agent_signal', 'import os,signal; os.kill(os.getpid(),signal.SIGKILL)'),
                          ('provider_replaced', "raise RuntimeError('provider classes are optional')"),
                          ('agent_timeout', 'import time; time.sleep(10)'),
                          ('context_limit', "from harness.task.agent.llm import get_response_from_llm; get_response_from_llm('hello',self.model)"),
                          ('api_deadline', "from harness.task.agent.llm import get_response_from_llm; get_response_from_llm('hello',self.model)"),
                          ('api_failure', "from harness.task.agent.llm import get_response_from_llm; get_response_from_llm('hello',self.model)")]:
    broken=snapshot(workspace,run/(mode+'-version'))
    (broken/'harness/task/task_agent.py').write_text(
     'from pathlib import Path\nfrom harness.task.agent.base_agent import AgentSystem\n'
     'from harness.task.agent.trace import Trace\nclass TaskAgent(AgentSystem):\n'
     ' def forward(self, inputs):\n'
     '  with Trace(Path(self.logger_manager.log_file).parent) as trace:\n'
     '   trace.history([dict(role="assistant",text="partial task work")])\n'
     '   '+statement+'\n')
    if mode=='provider_replaced':
     (broken/'harness/task/agent/providers.py').write_text('def complete(*args, **kwargs): return {}\n')
     check(broken,60)
    extra=run/mode;extra.mkdir()
    try:
     trial_model=model
     if mode in {'api_failure','api_deadline','context_limit'}:
      api_model={'api_failure':'fixture-api-error','api_deadline':'fixture-api-timeout','context_limit':'fixture-context-error'}[mode]
      trial_model=dict(model,profiles={'task_agent':parse_api(dict(model=api_model,api_key='fixture',provider='openai',url=f'http://{gateway}:{server.server_port}',retries=1 if mode=='api_deadline' else 0),'task_agent')})
     response=runtime.invoke_task(broken,extra,trial_model,2,dict(inp,_data_hash=task_digest(task)))
     assert mode not in {'api_failure','api_deadline'}
     assert response['task_failure'] and response['history'][0]['text']=='partial task work'
     if mode=='context_limit':assert response['task_failure']['reason']=='context_limit'
     assert runtime.grade({},None,extra)['passed'] is False
     assert runtime.grade({},None,extra)['error'] is None
     assert not (extra/'verifier.log').exists()
    except RuntimeError as exc:
     assert mode in {'api_failure','api_deadline'}
     if mode=='api_deadline':assert 'unresolved API failure' in str(exc),exc
     assert not (extra/'verifier/result.json').exists()
    assert json.loads((extra/'container.json').read_text())['cleanup_status'] in {'removed','already_removed'}
    print('FAILURE CLASSIFICATION PASS:',mode,flush=True)
  # Exercise full cold start and both repairs through the trusted scheduler.
  for file,text in originals.items():(task/file).write_text(text)
  with (task/'task.toml').open('a') as handle:handle.write('\n# preparation queue '+root.name+'\n')
  from tasks import TaskSuite
  from pipeline.app import run
  cases=[dict(id=name,language='python',source='tb',task_path=str(task),data_hash=task_digest(task),
              inputs=dict(domain='terminal',task_id=name,problem_statement='Create answer.txt containing ok and serve it.'))
         for name in ['a_forget','b_stable','c_keep']]
  suite=TaskSuite(cases=cases,name='terminal',prepare_case=runtime.prepare,grade_case=runtime.grade,
                  task_runner=runtime.invoke_task,setup_environment=runtime.setup_environment,
                  environment_ready=runtime.environment_ready,prepare_environment=runtime.prepare_environment)
  profile=dict(model='fixture-rsi',api_key='fixture',provider='openai',url=f'http://{gateway}:{server.server_port}',retries=0)
  config=root/'fixture.yaml';config.write_text(json.dumps(dict(task_agent=profile,meta_agent=dict(profile,model='fixture-meta'),
       evaluation=dict(workers=2),execution=dict(max_containers=2,cpus=1,memory='1g',pids_limit=128),
       scheduler=dict(cold_start_repeats=4,repeats_per_task=4,quotas=dict(A=1,B=1,C=1)))))
  with patch('pipeline.app.load_domain',return_value=suite):
   state=run(root/'evolution',rounds=2,timeout=40,config=config,domain='terminal')
   before=len(requests)
   resumed=run(root/'evolution',rounds=2,timeout=40,config=config,domain='terminal',resume=True)
   assert len(requests)==before
  assert state['round']==2 and resumed==state,state
  assert meta_stages==['stability_repair','forgetting_repair'],meta_stages
  for stage in meta_stages:
   changes=root/'evolution'/'round_0002'/stage/'attempt_0001'/'edit'/'changes.json'
   assert json.loads(changes.read_text())['files']==['harness/task/prompt.py'],changes
   response=json.loads((changes.parent/'agent/result.json').read_text())
   denied=next(m for m in response['history'] if m.get('call_id')=='unrelated-read')
   assert denied['is_error'] and 'does not exist' in denied['text'],denied
   allowed=next(m for m in response['history'] if m.get('call_id')=='candidate-read')
   assert not allowed['is_error'] and 'TASK_INSTRUCTION' in allowed['text'],allowed

  assert {key:value['label'] for key,value in state['tasks'].items()}==dict(a_forget='B',b_stable='A',c_keep='C')
  assert {key:value['evaluation_count'] for key,value in state['tasks'].items()}==dict(a_forget=12,b_stable=12,c_keep=8)
  assert trial_counts==dict(a_forget=12,b_stable=12,c_keep=8),trial_counts
  assert [s['status'] for s in state['generations'][-1]['stages']]==['accepted','accepted']
  assert len(state['generations'])==2
  manifest=json.loads((root/'evolution/config.json').read_text())['execution_code']
  assert 'pipeline/worker.py' in manifest and 'tasks/terminal/runtime.py' in manifest
  with patch('pipeline.app.execution_manifest',return_value={'pipeline/worker.py':'changed'}):
   try:run(root/'evolution',rounds=2,timeout=40,config=config,domain='terminal',resume=True)
   except ValueError as exc:assert 'Execution code' in str(exc),exc
   else:raise AssertionError('Changed execution code must reject resume')
  assert len(requests)==before
  pins=list((root/'evolution/image_pins').glob('*.json'))
  assert len(pins)==1 and json.loads(pins[0].read_text())['image_id']
  print('FULL ABC RSI SMOKE PASS: repeated evaluation, stability then forgetting with targeted acceptance validation, disjoint mounted evidence, checkpoint and resume',flush=True)
  print('Mock API calls',len(requests))
  print('Smoke artifacts',root)
finally:
 server.shutdown();server.server_close();thread.join()
