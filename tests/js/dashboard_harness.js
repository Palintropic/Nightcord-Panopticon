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
  replaced: null,
  replace(url){ this.replaced = url; },
};
const window = {
  matchMedia(){ return {matches: false}; },
  addEventListener(type, fn){ (windowListeners[type] = windowListeners[type] || []).push(fn); },
};

const responses = [];   // queue of {data} or {fail: true}
let role = 'superadmin';   // what /api/me answers; null = signed out (401)
function fetch(url){
  if(url === '/api/me'){
    if(role === 'network') return Promise.reject(new Error('network down'));
    if(role === 'broken') return Promise.resolve({ok: false, status: 500, json: async () => { throw new SyntaxError('html'); }});
    return Promise.resolve(role ? {ok: true, status: 200, json: async () => ({role})}
                                : {ok: false, status: 401, json: async () => ({error: '未登录'})});
  }
  if(!url.startsWith('/api/status') && !url.startsWith('/api/public/status')) return Promise.resolve({ok: true, status: 200, json: async () => ({samples: []})});
  const next = responses.shift() || {fail: true};
  if(next.fail) return Promise.reject(new Error('network down'));
  if(next.status) return Promise.resolve({ok: false, status: next.status, json: async () => ({error: '未登录'})});
  return Promise.resolve({ok: true, status: 200, json: async () => next.data});
}

const api = new Function('document', 'window', 'location', 'fetch', 'setInterval',
  script + '; return {refresh, machinesView, overviewItem, overview, panels, route, loadMe};')(document, window, location, fetch, () => 0);

const tick = () => new Promise(r => setTimeout(r, 0));
const summary = () => document.getElementById('statusSummary').textContent;

(async () => {
  const now = Date.now() / 1000;
  const status = ts => ({ts, panels: [{name: 'qy', kind: 'qingyuan', online: true,
    qingyuan: {online: true, cpu: 5, mem: 5, disk: 5, seen_at: ts}}], thresholds: {cpu: 90, mem: 90, disk: 85}});
  const out = {};
  await tick();   // the page's own first refresh waits for /api/me; let it go by on an empty queue

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
    {name: 'new', kind: 'qingyuan', online: true, qingyuan: rich},
    {name: 'noswap', kind: 'qingyuan', online: true, qingyuan: {...rich, swap: null, swap_total: 0}},
    {name: 'old', kind: 'qingyuan', online: true, qingyuan: {online: true, cpu: 5, mem: 5, disk: 5, seen_at: now}},
  ]}});
  await api.refresh(false); await tick();
  out.machines = api.machinesView();
  const byName = n => api.panels().find(p => p.name === n);
  out.overviewNew = api.overviewItem(byName('new'));
  out.overviewNoSwap = api.overviewItem(byName('noswap'));
  out.overviewOld = api.overviewItem(byName('old'));

  out.routes = {};
  for(const hash of ['#s=notify', '#s=appearance', '#s=panels', '#s=bogus']){
    location._hash = hash;
    out.routes[hash] = api.route();
  }

  role = 'admin';
  await api.loadMe();
  location._hash = '#s=notify';
  out.adminRoute = api.route();

  location._hash = '#p=1';
  responses.push({status: 401});
  await api.refresh(false); await tick();
  out.afterExpiry = location.replaced;

  role = null;
  await api.loadMe();
  location._hash = '#s=notify';
  out.publicRoute = api.route();
  responses.push({data: {ts: Date.now() / 1000, panels: [
    {name: 'tokyo', kind: 'qingyuan', online: true, state: 'caution', nodata: false},
    {name: 'gone', kind: 'qingyuan', online: false, state: 'warn', nodata: true},
    {name: 'Nightcord-Status', kind: 'nightcord-status', online: true, state: 'ok', nodata: false}],
    kaomoji: {assign: {tokyo: '爱莉'}, faces: {'爱莉': ['PUBLIC-FACE', 'PUBLIC-NEAR', 'PUBLIC-BAD']}}}});
  await api.refresh(false); await tick();
  out.publicSummary = summary();
  out.publicOverview = api.overview();
  out.publicButton = document.getElementById('logout').textContent;
  out.publicUpdated = document.getElementById('statusUpdated').textContent;
  out.publicRedirect = location.replaced;

  for(const failure of ['network', 'broken']){
    role = 'superadmin'; await api.loadMe();
    role = failure; await api.loadMe();
    out['meFails_' + failure] = api.route();
  }

  process.stdout.write(JSON.stringify(out));
})();
