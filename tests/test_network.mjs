import assert from 'node:assert/strict';
import {buildNetwork, edgeGeometry} from '../evotask/web/static/network-model.js';

const task = (id, owner, status, dependencies = [], extra = {}) => ({id, owner, role: owner || 'verifier', status, title: `任务 ${id}`, objective: '完成目标', depends_on: dependencies, inputs: [], ...extra});
const event = (seq, kind, agent, data) => ({seq, kind, agent, data, run_id: 'run_fixture', created: 100 + seq});
const run = {id: 'run_fixture', status: 'running', executing: true, pending: null, tasks: [
  task('t1', 'researcher', 'succeeded'), task('t2', 'coder', 'running', ['t1'], {inputs:['inputs/notes.md']}),
  task('t3', null, 'pending', ['t2']), task('t4', 'coder', 'pending', ['t2']),
  task('sub1', 'researcher', 'running', [], {parent:'t2', role:'verifier'}),
], artifacts: [{id:'a1',task_id:'t1',owner:'researcher',path:'notes.md'}]};
const events = [
  event(1,'task_assigned','researcher',{task_id:'t1'}),
  event(2,'task_assigned','coder',{task_id:'t2'}),
  event(3,'artifact_handoff','coder',{artifact_id:'a1',path:'inputs/notes.md'}),
  event(4,'delegated','coder',{from_task:'t2',to_task:'sub1',role:'verifier'}),
  event(5,'task_assigned','researcher',{task_id:'sub1'}),
  event(6,'revision_requested','researcher',{from_task:'sub1',target_task:'t2',reason:'边界错误',round:1}),
];
let count=0;
function test(name, fn) { fn();count++;console.log('PASS '+name); }
const clone = value => JSON.parse(JSON.stringify(value));
test('actual owners, dependencies, delegation, revision and same-direction aggregation', () => {
  const before=JSON.stringify({run,events}), model=buildNetwork(run,events);
  assert.equal(JSON.stringify({run,events}),before);
  assert.equal(model.nodes.length,4);
  assert.equal(model.nodes[0].status.label,'已安排分工');
  assert.equal(model.nodes[0].coordinationCount,3);
  assert.equal(model.nodes.find(n=>n.id==='researcher').status.code,'running');
  assert.equal(model.nodes.find(n=>n.id==='verifier').status.code,'pending');
  assert.equal(model.nodes.find(n=>n.id==='coder').internal.length,1);
  assert.ok(model.edges.every(e=>e.from!==e.to));
  const delegated=model.relations.find(r=>r.kind==='delegation');
  assert.equal(delegated.to,'researcher');assert.equal(delegated.planned,false);
  assert.equal(model.edges.find(e=>e.key==='researcher:coder').relations.length,3);
  assert.equal(model.edges.find(e=>e.key==='coordinator:researcher').relations.length,2);
  assert.equal(model.relations.find(r=>r.kind==='revision').reason,'边界错误');
});
test('duplicate SSE events do not duplicate relations, real repeat handoffs do', () => {
  const baseline=buildNetwork(run,events);
  assert.equal(buildNetwork(run,[...events,...events]).relations.length,baseline.relations.length);
  assert.equal(buildNetwork(run,[...events,event(7,'artifact_handoff','coder',{artifact_id:'a1'})]).relations.length,baseline.relations.length+1);
});
test('unassigned roles remain planned and switch to real owners', () => {
  const unassigned=clone(run);unassigned.tasks.find(t=>t.id==='sub1').owner=null;
  const model=buildNetwork(unassigned,events.slice(0,4));
  const delegated=model.relations.find(r=>r.kind==='delegation');
  assert.equal(delegated.to,'verifier');assert.equal(delegated.planned,true);
  assert.equal(buildNetwork(run,events.slice(0,4)).relations.find(r=>r.kind==='delegation').to,'researcher');
  const dep=model.relations.find(r=>r.id==='dependency:t2:t3');
  assert.equal(dep.planned,true);assert.equal(dep.preassigned,true);
});
test('planning, pause, interruption, cancellation and terminal states stop work animations', () => {
  for (const patch of [{executing:false},{pending:{task_id:'t3'},status:'waiting_human'},{cancelling:true},{status:'failed'},{status:'succeeded'},{status:'cancelled'}]) {
    assert.ok(buildNetwork({...run,...patch},events).nodes.every(n=>n.status.code!=='running'));
  }
  assert.equal(buildNetwork({...run,executing:false},events).nodes.find(n=>n.id==='coder').status.code,'interrupted');
  let planning=buildNetwork({...run,status:'planning',tasks:[]},[]);
  assert.equal(planning.nodes.filter(n=>n.status.code==='running').length,1);
  assert.equal(planning.nodes[0].status.label,'规划中');
  planning=buildNetwork({...run,status:'planning',tasks:[],executing:false},[]);
  assert.equal(planning.nodes[0].status.code,'interrupted');
  const question=buildNetwork({...run,status:'waiting_human',tasks:[],pending:{task_id:null},executing:false},[]);
  assert.equal(question.nodes[0].status.code,'waiting_human');
});
test('aggregate status respects waiting, failure and incomplete work', () => {
  const r=clone(run);r.tasks=[task('a','coder','succeeded'),task('b','coder','pending')];
  assert.equal(buildNetwork(r).nodes[2].status.code,'pending');
  r.tasks.push(task('c','coder','failed'));assert.equal(buildNetwork(r).nodes[2].status.code,'failed');
  r.tasks.push(task('d','coder','waiting_child'));assert.equal(buildNetwork(r).nodes[2].status.code,'waiting_child');
  r.tasks.push(task('e','coder','waiting_human'));assert.equal(buildNetwork(r).nodes[2].status.code,'waiting_human');
  r.tasks.push(task('f','coder','running'));assert.equal(buildNetwork(r).nodes[2].status.code,'running');
  r.pending={task_id:'e'};assert.equal(buildNetwork(r).nodes[2].status.code,'waiting_human');
  r.tasks=[task('f',null,'failed')];assert.equal(buildNetwork(r).nodes[3].status.code,'failed');
});
test('unknown historical evidence is omitted rather than invented', () => {
  assert.equal(buildNetwork(run,[event(1,'revision_requested','verifier',{target_task:'missing'})]).relations.filter(r=>r.kind==='revision').length,0);
  assert.equal(buildNetwork(run,[event(1,'artifact_handoff','coder',{artifact_id:'missing'})]).relations.filter(r=>r.kind==='handoff').length,0);
  assert.ok(buildNetwork(run,[]).relations.every(r=>r.created===null && r.planned));
  assert.equal(buildNetwork({...run,tasks:[]},[]).edges.length,0);
  assert.equal(buildNetwork(run,events.map(e=>({...e,run_id:'other'}))).relations.filter(r=>r.eventSeq).length,0);
});
test('reverse arrows use distinct curves and terminate outside nodes', () => {
  const a={x:100,y:10,width:124,height:82},b={x:10,y:140,width:124,height:82};
  const forward=edgeGeometry(a,b,false,350),reverse=edgeGeometry(b,a,false,350);
  assert.notEqual(forward,reverse);assert.ok(!forward.includes('NaN'));
  const stacked=edgeGeometry(a,{...a,y:250},true,350);
  assert.ok(stacked.includes('C 345'));assert.ok(edgeGeometry({...a,y:250},a,true,350).includes('C 5'));
});
console.log(JSON.stringify({status:'passed',tests:count}));
