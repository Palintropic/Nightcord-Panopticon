// Settings pages of the dashboard (static/index.html, #settingsView).
const SECRET_PLACEHOLDER = '••••••••';
// Driven by the dashboard (index.html): its 设置 pivot item lists the pages below, and each one
// opens as #s=<key>. Nothing is fetched until settings are first opened.
const settings = window.panopticonSettings = {
  firstRun: false,
  loaded: false,
  ensureLoaded(){
    if(settings.loaded) return;
    settings.loaded = true;
    load();
    loadAgentHosts();
    loadAgentList();
    loadPasskeys();
  },
  // fresh = the page was just opened (not a background redraw): live lists reload.
  open(page, fresh){
    const first = !settings.loaded;
    settings.ensureLoaded();
    if(page === 'theme'){
      renderAccents(fresh && !accentResyncing);
      if(accentResyncing) resyncAccents();
      else if(fresh && !appearanceLoaded) loadAppearance().then(ok => { if(!ok) loadFailed('accentMsg'); });
    }
    if(page === 'kaomoji'){
      renderKaomoji(fresh && !kaomojiResyncing);
      if(kaomojiResyncing) resyncKaomoji();
      else if(fresh && !kaomojiLoaded) loadKaomoji().then(ok => { if(!ok) loadFailed('kaomojiMsg'); });
    }
    if(page === 'agents' && fresh && !first) loadAgentList();
  },
};

const RESYNC_FAILED = '没保存上，也没能读回服务器上的设置；过一会儿自动重试，没保存的内容先留着';
const RESYNC_DONE = '已经读回服务器上的设置，没保存的内容还在，可以再保存一次';

function loadFailed(msgId){
  const msg = document.getElementById(msgId);
  msg.className = 'err'; msg.textContent = '读取服务器上的设置失败，刷新页面再试';
}

// ---------- 主题 ----------
// Mode applies on selection, like Windows 10 Settings; stored per browser by /static/theme.js.
function syncThemeRadios(){
  const v = window.panopticonTheme.get();
  document.querySelectorAll('input[name="theme"]').forEach(r => { r.checked = r.value === v; });
}
document.querySelectorAll('input[name="theme"]').forEach(r => {
  r.addEventListener('change', () => { if(r.checked) window.panopticonTheme.set(r.value); });
});
// 缩放: also per browser, applied by theme.js as CSS zoom on <html>.
function syncScale(){
  const current = window.panopticonTheme.getScale();
  document.getElementById('uiScale').innerHTML = window.panopticonTheme.SCALES.map(v =>
    `<option value="${v}"${v === current ? ' selected' : ''}>${v}%${v === 100 ? '（默认）' : ''}</option>`).join('');
}
document.getElementById('uiScale').addEventListener('change', e => window.panopticonTheme.setScale(Number(e.target.value)));
window.addEventListener('storage', () => { syncThemeRadios(); syncScale(); renderAccents(); });
syncThemeRadios();
syncScale();

// Surface colours are saved on the server the moment one is picked (POST /api/appearance), so
// every device shows the same. Swatches: 25 時's purple, then the Windows 10 accent palette.
const DEFAULT_ACCENT = '#884499';
const ACCENT_SWATCHES = ['#884499',
  '#ffb900', '#ff8c00', '#f7630c', '#ca5010', '#da3b01', '#ef6950',
  '#d13438', '#ff4343', '#e74856', '#e81123', '#ea005e', '#c30052',
  '#e3008c', '#bf0077', '#c239b3', '#9a0089', '#0078d7', '#0063b1',
  '#8e8cd8', '#6b69d6', '#8764b8', '#744da9', '#b146c2', '#881798',
  '#0099bc', '#2d7d9a', '#00b7c3', '#038387', '#00b294', '#018574',
  '#00cc6a', '#10893e', '#7a7574', '#5d5a58', '#68768a', '#515c6b',
  '#567c73', '#486860', '#498205', '#107c10', '#767676', '#4c4a48',
  '#69797e', '#4a5459', '#647c64', '#525e54', '#847545', '#7e735f'];
// One per unit: tile in one colour, curve and bar in members' colours (picked so neither sits
// next to the warn red or the caution amber).
const ACCENT_PRESETS = [
  {name:'默认（25 時紫）', tile:'#884499', curve:'#884499', bar:'#884499'},
  {name:'25時、ナイトコードで。', tile:'#884499', curve:'#ddaacc', bar:'#8888cc'},
  {name:'Leo/need', tile:'#4455dd', curve:'#bbdd22', bar:'#33aaee'},
  {name:'MORE MORE JUMP!', tile:'#88dd44', curve:'#ffaacc', bar:'#99ccff'},
  {name:'Vivid BAD SQUAD', tile:'#0077dd', curve:'#ff6699', bar:'#00bbdd'},
  {name:'ワンダーランズ×ショウタイム', tile:'#bb88ee', curve:'#ff66bb', bar:'#ff9900'},
];
const ACCENT_PARTS = ['tile', 'curve', 'bar'];

function currentAccents(){
  const a = window.panopticonTheme.getAccents();
  const out = {};
  ACCENT_PARTS.forEach(k => { out[k] = a[k] || DEFAULT_ACCENT; });
  return out;
}

// Background redraws (force false) leave a custom field alone while it is focused or holds an
// unapplied value. force true rewrites all three (page opened, preset picked); a list of parts
// rewrites only those (a swatch or custom colour for that part was picked).
function renderAccents(force){
  const a = currentAccents();
  const fg = window.panopticonTheme.foreground;
  // Until the saved colours arrive (appearanceLoaded, index.html) nothing here can save.
  const off = appearanceLoaded && !accentResyncing ? '' : ' disabled';
  const msg = document.getElementById('accentMsg');
  if(!appearanceLoaded && !msg.textContent){ msg.className = 'caption'; msg.textContent = '正在读取服务器上的配色…'; }
  if(appearanceLoaded && msg.textContent === '正在读取服务器上的配色…') msg.textContent = '';
  document.getElementById('accentPresets').innerHTML = ACCENT_PRESETS.map((pr, i) =>
    `<button class="preset" data-preset="${i}"${off}><span class="chips">${ACCENT_PARTS.map(k =>
      `<i style="background:${pr[k]}"></i>`).join('')}</span><span>${escapeHtml(pr.name)}</span></button>`).join('');
  ACCENT_PARTS.forEach(k => {
    document.querySelector(`.swatches[data-accent="${k}"]`).innerHTML = ACCENT_SWATCHES.map(c =>
      `<button class="swatch" role="radio" aria-checked="${c === a[k]}" aria-label="${c}" title="${c}"
        data-part="${k}" data-colour="${c}" style="background:${c};color:${fg(c)}"${off}></button>`).join('');
    const custom = document.querySelector(`[data-accent-custom="${k}"]`);
    const input = custom.querySelector('input');
    const rewrite = force === true || (Array.isArray(force) && force.includes(k));
    if(rewrite || !(custom.contains(document.activeElement) || (input && input.dataset.dirty))){
      custom.innerHTML = `<span class="chip" style="background:${a[k]}"></span>
        <input type="text" value="${a[k]}" aria-label="自定义颜色" maxlength="7" spellcheck="false"${off}>
        <button class="btn" data-custom-apply="${k}"${off}>应用</button>`;
    } else {
      // Kept as typed, but it still follows the page's state (locked while loading or resyncing).
      custom.querySelectorAll('input, button').forEach(el => { el.disabled = !!off; });
    }
  });
}

// Saves of one section go out one at a time, each carrying the section as it stands when it is
// sent, so the last pick is what the server keeps however the network orders the replies.
// send() resolves {ok, data, error}; done(result, last) runs after each, last = none queued behind,
// and the next save waits for whatever done() returns (a resync after a failure). dropQueued()
// cancels the saves still waiting: after a failure they were built on the change that failed.
function makeSaveQueue(){
  let tail = Promise.resolve();
  let pending = 0;
  let generation = 0;
  const queue = (send, done) => {
    pending++;
    const gen = generation;
    const run = tail
      .then(() => gen === generation ? send() : {ok:false, dropped:true})
      .then(r => {
        pending--;
        return Promise.resolve(r.dropped ? null : done(r, pending === 0)).then(() => r.ok);
      });
    tail = run.catch(() => {});
    return run;
  };
  queue.dropQueued = () => { generation++; };
  return queue;
}
async function postJson(url, body){
  try{
    const res = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const data = await res.json().catch(() => ({}));
    return res.ok ? {ok:true, data} : {ok:false, error:(data.errors || [data.error || `HTTP ${res.status}`]).join('；')};
  }catch(e){
    return {ok:false, error:String(e)};
  }
}
function setMsg(id, cls, text){
  const msg = document.getElementById(id);
  msg.className = cls; msg.textContent = text;
}

// After a failed save the page takes no new picks until the server's copy is back. A failed
// reload keeps it that way and is retried on every background redraw (open() below); a typed
// custom colour is kept throughout.
let accentResyncing = false;
let accentReload = null;
function resyncAccents(){
  accentResyncing = true;
  renderAccents(false);
  if(accentReload) return accentReload;
  accentReload = loadAppearance().then(ok => {
    accentReload = null;
    if(ok){
      accentResyncing = false;
      if(document.getElementById('accentMsg').textContent === RESYNC_FAILED) setMsg('accentMsg', 'caption', RESYNC_DONE);
    } else {
      setMsg('accentMsg', 'err', RESYNC_FAILED);
    }
    renderAccents(false);
  });
  return accentReload;
}
const accentQueue = makeSaveQueue();
function saveAccents(next){
  if(!appearanceLoaded || accentResyncing) return;
  window.panopticonTheme.setAccents({...currentAccents(), ...next});   // shows at once; the server copy follows
  renderAccents(Object.keys(next));
  setMsg('accentMsg', 'caption', '保存中…');
  accentQueue(() => postJson('/api/appearance', currentAccents()), (r, last) => {
    if(!r.ok){
      setMsg('accentMsg', 'err', '没保存上：' + r.error);
      // Show what the server really holds instead of the colours that did not save; the queue
      // waits for this before anything else goes out.
      accentQueue.dropQueued();
      return resyncAccents();
    }
    if(last && document.getElementById('accentMsg').className !== 'err') setMsg('accentMsg', 'caption', '已保存 ✓');
  });
}

document.getElementById('settingsView').addEventListener('click', e => {
  const sw = e.target.closest('.swatch');
  if(sw){ saveAccents({[sw.dataset.part]: sw.dataset.colour}); return; }
  const pr = e.target.closest('[data-preset]');
  if(pr){
    const p = ACCENT_PRESETS[Number(pr.dataset.preset)];
    saveAccents({tile:p.tile, curve:p.curve, bar:p.bar});
    return;
  }
  const ap = e.target.closest('[data-custom-apply]');
  if(ap) applyCustom(ap.dataset.customApply);
});
document.getElementById('settingsView').addEventListener('input', e => {
  if(e.target.closest('[data-accent-custom]')) e.target.dataset.dirty = '1';
});
document.getElementById('settingsView').addEventListener('keydown', e => {
  const wrap = e.target.closest('[data-accent-custom]');
  if(wrap && e.key === 'Enter') applyCustom(wrap.dataset.accentCustom);
});
function applyCustom(part){
  const input = document.querySelector(`[data-accent-custom="${part}"] input`);
  const v = input.value.trim().replace(/^#?/, '#').toLowerCase();
  const msg = document.getElementById('accentMsg');
  if(!/^#[0-9a-f]{6}$/.test(v)){
    msg.className = 'err'; msg.textContent = '自定义颜色要写成 #rrggbb，比如 #884499';
    return;
  }
  input.blur();
  saveAccents({[part]: v});
}

// ---------- 颜文字 ----------
// The character table (CHARACTERS) and the saved choices (kaomojiConfig) live in index.html,
// which draws the tiles. Picking a character saves at once; typed faces wait for 保存.
function renderKaomoji(fresh){
  // Until the saved choices arrive (kaomojiLoaded, index.html) there is nothing to edit: an empty
  // form saved now would wipe them.
  document.getElementById('kaomojiSave').disabled = !kaomojiLoaded || kaomojiResyncing;
  document.getElementById('kaomojiReset').disabled = !kaomojiLoaded || kaomojiResyncing;
  if(!kaomojiLoaded){
    document.getElementById('kaomojiAssign').innerHTML = '<div class="empty-hint">读取中…</div>';
    document.getElementById('kaomojiFaces').innerHTML = '<div class="empty-hint">读取中…</div>';
    return;
  }
  const msg = document.getElementById('kaomojiMsg');
  if(msg.className === 'err' && msg.textContent.startsWith('读取')) msg.textContent = '';
  const assign = kaomojiConfig.assign;
  const machines = panels().map(p => p.name).filter(Boolean);
  const options = name => {
    const picked = own(assign, name) ? assign[name] : '';
    return `<option value=""${picked ? '' : ' selected'}>自动（${escapeHtml(autoCharacterFor(name)[0])}）</option>` +
      CHARACTERS.map(([c]) => `<option value="${escapeAttr(c)}"${c === picked ? ' selected' : ''}>${escapeHtml(c)}</option>`).join('');
  };
  const wrap = document.getElementById('kaomojiAssign');
  if(!wrap.contains(document.activeElement)){
    wrap.innerHTML = machines.length ? machines.map(name => `<label class="assign-row"><span>${escapeHtml(name)}</span>
        <select data-assign="${escapeAttr(name)}"${kaomojiResyncing ? ' disabled' : ''}>${options(name)}</select></label>`).join('')
      : '<div class="empty-hint">还没有机器。</div>';
  }
  // The face fields are only rebuilt when the page is opened, so a refresh never eats typing.
  if(!fresh && document.querySelector('#kaomojiFaces .face-row')) return;
  document.getElementById('kaomojiFaces').innerHTML = CHARACTERS.map(([c, defaults]) => {
    const custom = own(kaomojiConfig.faces, c) ? kaomojiConfig.faces[c] : [];
    return `<div class="face-row" data-chara="${escapeAttr(c)}"><span class="who">${escapeHtml(c)}</span>${defaults.map((d, i) =>
      `<input type="text" maxlength="24" spellcheck="false" aria-label="${escapeAttr(c)} ${['一切正常', '接近阈值', '离线或需要关注'][i]}"
        placeholder="${escapeAttr(d)}" value="${escapeAttr(custom[i] || '')}">`).join('')}</div>`;
  }).join('');
}

function collectFaces(){
  const faces = {};
  document.querySelectorAll('#kaomojiFaces .face-row').forEach(row => {
    const triple = Array.from(row.querySelectorAll('input')).map(i => i.value.trim());
    if(triple.some(Boolean)) faces[row.dataset.chara] = triple;
  });
  return faces;
}

// After a failed save: drop any optimistic change and show what the server really holds. Until
// then the pickers and buttons are disabled; typed faces stay as typed so they can be saved again.
// A failed reload keeps it that way and is retried on every background redraw (open() below).
let kaomojiResyncing = false;
let kaomojiReload = null;
function resyncKaomoji(){
  kaomojiResyncing = true;
  if(document.activeElement && document.activeElement.closest('#kaomojiAssign')) document.activeElement.blur();
  renderKaomoji(false);
  if(kaomojiReload) return kaomojiReload;
  kaomojiReload = loadKaomoji().then(ok => {
    kaomojiReload = null;
    if(ok){
      kaomojiResyncing = false;
      if(document.getElementById('kaomojiMsg').textContent === RESYNC_FAILED) setMsg('kaomojiMsg', 'caption', RESYNC_DONE);
    } else {
      setMsg('kaomojiMsg', 'err', RESYNC_FAILED);
    }
    renderKaomoji(false);
  });
  return kaomojiReload;
}

// The change is recorded at once, so a save made before this one is answered builds on it; the
// queue then sends the whole section as it stands. Resolves to whether this save landed.
const kaomojiQueue = makeSaveQueue();
function saveKaomoji(update){
  if(!kaomojiLoaded || kaomojiResyncing) return Promise.resolve(false);
  kaomojiConfig = {...kaomojiConfig, ...update};
  setMsg('kaomojiMsg', 'caption', '保存中…');
  return kaomojiQueue(() => postJson('/api/kaomoji', kaomojiConfig), (r, last) => {
    if(!r.ok){
      setMsg('kaomojiMsg', 'err', '没保存上：' + r.error);
      kaomojiQueue.dropQueued();
      return resyncKaomoji();   // the queue waits for this before anything else goes out
    }
    // An earlier reply would roll back changes still queued behind it; only the last one applies.
    if(last){
      setKaomojiConfig(r.data);
      if(document.getElementById('kaomojiMsg').className !== 'err') setMsg('kaomojiMsg', 'ok', '已保存 ✓');
    }
  });
}

document.getElementById('kaomojiAssign').addEventListener('change', e => {
  const sel = e.target.closest('[data-assign]');
  if(!sel || !kaomojiLoaded || kaomojiResyncing) return;
  const assign = {...kaomojiConfig.assign};
  if(sel.value) assign[sel.dataset.assign] = sel.value; else delete assign[sel.dataset.assign];
  // Face fields not yet saved stay as typed; only the picks change.
  saveKaomoji({assign});
});
document.getElementById('kaomojiSave').addEventListener('click', () => saveKaomoji({faces:collectFaces()}));
document.getElementById('kaomojiReset').addEventListener('click', async () => {
  if(!confirm('把 20 个人的表情全部恢复成默认的？（谁看哪台机器不受影响）')) return;
  if(await saveKaomoji({faces:{}})) renderKaomoji(true);
});

// ---------- Passkey ----------
function base64urlToBuffer(base64url){
  const base64 = base64url.replace(/-/g, '+').replace(/_/g, '/');
  const pad = base64.length % 4 ? '='.repeat(4 - base64.length % 4) : '';
  const raw = atob(base64 + pad);
  const buf = new Uint8Array(raw.length);
  for(let i=0;i<raw.length;i++) buf[i] = raw.charCodeAt(i);
  return buf.buffer;
}

function bufferToBase64url(buf){
  const bytes = new Uint8Array(buf);
  let str = '';
  for(const b of bytes) str += String.fromCharCode(b);
  return btoa(str).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

async function loadPasskeys(){
  const res = await fetch('/api/webauthn/passkeys');
  if(!res.ok) return;
  const list = await res.json();
  const el = document.getElementById('passkeyList');
  el.innerHTML = '';
  if(!list.length){
    el.innerHTML = '<div class="empty-hint">还没有添加任何 Passkey</div>';
    return;
  }
  list.forEach(p => {
    const date = p.created_at ? new Date(p.created_at * 1000).toLocaleString() : '';
    el.insertAdjacentHTML('beforeend', `<div class="list-item panel-card" data-passkey-id="${escapeAttr(p.credential_id)}">
      <div><div>${escapeAttr(p.name)}</div><div class="caption">添加于 ${date}</div></div>
      <div class="actions"><button class="command btn-danger passkey-delete">删除</button></div>
    </div>`);
  });
  el.querySelectorAll('.passkey-delete').forEach(button => {
    button.onclick = () => deletePasskey(button.closest('[data-passkey-id]').dataset.passkeyId);
  });
}

async function deletePasskey(id){
  await fetch('/api/webauthn/passkeys/' + encodeURIComponent(id), {method: 'DELETE'});
  loadPasskeys();
}

async function addPasskey(){
  const hint = document.getElementById('passkeyHint');
  if(!window.PublicKeyCredential){
    hint.textContent = '当前浏览器不支持 Passkey';
    return;
  }
  const name = prompt('给这个 Passkey 起个名字（例如：我的 MacBook）', '') || '未命名 Passkey';
  hint.textContent = '等待设备验证…';
  try{
    const optRes = await fetch('/api/webauthn/register/options');
    const options = await optRes.json();
    if(!optRes.ok){ hint.textContent = options.error || '获取注册选项失败'; return; }

    options.challenge = base64urlToBuffer(options.challenge);
    options.user.id = base64urlToBuffer(options.user.id);
    if(options.excludeCredentials){
      options.excludeCredentials = options.excludeCredentials.map(c => ({...c, id: base64urlToBuffer(c.id)}));
    }

    const cred = await navigator.credentials.create({publicKey: options});
    const credential = {
      id: cred.id,
      rawId: bufferToBase64url(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: bufferToBase64url(cred.response.clientDataJSON),
        attestationObject: bufferToBase64url(cred.response.attestationObject),
      },
      clientExtensionResults: cred.getClientExtensionResults(),
    };

    const verifyRes = await fetch('/api/webauthn/register/verify', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({credential, name}),
    });
    const result = await verifyRes.json();
    if(!verifyRes.ok){ hint.textContent = result.error || '注册失败'; return; }
    hint.textContent = '添加成功 ✓';
    loadPasskeys();
  }catch(err){
    hint.textContent = '添加失败：' + err;
  }
}

// ---------- config load / save ----------
// Saving submits every field, so it stays disabled until the form holds the saved config:
// saving defaults (e.g. an unchecked "开启登录验证") would silently switch protections off.
function setSaveEnabled(on){ document.querySelectorAll('.save-btn').forEach(b => { b.disabled = !on; }); }

async function load(){
  setSaveEnabled(false);
  const res = await fetch('/api/config');
  if(!res.ok){ setSaveMsg('err', `读取配置失败（HTTP ${res.status}），刷新页面再试`); return; }
  const cfg = await res.json();

  const da = cfg.dashboard_auth || {};
  document.getElementById('authEnabled').checked = !!da.enabled;
  document.getElementById('authUsername').value = da.username || '';
  document.getElementById('authMaxAttempts').value = da.max_attempts || 5;
  document.getElementById('authLockout').value = da.lockout_seconds || 900;
  document.getElementById('authPasswordHint').textContent = da.has_password ? '已设置密码（留空保持不变）' : '还没设置密码';
  document.getElementById('ssoState').textContent = cfg.sso_enabled
    ? '已开启。谁能进、是哪一档在 Palintropic 那边分配；开着时总要登录，下面「开启登录验证」关了也一样。密码和 Passkey 是 SSO 连不上时的应急入口，登进来就是超级管理员，12 小时后要重新登录。'
    : '没开。在 config.yaml 的 sso 段填好 client 后开启（见 SETUP）。';

  const notif = cfg.notifications || {};
  const th = notif.thresholds || {};
  document.getElementById('thCpu').value = th.cpu ?? 90;
  document.getElementById('thMem').value = th.mem ?? 90;
  document.getElementById('thDisk').value = th.disk ?? 85;
  document.getElementById('cooldown').value = notif.cooldown_seconds ?? 600;

  const fs = notif.feishu || {};
  document.getElementById('feishuEnabled').checked = !!fs.enabled;
  document.getElementById('feishuHint').textContent = fs.webhook === SECRET_PLACEHOLDER ? '已设置 Webhook（留空保持不变）' : '还没设置';

  const em = notif.email || {};
  document.getElementById('emailEnabled').checked = !!em.enabled;
  document.getElementById('emailHost').value = em.smtp_host || '';
  document.getElementById('emailPort').value = em.smtp_port || 465;
  document.getElementById('emailSsl').checked = !!em.use_ssl;
  document.getElementById('emailTls').checked = !!em.use_tls;
  document.getElementById('emailUsername').value = em.username || '';
  document.getElementById('emailFrom').value = em.from || '';
  document.getElementById('emailTo').value = (em.to||[]).join(', ');
  document.getElementById('emailHint').textContent = em.password === SECRET_PLACEHOLDER ? '已设置授权码（留空保持不变）' : '还没设置';

  setSaveEnabled(true);

  // First run = nothing configured yet: no password and no data source (no agent has ever
  // reported, Nightcord-Status off). Checked after the form is filled, so it never holds up saving.
  let firstRun = false;
  if(!da.has_password && !cfg.nightcord_status_enabled){
    const agents = await fetch('/api/agents').then(r => r.ok ? r.json() : {agents: []}).catch(() => ({agents: []}));
    firstRun = !(agents.agents || []).length;
  }
  if(firstRun !== settings.firstRun){
    settings.firstRun = firstRun;
    if(typeof settingsChanged === 'function') settingsChanged();
  }
}

function setSaveMsg(cls, text){
  document.querySelectorAll('.save-msg').forEach(m => { m.className = 'save-msg ' + cls; m.textContent = text; });
}

async function save(){
  if(document.querySelector('.save-btn').disabled) return;
  setSaveEnabled(false);
  setSaveMsg('', '保存中…');
  try{
    // 保存是整份配置一起提交：各页的输入框都在页面里（只是不显示），哪一页点保存都一样
    const payload = {
      dashboard_auth: {
        enabled: document.getElementById('authEnabled').checked,
        username: document.getElementById('authUsername').value.trim(),
        new_password: document.getElementById('authPassword').value,
        max_attempts: parseInt(document.getElementById('authMaxAttempts').value) || 5,
        lockout_seconds: parseInt(document.getElementById('authLockout').value) || 900,
      },
      notifications: {
        cooldown_seconds: parseInt(document.getElementById('cooldown').value) || 600,
        thresholds: {
          cpu: parseInt(document.getElementById('thCpu').value) || 90,
          mem: parseInt(document.getElementById('thMem').value) || 90,
          disk: parseInt(document.getElementById('thDisk').value) || 85,
        },
        feishu: {
          enabled: document.getElementById('feishuEnabled').checked,
          webhook: document.getElementById('feishuWebhook').value.trim(),
        },
        email: {
          enabled: document.getElementById('emailEnabled').checked,
          smtp_host: document.getElementById('emailHost').value.trim(),
          smtp_port: parseInt(document.getElementById('emailPort').value) || 465,
          use_ssl: document.getElementById('emailSsl').checked,
          use_tls: document.getElementById('emailTls').checked,
          username: document.getElementById('emailUsername').value.trim(),
          password: document.getElementById('emailPassword').value,
          from: document.getElementById('emailFrom').value.trim(),
          to: document.getElementById('emailTo').value,
        },
      },
    };
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if(!res.ok){
      setSaveMsg('err', (data.errors || [data.error || '保存失败']).join('；'));
      setSaveEnabled(true);
      return;
    }
    setSaveMsg('ok', '已保存 ✓');
    document.getElementById('authPassword').value = '';
    document.getElementById('feishuWebhook').value = '';
    document.getElementById('emailPassword').value = '';
    await load();
  }catch(e){
    setSaveMsg('err', '保存失败：' + e);
    setSaveEnabled(true);
  }
}

// ---------- 青源机器 ----------
async function loadAgentList(){
  const res = await fetch('/api/agents');
  const data = await res.json();
  const wrap = document.getElementById('agentList');
  const agents = data.agents || [];
  if(!agents.length){
    wrap.innerHTML = '<div class="empty-hint">还没有机器上报过。</div>';
    return;
  }
  wrap.innerHTML = agents.map(a => `
    <div class="list-item" data-agent-name="${escapeAttr(a.name)}">
      <div>
        <div><span style="font:var(--type-base)">${escapeAttr(a.name)}</span>
          <span class="${a.online ? 'ok' : 'warn'}" style="margin-left:8px">${a.online ? '在线' : '离线'}</span></div>
        <div class="caption">最后一次上报：${escapeAttr(new Date(a.last_seen * 1000).toLocaleString())}</div>
      </div>
      <div class="actions"><button class="command btn-danger retire-btn">移除</button></div>
    </div>
  `).join('');
  wrap.querySelectorAll('.retire-btn').forEach(btn => {
    btn.onclick = () => retireAgent(btn.closest('[data-agent-name]').dataset.agentName);
  });
}

async function retireAgent(name){
  if(!confirm(`把「${name}」移出监控？之后它离线不会再告警。（不会卸载 agent，它要是又上报了会自动回来）`)) return;
  const res = await fetch(`/api/agents/${encodeURIComponent(name)}/retire`, {method: 'POST'});
  if(!res.ok){
    const data = await res.json().catch(() => ({}));
    alert('移除失败：' + (data.error || res.status));
  }
  await loadAgentList();
}

// ---------- 部署 ----------
async function loadAgentHosts(){
  const res = await fetch('/api/agent/hosts');
  const data = await res.json();
  const wrap = document.getElementById('agentHosts');
  const hosts = data.hosts || [];
  if(!hosts.length){
    wrap.innerHTML = '<div class="empty-hint">还没有记住任何服务器的密码。</div>';
    return;
  }
  wrap.innerHTML = hosts.map(h => `
    <div class="list-item" data-host-id="${escapeAttr(h.id)}">
      <div>
        <div style="font:var(--type-base)">${escapeAttr(h.panel_name || h.ip)}</div>
        <div class="caption">${escapeAttr(h.ssh_user)}@${escapeAttr(h.ip)}:${escapeAttr(h.port)}</div>
      </div>
      <div class="actions">
        <button class="command redeploy-btn">重新安装</button>
        <button class="command btn-danger delete-btn">删除</button>
      </div>
    </div>
  `).join('');
  wrap.querySelectorAll('.redeploy-btn').forEach(btn => {
    btn.onclick = () => redeployHost(btn.closest('[data-host-id]').dataset.hostId);
  });
  wrap.querySelectorAll('.delete-btn').forEach(btn => {
    btn.onclick = () => deleteHost(btn.closest('[data-host-id]').dataset.hostId);
  });
}

async function streamDeploy(url, options, msg, log, onDone){
  // /api/agent/deploy 和 .../redeploy 返回的是 NDJSON 流（一行一个 JSON），
  // 装的过程（建虚拟环境、装依赖）动辄一两分钟，边跑边把日志行 push 过来。
  msg.className = 'caption';
  msg.textContent = '安装中，可能需要一两分钟…';
  log.hidden = false;
  log.textContent = '';
  try{
    const res = await fetch(url, options);
    if(!res.ok || !res.body){
      const data = await res.json().catch(() => ({}));
      msg.className = 'err';
      msg.textContent = '安装失败：' + (data.error || `HTTP ${res.status}`);
      log.hidden = true;
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while(true){
      const {value, done} = await reader.read();
      if(done) break;
      buf += decoder.decode(value, {stream: true});
      let idx;
      while((idx = buf.indexOf('\n')) >= 0){
        const line = buf.slice(0, idx);
        buf = buf.slice(idx + 1);
        if(!line.trim()) continue;
        const evt = JSON.parse(line);
        if(evt.type === 'log'){
          log.textContent += evt.line + '\n';
          log.scrollTop = log.scrollHeight;
        } else if(evt.type === 'done'){
          if(evt.ok){
            msg.className = 'ok';
            msg.textContent = `安装成功 ✓（名字：${evt.panel_name}）`;
            if(onDone) await onDone(evt);
          } else {
            msg.className = 'err';
            msg.textContent = '安装失败：' + (evt.error || '未知错误');
          }
        }
      }
    }
  }catch(e){
    msg.className = 'err';
    msg.textContent = '请求失败：' + e;
  }
}

async function testFeishu(){
  const msg = document.getElementById('feishuTestMsg');
  msg.className = 'caption';
  msg.textContent = '发送中…';
  try{
    const res = await fetch('/api/notifications/test', {method: 'POST'});
    const data = await res.json();
    msg.className = data.ok ? 'ok' : 'err';
    msg.textContent = data.message || data.error || (data.ok ? '成功' : '失败');
  }catch(e){
    msg.className = 'err';
    msg.textContent = '请求失败：' + e;
  }
}

async function deployAgent(){
  const msg = document.getElementById('deployMsg');
  const log = document.getElementById('deployLog');
  const ip = document.getElementById('deployIp').value.trim();
  const port = parseInt(document.getElementById('deployPort').value) || 22;
  const ssh_user = document.getElementById('deployUser').value.trim() || 'root';
  const password = document.getElementById('deployPassword').value;
  const remember = document.getElementById('deployRemember').checked;
  if(!ip || !password){
    msg.className = 'err';
    msg.textContent = '服务器 IP 和密码不能为空';
    return;
  }
  await streamDeploy('/api/agent/deploy', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ip, port, ssh_user, password, remember}),
  }, msg, log, async () => {
    document.getElementById('deployPassword').value = '';
    await loadAgentHosts();
  });
}

async function redeployHost(id){
  const msg = document.getElementById('deployMsg');
  const log = document.getElementById('deployLog');
  await streamDeploy(`/api/agent/hosts/${encodeURIComponent(id)}/redeploy`, {method: 'POST'}, msg, log);
}

async function deleteHost(id){
  if(!confirm('删除这台服务器记住的密码？（不会卸载已经装好的 agent，只是以后不能一键重装了）')) return;
  await fetch(`/api/agent/hosts/${encodeURIComponent(id)}`, {method: 'DELETE'});
  await loadAgentHosts();
}
