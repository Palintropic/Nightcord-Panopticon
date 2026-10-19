// Runs static/index.html's script against a minimal fake DOM and a scripted fetch, then prints
// what the status bar says after each step as JSON. Driven by tests/test_dashboard_js.py.
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];

function makeElement(){
  const listeners = {};
  return {
    textContent: '', innerHTML: '', hidden: false, style: {}, dataset: {},
    classList: {add(){}, remove(){}, toggle(){}},
    addEventListener(type, fn){ (listeners[type] = listeners[type] || []).push(fn); },
    querySelectorAll(){ return []; },
    querySelector(){ return null; },
    closest(){ return null; },
  };
}
const elements = {};
const windowListeners = {};
const document = {
  getElementById(id){ return elements[id] = elements[id] || makeElement(); },
  querySelectorAll(){ return []; },
  querySelector(){ return null; },
  addEventListener(){},
};
const location = {
  _hash: '',
  get hash(){ return this._hash; },
  set hash(v){ this._hash = v; (windowListeners.hashchange || []).forEach(fn => fn()); },
};
const window = {
  matchMedia(){ return {matches: false}; },
  addEventListener(type, fn){ (windowListeners[type] = windowListeners[type] || []).push(fn); },
};

const responses = [];   // queue of {data} or {fail: true}
function fetch(url){
  if(!url.startsWith('/api/status')) return Promise.resolve({ok: true, status: 200, json: async () => ({samples: []})});
  const next = responses.shift() || {fail: true};
  if(next.fail) return Promise.reject(new Error('network down'));
  return Promise.resolve({ok: true, status: 200, json: async () => next.data});
}

const api = new Function('document', 'window', 'location', 'fetch', 'setInterval',
  script + '; return {refresh, machinesView, overviewItem, panels};')(document, window, location, fetch, () => 0);

const tick = () => new Promise(r => setTimeout(r, 0));
const summary = () => document.getElementById('statusSummary').textContent;

(async () => {
  const now = Date.now() / 1000;
  const status = ts => ({ts, panels: [{name: 'qy', kind: 'qingyuan-only', online: true,
    qingyuan: {online: true, cpu: 5, mem: 5, disk: 5, seen_at: ts}}], thresholds: {cpu: 90, mem: 90, disk: 85}});
  const out = {};

  responses.push({data: status(now)});
  await api.refresh(true); await tick();
  out.fresh = summary();

  responses.push({fail: true});
  await api.refresh(false); await tick();
  out.afterFailure = summary();

  location.hash = '#p=1'; await tick();
  out.afterSwitchingPivot = summary();

  responses.push({data: status(now - 600)});
  await api.refresh(false); await tick();
  out.oldData = summary();

  // A current agent with swap / load / processes, one without swap, and an old agent with none of it.
  const rich = {online: true, cpu: 5, mem: 5, disk: 5, seen_at: now, swap: 12.5, swap_total: 2147483648,
    load1: 0.42, load5: 0.3, load15: 0.25, cores: 4, procs: 187, tcp_established: 23, boot_time: now - 90000};
  responses.push({data: {ts: now, thresholds: {cpu: 90, mem: 90, disk: 85}, panels: [
    {name: 'new', kind: 'qingyuan-only', online: true, qingyuan: rich},
    {name: 'noswap', kind: 'qingyuan-only', online: true, qingyuan: {...rich, swap: null, swap_total: 0}},
    {name: 'old', kind: 'qingyuan-only', online: true, qingyuan: {online: true, cpu: 5, mem: 5, disk: 5, seen_at: now}},
  ]}});
  await api.refresh(false); await tick();
  out.machines = api.machinesView();
  const byName = n => api.panels().find(p => p.name === n);
  out.overviewNew = api.overviewItem(byName('new'));
  out.overviewNoSwap = api.overviewItem(byName('noswap'));
  out.overviewOld = api.overviewItem(byName('old'));

  process.stdout.write(JSON.stringify(out));
})();
