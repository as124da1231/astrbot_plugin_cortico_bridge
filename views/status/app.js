/**
 * Coo 桥控制台
 *
 * 约束：页面跑在受限 iframe 里（sandbox 无 same-origin），拿不到 Dashboard 的
 * cookie / localStorage，也不能用 alert/confirm。所有后端调用都走
 * window.AstrBotPluginView，所有确认都在页面里自己做。
 */
const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

const PANELS = {
  status: {
    title: '概览',
    desc: '查看桥接状态和已配置的平台。',
  },
  connection: {
    title: '连接与令牌',
    desc: '管理 Coo 与 AstrBot 之间的连接。',
  },
  records: {
    title: '消息记录',
    desc: '查看桥接消息、会话和工具回执。',
  },
  instances: {
    title: '实例接入',
    desc: '选择一个机器人，管理它自己的接入与授权。',
  },
  tools: {
    title: '工具',
    desc: '选择 Coo 可以调用的 AstrBot 工具。',
  },
  images: {
    title: '图片',
    desc: '选择原图、视觉转述或关闭图片处理。',
  },
  voice: {
    title: '语音',
    desc: '配置语音转写和语音回复。',
  },
};

const RISK = {
  read: { label: '读', why: '只查询，不改变状态' },
  write: { label: '写', why: '会真的发消息、戳人或改状态' },
  exec: { label: '执行', why: '会跑代码或操作本机' },
};

const state = {
  panel: 'status',
  status: null,
  rows: [],
  msgFilter: 'all',
  msgInstance: '',
  toolLog: [],
  convs: [],
  refusals: { origin: 0, query: 0, token: 0, version: 0 },
  tools: [],
  wildcard: false,
  toolsTouched: false,
  picked: new Set(),
  saved: new Set(),
  query: '',
  risk: 'all',
  onlySel: false,
  collapsed: new Set(),
  used: new Set(),
  saving: false,
  toolsSaveGen: 0,
  tokenBusy: false,
  loaded: false,
  armClear: false,
  armTimer: 0,
  tokenArm: { action: '', timer: 0 },
};

/* ---------------- 主题 ---------------- */

// AstrBot 插件页 SDK 是否提供主题 API 未证实（DESIGN_REVIEW M13），
// 所以只在 getTheme 真的是函数时才问宿主；否则用系统偏好。
const darkMedia =
  typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: dark)')
    : null;
let hostTheme = false;
let paintedTheme = document.documentElement.dataset.theme || '';

function paintTheme(theme) {
  if (theme !== 'dark' && theme !== 'light') return;
  const root = document.documentElement;
  if (root.dataset.theme === theme) {
    paintedTheme = theme;
    return;
  }
  paintedTheme = theme;
  root.dataset.theme = theme;
}

function syncTheme() {
  // 宿主已经接管主题后不再抢着改回去。
  if (hostTheme) return;
  let wanted = '';
  try {
    wanted = new URLSearchParams(location.search).get('theme') || '';
  } catch (e) {
    wanted = '';
  }
  if (wanted === 'dark' || wanted === 'light') {
    paintTheme(wanted);
    return;
  }
  paintTheme(darkMedia && darkMedia.matches ? 'dark' : 'light');
}

function initTheme() {
  syncTheme();
  if (darkMedia && typeof darkMedia.addEventListener === 'function') {
    darkMedia.addEventListener('change', syncTheme);
  }
  // 宿主若直接改 data-theme，视为它接管；之后的系统主题变化不再覆盖。
  if (typeof MutationObserver === 'function') {
    const root = document.documentElement;
    new MutationObserver(() => {
      if ((root.dataset.theme || '') !== paintedTheme) hostTheme = true;
    }).observe(root, {
      attributes: true,
      attributeFilter: ['data-theme'],
    });
  }
}

async function adoptHostTheme() {
  if (!bridge || typeof bridge.getTheme !== 'function') return;
  // URL 显式指定时优先，用户/排障参数不被宿主覆盖。
  try {
    const forced = new URLSearchParams(location.search).get('theme');
    if (forced === 'dark' || forced === 'light') return;
  } catch (e) {
    // 拿不到 search 就继续问宿主。
  }
  try {
    const theme = await bridge.getTheme();
    if (theme === 'dark' || theme === 'light') {
      hostTheme = true;
      paintTheme(theme);
    }
  } catch (e) {
    // 宿主没实现或拒绝：保持系统偏好，不报错打扰用户。
  }
}


/* ---------------- 小工具 ---------------- */

function toast(msg, isErr) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, 2600);
}

function unwrap(v) {
  if (v && typeof v === 'object' && v.status === 'ok' && 'data' in v) return v.data;
  return v;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    // 受限 iframe 里可能没有 clipboard 权限，退回选中复制。
  }
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch (e) {
    return false;
  }
}

function ago(sec) {
  if (sec === undefined || sec === null || sec < 0) return '—';
  if (sec < 60) return `${sec} 秒前`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  return `${Math.floor(sec / 3600)} 小时前`;
}

function dur(sec) {
  if (!sec || sec < 0) return '';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h > 0) return `已运行 ${h} 小时 ${m} 分`;
  if (m > 0) return `已运行 ${m} 分`;
  return `已运行 ${Math.floor(sec)} 秒`;
}

function clock(ts) {
  const text = String(ts || '');
  return text.length >= 19 ? text.slice(11, 19) : '--:--:--';
}

function lampClass(s) {
  if (!s) return '';
  if (s.connected) return 'on';
  return s.listening ? 'idle' : 'off';
}

function convLabel(kind, id, name) {
  if (name) return name;
  if (kind === 'group') return `群 ${id}`;
  if (kind === 'private') return `私聊 ${id}`;
  return String(id || '');
}

/** 把一段文字拆成节点，命中搜索词的片段用 <mark> 包起来。 */
function marked(text, needle) {
  const raw = String(text ?? '');
  const q = String(needle || '').trim();
  if (!q) return [document.createTextNode(raw)];
  const lower = raw.toLowerCase();
  const key = q.toLowerCase();
  const out = [];
  let at = 0;
  while (at < raw.length) {
    const hit = lower.indexOf(key, at);
    if (hit < 0) break;
    if (hit > at) out.push(document.createTextNode(raw.slice(at, hit)));
    out.push(el('mark', null, raw.slice(hit, hit + key.length)));
    at = hit + key.length;
  }
  if (at < raw.length) out.push(document.createTextNode(raw.slice(at)));
  return out.length ? out : [document.createTextNode(raw)];
}

/* ---------------- 分页 ---------------- */

const fullModePanels = ['tools'];
const sharedPanels = ['images', 'voice', 'tools'];
const settingsLoaders = new Map();

function setPanel(name) {
  const meta = PANELS[name];
  if (!meta || (fullModePanels.includes(name) && state.status?.mode !== 'full')) return;
  state.panel = name;
  if (state.armClear && name !== 'tools') disarmClear();

  document.querySelectorAll('.navitem').forEach((btn) => {
    const on = btn.hasAttribute('data-shared-nav') ? sharedPanels.includes(name) : btn.dataset.panel === name;
    btn.classList.toggle('active', on);
    if (on) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  });
  for (const panel of Object.keys(PANELS)) $(`panel-${panel}`).hidden = name !== panel;
  $('btnRestart').hidden = name !== 'connection';
  $('btnReset').hidden = name !== 'records';
  $('actionsTools').hidden = name !== 'tools';

  if (name !== 'connection') clearOnceToken();

  const shared = sharedPanels.includes(name);
  $('sharedTabs').hidden = !shared;
  for (const tab of $('sharedTabs').querySelectorAll('[data-open-panel]')) {
    if (tab.dataset.openPanel === name) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  }
  $('pageTitle').textContent = shared ? '共享能力' : meta.title;
  $('pageDesc').textContent = shared ? '管理已接入实例共用的图片、语音和工具。' : meta.desc;
  $('scroll').scrollTop = 0;
  settingsLoaders.get(name)?.();

  if (name === 'tools') {
    if (!state.loaded) {
      loadTools().catch((e) => {
        $('toolLoading').hidden = true;
        toast('读取工具失败：' + (e.message || e), true);
      });
    } else {
      renderTools();
    }
  }

  if (name === 'voice') {
    loadVoice().catch((e) => toast('读取语音设置失败：' + (e.message || e), true));
  }

  if (name === 'instances') void loadInstances();

}

/* ---------------- token ---------------- */

const TOKEN_BTN = {
  copy: 'btnTokenCopy',
  rotate: 'btnTokenRotate',
  clear: 'btnTokenClear',
};
const TOKEN_LABEL = {
  copy: '复制',
  rotate: '生成（轮换）',
  clear: '清除',
};
const TOKEN_ARMED = {
  rotate: '再点一次生成',
  clear: '再点一次清除',
};

function disarmToken() {
  state.tokenArm.action = '';
  clearTimeout(state.tokenArm.timer);
  for (const [action, id] of Object.entries(TOKEN_BTN)) {
    const btn = $(id);
    if (!btn) continue;
    btn.classList.remove('armed');
    btn.textContent = TOKEN_LABEL[action];
  }
}

function armToken(action) {
  disarmToken();
  state.tokenArm.action = action;
  const btn = $(TOKEN_BTN[action]);
  if (btn) {
    btn.classList.add('armed');
    btn.textContent = TOKEN_ARMED[action];
  }
  state.tokenArm.timer = setTimeout(disarmToken, 4000);
}

function showOnceToken(token, hint) {
  const box = $('tokenOnceBox');
  const input = $('tokenOnce');
  if (!box || !input) return;
  if (!token) {
    clearOnceToken();
    return;
  }
  input.value = token;
  const line = $('tokenOnceHint');
  if (line) line.textContent = hint || '替换 Coo 里保存的旧 token，否则它会一直重连失败。';
  box.hidden = false;
  input.focus();
  input.select();
}

/** 一次性 token 用完就抹掉，别让它一直躺在 DOM 里。 */
function clearOnceToken() {
  const box = $('tokenOnceBox');
  const input = $('tokenOnce');
  if (input) {
    input.value = '';
    // 某些浏览器会把 value 属性留在属性表里，一并清掉。
    input.removeAttribute('value');
    input.blur();
  }
  if (box) box.hidden = true;
  const line = $('tokenOnceHint');
  if (line) line.textContent = '';
}

function setTokenBusy(busy) {
  state.tokenBusy = !!busy;
  for (const id of ['btnTokenRotate', 'btnTokenClear']) {
    const btn = $(id);
    if (btn) btn.disabled = !!busy;
  }
}

async function copyToken() {
  if (state.tokenBusy) return;
  clearOnceToken();
  let data;
  try {
    data = unwrap(await bridge.apiGet('token')) || {};
  } catch (err) {
    toast('读取 token 失败：' + (err && err.message ? err.message : err), true);
    return;
  }
  const plain = data.token || '';
  if (!plain) {
    toast(data.loopback ? '本机免密，还没有 token；点「生成」' : '还没有 token，点「生成」', true);
    return;
  }
  const ok = await copyText(plain);
  if (!ok) {
    showOnceToken(plain, '浏览器不允许自动复制，请手动选中这一行。');
    toast('复制失败：token 已显示在下方，手动复制即可', true);
    return;
  }
  toast('token 已复制');
}

async function rotateToken() {
  if (state.tokenBusy) return;
  disarmToken();
  const btn = $('btnTokenRotate');
  state.tokenBusy = true;
  setTokenBusy(true);
  if (btn) btn.textContent = '生成中…';
  try {
    const data = unwrap(await bridge.apiPost('token', { action: 'generate' })) || {};
    const plain = data.token || '';
    if (!plain) {
      toast('后端没有返回新 token', true);
      return;
    }
    showOnceToken(plain, '新 token 只显示这一次；Coo 需要用新 token 重连。');
    const ok = await copyText(plain);
    toast(
      ok
        ? '已生成新 token 并复制；当前 Coo 会断开，需要用新 token 重连'
        : '已生成新 token（显示在下方）；Coo 会断开，需要手动复制并重连',
      !ok,
    );
    await refresh().catch(() => toast('token 已更新，但刷新状态失败', true));
  } catch (err) {
    toast('生成 token 失败：' + (err && err.message ? err.message : err), true);
  } finally {
    state.tokenBusy = false;
    setTokenBusy(false);
    if (btn) btn.textContent = TOKEN_LABEL.rotate;
  }
}

async function clearToken() {
  if (state.tokenBusy) return;
  disarmToken();
  const btn = $('btnTokenClear');
  state.tokenBusy = true;
  setTokenBusy(true);
  if (btn) btn.textContent = '清除中…';
  try {
    await bridge.apiPost('token', { action: 'clear' });
    showOnceToken('', '');
    toast('token 已清除；Coo 会断开，本机仍可免密连接');
    await refresh().catch(() => toast('token 已清除，但刷新状态失败', true));
  } catch (err) {
    toast('清除 token 失败：' + (err && err.message ? err.message : err), true);
  } finally {
    state.tokenBusy = false;
    setTokenBusy(false);
    if (btn) btn.textContent = TOKEN_LABEL.clear;
  }
}

function initToken() {
  const onCopy = () => {
    disarmToken();
    copyToken();
  };
  $('btnTokenCopy').addEventListener('click', onCopy);
  $('kToken').addEventListener('click', onCopy);
  $('btnTokenOnceCopy').addEventListener('click', async () => {
    const input = $('tokenOnce');
    const token = input ? input.value : '';
    if (!token) return;
    const ok = await copyText(token);
    toast(ok ? 'token 已复制' : '复制失败，请手动选中', !ok);
  });

  $('btnTokenRotate').addEventListener('click', () => {
    if (state.tokenBusy) return;
    if (state.tokenArm.action === 'rotate') {
      disarmToken();
      rotateToken();
      return;
    }
    clearOnceToken();
    armToken('rotate');
    toast('生成新 token 会让当前 Coo 断开，需要用新 token 重连；再点一次确认', true);
  });
  $('btnTokenClear').addEventListener('click', () => {
    if (state.tokenBusy) return;
    if (state.tokenArm.action === 'clear') {
      disarmToken();
      clearToken();
      return;
    }
    clearOnceToken();
    armToken('clear');
    toast('清除后跨机连接会失效，Coo 需要重连；再点一次确认', true);
  });

  // 关页面/切走时把明文清掉，别留在 DOM 里。
  window.addEventListener('pagehide', clearOnceToken);
  window.addEventListener('beforeunload', clearOnceToken);
}

function applyStatus(s) {
  s = { ...(state.status || {}), ...s };
  state.status = s;
  if (state.panel === 'instances') updateInstanceSaveStatus();
  const routes = (s.platformRoutes || []).filter(route => route.enabled);
  const safe = routes.filter(route => route.mode === 'safe').length;
  const full = routes.filter(route => route.mode === 'full').length;
  const summary = routes.length ? [safe && safe + ' 个安全模式', full && full + ' 个完整模式'].filter(Boolean).join(' · ') : '暂无接入实例';
  $('bridgeMode').textContent = (summary || '查看实例接入') + (s.connected ? ' · 已同步 Coo' : ' · 等待 Coo 连接');
  const recordInstance = $('recordInstance');
  const reported = s.platformRoutes || [];
  recordInstance.replaceChildren(new Option('全部实例', ''), ...reported.map(item => new Option(
    (item.name || item.id) + ' · ' + adapterLabel(item), item.id)));
  recordInstance.value = state.msgInstance;
  if (!recordInstance.value && state.msgInstance) {
    state.msgInstance = '';
    recordInstance.value = '';
  }
  const directory = $('platformInstances');
  directory.replaceChildren(...(s.platformRoutes || []).map(route => {
    const card = el('button', 'platform-card');
    card.type = 'button';
    const name = el('span', 'platform-card-name', route.name || route.id);
    const status = el('span', 'platform-card-status', instanceStatus(route));
    status.classList.toggle('is-enabled', route.enabled);
    const info = el('span', 'platform-card-info', adapterLabel(route));
    card.addEventListener('click', () => openInstance(route.id));
    card.append(name, status, info);
    return card;
  }));
  if (!directory.hasChildNodes()) directory.textContent = '暂无 AstrBot 实例';
  for (const panel of fullModePanels) {
    const button = $('sharedTabs').querySelector('[data-open-panel="' + panel + '"]');
    if (button) button.hidden = s.mode !== 'full';
  }
  $('metricTools').hidden = s.mode !== 'full';
  if (s.mode !== 'full' && fullModePanels.includes(state.panel)) setPanel('status');
  $('btnVoiceSend').textContent = s.mode === 'safe' ? '发到主人私聊' : '发到最近的允许会话';

  const cls = lampClass(s);
  $('railLamp').className = 'lamp ' + cls;
  $('heroLamp').className = 'lamp big ' + cls;
  $('railDot').hidden = !s.connected;
  $('railDot').className = 'navdot ' + cls;

  const text = s.connected
    ? 'Coo 已连接'
    : s.listening
      ? '等待 Coo 连接'
      : s.listenError
        ? '监听失败'
        : '监听未开启';
  $('connText').textContent = text;
  $('railState').textContent = s.connected ? '已连接' : s.listening ? '等待连接' : s.listenError ? '监听失败' : '未监听';

  const versionBadge = $('versionBadge');
  if (versionBadge) {
    versionBadge.textContent = s.version ? 'v' + s.version : 'v—';
    versionBadge.title = s.version ? `Coo 桥 v${s.version}` : 'Coo 桥版本';
  }

  const headState = $('headState');
  if (headState) {
    headState.className =
      'headerstate ' + (s.connected ? 'on' : s.listening ? 'idle' : 'off');
    const label = headState.querySelector('span');
    if (label) {
      label.textContent = s.connected
        ? '已连接'
        : s.listening
          ? '等待连接'
          : s.listenError
            ? '监听失败'
            : '未连接';
    }
  }

  const parts = [];
  if (s.connected) {
    if (s.client) parts.push(s.client);
    parts.push(s.connectedFor ? `已连 ${Math.floor(s.connectedFor / 60) || '<1'} 分钟` : '刚刚连上');
    if (s.lastFrameAgo >= 0) parts.push(`最后一条帧 ${ago(s.lastFrameAgo)}`);
    else parts.push('还没有收到帧');
  } else if (s.listening) {
    parts.push(`${s.host || '0.0.0.0'}:${s.port || '—'}`);
    parts.push(dur(s.uptime) || '刚启动');
  } else if (s.listenError) {
    parts.push(`监听失败：${s.listenError}`);
  } else {
    parts.push('在「连接」页打开接管对话。');
  }
  $('connSub').textContent = parts.join(' · ');

  $('railAddr').textContent = `${s.host || '0.0.0.0'}:${s.port || '—'}`;
  $('railAddr').title = 'Cortico 扩展里要填 ws://<这台机器的地址>:' + (s.port || '');

  const st = s.stats || {};
  $('mPushed').textContent = st.pushed || 0;
  $('mSent').textContent = st.sent || 0;
  $('mFailed').textContent = st.failed || 0;
  $('mTools').textContent = s.toolCount || 0;
  // 工具页正在编辑时，不要把未保存的已选数覆盖成后端数字。
  if (!(state.panel === 'tools' && state.loaded)) {
    $('railTools').textContent = s.toolCount ? s.toolCount : '—';
  }

  // /status 只给 tokenSet，明文一律按需走 /token（UI-002）。
  const tokenSet = !!s.tokenSet;
  const loopback = !!s.loopback;
  const tokenNode = $('kToken');
  if (tokenNode) {
    tokenNode.textContent = tokenSet ? '已设置 · 点击复制' : loopback ? '本机免密' : '未设置';
    tokenNode.classList.toggle('on', tokenSet);
    tokenNode.title = tokenSet
      ? '点击复制当前 token'
      : loopback
        ? '本机免密：只有本机能连；点「生成」可以跨机使用'
        : '非回环监听必须设置 token；点「生成」';
  }

  const tokenNote = $('kTokenNote');
  if (tokenNote) {
    const noteText = tokenSet
      ? loopback
        ? '只在本机使用；浏览器已被 Origin 拦住'
        : '非回环监听，必须与 Coo 一致'
      : loopback
        ? '本机免密；生成后可以跨机使用'
        : '非回环必须设置 token';
    // aria-live 区域只在内容真的变化时更新，避免 20 秒轮询反复朗读。
    if (tokenNote.textContent !== noteText) tokenNote.textContent = noteText;
  }

  const listen = $('kListen');
  if (listen) listen.textContent = `${s.host || '—'}:${s.port || '—'}`;
  const listenNote = $('kListenNote');
  if (listenNote) {
    listenNote.textContent = s.connected
      ? 'Cortico 已连接'
      : s.listening
        ? '等待连接'
        : s.listenError || '未监听';
  }
  state.refusals = {
    origin: s.originRefused || 0,
    query: s.queryTokenRefused || 0,
    token: s.badTokenRefused || 0,
    version: s.versionRefused || 0,
  };
  renderRefusals();

  if (Array.isArray(s.recent)) {
    state.rows = s.recent.slice().reverse();
    renderRows();
  }
  if (Array.isArray(s.toolLog)) state.toolLog = s.toolLog;
  state.convs = s.conversations || [];
  renderConvs();
  renderToolLog();
}

function renderRows() {
  const host = $('rows');
  host.textContent = '';
  const filter = state.msgFilter;
  const shown = state.rows.filter((row) => {
    if (state.msgInstance && row.umo?.split(':', 1)[0] !== state.msgInstance) return false;
    if (filter === 'in') return row.dir !== 'out';
    if (filter === 'out') return row.dir === 'out';
    if (filter === 'at') return !!row.mentioned;
    return true;
  });

  for (const row of shown) {
    const item = el('div', 'msg' + (row.dir === 'out' ? ' out' : '') + (row.notice ? ' notice' : ''));

    const time = el('span', 'mtime', clock(row.ts));
    time.title = row.ts || '';

    const pill = el('span', 'pill');
    if (row.dir === 'out') {
      pill.classList.add('out');
      if (row.image) pill.classList.add('img');
      pill.textContent = row.image ? '发图' : '发出';
    } else if (row.notice) {
      pill.textContent = '通知';
    } else if (row.mentioned) {
      pill.classList.add('at');
      pill.textContent = '叫到';
    } else {
      pill.textContent = '收到';
    }

    const body = el('div', 'mbody');
    const meta = el('div', 'mmeta');
    const conv = String(row.conv || '');
    const sep = conv.indexOf(':');
    const kind = conv.slice(0, sep);
    const id = conv.slice(sep + 1);
    meta.appendChild(
      el('b', null, row.dir === 'out' ? 'Coo' : row.notice ? '群通知' : (row.sender || '未知')),
    );
    if (row.owner && !row.notice && row.dir !== 'out') {
      meta.appendChild(el('span', 'who owner', '· 主人'));
    }
    const service = {'qq': 'QQ', 'discord': 'Discord', 'wecom_ai_bot': '企微'}[row.channel] || row.channel || 'AstrBot';
    const ch = service + (kind === 'private' ? ' 私聊' : ' 群或频道');
    meta.appendChild(
      el('span', 'who', `· ${kind === 'private' ? ch : `${ch} ${convLabel(kind, id, row.convName)}`}`),
    );
    const text = el('div', 'mtext');
    text.textContent = row.text || '';

    body.append(meta);
    if (row.umo) {
      const wrap = el('div', 'mumo');
      const umo = el('button', 'umobtn');
      umo.type = 'button';
      umo.title = `点击复制 ${row.umo}`;
      umo.setAttribute('aria-label', `复制会话标识 ${row.umo}`);
      umo.appendChild(el('span', 'umotext', row.umo));
      umo.addEventListener('click', async () => {
        const ok = await copyText(row.umo);
        toast(ok ? '会话标识已复制' : '复制失败，请重试', !ok);
      });
      wrap.appendChild(umo);
      body.appendChild(wrap);
    }
    body.appendChild(text);
    item.append(time, pill, body);
    host.appendChild(item);
  }

  $('empty').hidden = state.rows.length > 0;
  $('msgCount').textContent = filter === 'all'
    ? `${state.rows.length} 条`
    : `${shown.length} / ${state.rows.length} 条`;
}

function pushRow(row) {
  state.rows.unshift(row);
  if (state.rows.length > 60) state.rows.length = 60;
  renderRows();
}

function renderToolLog() {
  const host = $('toolLog');
  host.textContent = '';
  const log = state.toolLog || [];
  state.used = new Set(log.map((x) => x.name).filter(Boolean));
  $('toolLogEmpty').hidden = log.length > 0;

  for (const item of log.slice(0, 8)) {
    const row = el('div', 'tlrow' + (item.ok ? '' : ' bad'));
    const dot = el('span', 'tldot');
    const main = el('div', 'tlmain');
    main.appendChild(el('div', 'tlname', item.name || '?'));
    const where = [item.kind === 'private' ? '私聊' : item.kind === 'group' ? '群' : '', item.conv]
      .filter(Boolean)
      .join(' ');
    const who = [item.sender, where].filter(Boolean).join(' · ');
    if (who) main.appendChild(el('div', 'tlmeta', who));
    main.appendChild(el('div', 'tltext', item.text || (item.ok ? '（没有文字返回）' : '失败')));
    const time = el('span', 'tltime', clock(item.ts));
    time.title = item.ts || '';
    row.append(dot, main, time);
    host.appendChild(row);
  }
}

function renderRefusals() {
  const node = $('kRefused');
  if (!node) return;
  const r = state.refusals;
  const parts = [];
  const add = (label, n) => { if (n) parts.push(`${label} ${n}`); };
  add('浏览器', r.origin);
  add('URL', r.query);
  add('token', r.token);
  add('版本', r.version);
  node.textContent = parts.length ? parts.join(' · ') : '没有';
  node.classList.toggle('bad', !!(r.token || r.version));
}

function renderConvs() {
  const host = $('convLog');
  if (!host) return;
  host.textContent = '';
  const list = state.convs || [];
  $('convLogEmpty').hidden = list.length > 0;

  for (const c of list.slice(0, 5)) {
    const row = el('div', 'clogrow');
    row.title = c.umo || c.key || '';
    row.appendChild(el('span', 'clogkind', c.kind === 'group' ? '群' : c.kind === 'private' ? '私聊' : '—'));
    const main = el('div', 'clogmain');
    main.appendChild(el('div', 'clogname', c.name || c.id || c.umo || '—'));
    main.appendChild(el('div', 'clogid', c.umo || ''));
    row.appendChild(main);
    row.appendChild(el('span', 'clogtime', ago(c.age_sec)));
    host.appendChild(row);
  }
}

async function refresh() {
  const s = unwrap(await bridge.apiGet('status'));
  applyStatus(s);
}

const stream = {
  handle: null,
  timer: 0,
  watch: 0,
  tries: 0,
  gen: 0,
  state: 'idle',
};

function setLive(mode, detail) {
  stream.state = mode;
  const band = $('liveband');
  const label = $('liveLabel');
  const text = mode === 'live' ? '实时' : mode === 'retry' ? '重连中' : '已断开';
  if (label) label.textContent = text;
  if (band) {
    band.dataset.live = mode;
    band.title =
      detail ||
      (mode === 'live'
        ? '实时流已连上'
        : mode === 'retry'
          ? '实时流断开，5 秒后自动重连；期间靠 20 秒轮询兜底'
          : '实时流已断开，仍在重试；期间靠 20 秒轮询兜底');
  }
  if ($('msgCount')) $('msgCount').title = band ? band.title : '';
}

/** 连上或收到帧：清掉重连定时器，状态回到“实时”。 */
function markStreamLive() {
  clearTimeout(stream.timer);
  stream.timer = 0;
  clearTimeout(stream.watch);
  stream.tries = 0;
  if (stream.state !== 'live') setLive('live');
}

function closeHandle(handle) {
  if (!handle) return;
  try {
    if (typeof handle.abort === 'function') handle.abort();
    else if (typeof handle.close === 'function') handle.close();
    else if (typeof handle === 'function') handle();
  } catch (e) {
    // 关不掉也没关系：重连前只求别在原对象上再挂监听。
  }
}

function closeStreamHandle() {
  const handle = stream.handle;
  stream.handle = null;
  closeHandle(handle);
}

/** 只有能 abort/close 的返回值才算句柄；其余靠 gen 忽略旧回调。 */
function abortable(handle) {
  return (
    !!handle &&
    (typeof handle.abort === 'function' ||
      typeof handle.close === 'function' ||
      typeof handle === 'function')
  );
}

function streamDown(detail) {
  stream.tries += 1;
  clearTimeout(stream.watch);
  closeStreamHandle();
  setLive(stream.tries > 2 ? 'off' : 'retry', detail);
  clearTimeout(stream.timer);
  stream.timer = setTimeout(connectStream, 5000);
}

function connectStream() {
  clearTimeout(stream.timer);
  stream.timer = 0;
  clearTimeout(stream.watch);
  closeStreamHandle();

  const gen = ++stream.gen;
  let handle;
  try {
    handle = bridge.subscribeSSE('events', {
      onOpen() {
        if (gen !== stream.gen) return;
        markStreamLive();
        // 断线期间可能漏了事件，连上后补一次状态（失败不影响实时流）。
        refresh().catch(() => {});
      },
      onMessage(ev) {
        if (gen !== stream.gen) return;
        markStreamLive();
        const d = ev.parsed;
        if (!d || typeof d !== 'object') return;

        if (d.type === 'snapshot') {
          applySnapshot(d);
          return;
        }
        if (d.type === 'message') {
          // 通知（撤回 / 戳一戳 / 入退群）也走这里，notice 字段决定流水样式。
          pushRow(d);
          return;
        }
        if (d.type === 'tool') {
          state.toolLog.unshift(d);
          if (state.toolLog.length > 12) state.toolLog.length = 12;
          renderToolLog();
          if (state.panel === 'tools' && state.loaded) renderTools();
          return;
        }
        if (d.type === 'mode') {
          refresh().catch(() => {});
          return;
        }
        if (d.type === 'connected' || d.type === 'disconnected' || d.type === 'listening' || d.type === 'info') {
          toast(d.text || d.type, d.type === 'disconnected');
          refresh().catch(() => {});
        }
      },
      onError() {
        if (gen !== stream.gen) return;
        // 有的 SDK 会自己重连：先给 6 秒恢复窗口，没恢复再走 5 秒退避重连。
        setLive('retry', '实时流断开，正在尝试恢复；期间靠 20 秒轮询兜底');
        if (stream.timer) return;
        stream.timer = setTimeout(() => {
          if (gen === stream.gen && stream.state !== 'live') {
            streamDown('实时流已断开，5 秒后重连；期间靠 20 秒轮询兜底');
          }
        }, 6000);
      },
    });
  } catch (err) {
    streamDown('实时流连不上，5 秒后重试；期间靠 20 秒轮询兜底');
    return;
  }

  // 只有真的能 abort/close 的返回值才保存成句柄；拿到的不是句柄时
  // 不保存（避免误以为是可关闭对象），旧流的回调由 gen 计数忽略。
  if (abortable(handle)) {
    stream.handle = handle;
  } else if (handle && typeof handle.then === 'function') {
    handle
      .then((resolved) => {
        if (gen !== stream.gen) {
          closeHandle(resolved);
          return;
        }
        if (abortable(resolved)) stream.handle = resolved;
      })
      .catch(() => {
        if (gen === stream.gen) streamDown('实时流已断开，5 秒后重连；期间靠 20 秒轮询兜底');
      });
  }
  // 没等到 onOpen 也没收到帧时，别一直显示“实时”。
  stream.watch = setTimeout(() => {
    if (gen === stream.gen && stream.state === 'idle') {
      setLive('retry', '实时流还没连上，正在等待；期间靠 20 秒轮询兜底');
    }
  }, 8000);
}

function applySnapshot(snap) {
  if (Array.isArray(snap.recent)) {
    state.rows = snap.recent.slice().reverse();
    renderRows();
  }
  if (Array.isArray(snap.toolLog)) {
    state.toolLog = snap.toolLog;
    renderToolLog();
  }
  if (snap.stats) {
    const st = snap.stats;
    $('mPushed').textContent = st.pushed || 0;
    $('mSent').textContent = st.sent || 0;
    $('mFailed').textContent = st.failed || 0;
  }
  if (typeof snap.originRefused === 'number') state.refusals.origin = snap.originRefused;
  if (typeof snap.queryTokenRefused === 'number') state.refusals.query = snap.queryTokenRefused;
  if (typeof snap.badTokenRefused === 'number') state.refusals.token = snap.badTokenRefused;
  if (typeof snap.versionRefused === 'number') state.refusals.version = snap.versionRefused;
  if (['originRefused', 'queryTokenRefused', 'badTokenRefused', 'versionRefused']
    .some((k) => typeof snap[k] === 'number')) {
    renderRefusals();
  }
  if (Array.isArray(snap.conversations)) {
    state.convs = snap.conversations;
    renderConvs();
  }
  if (typeof snap.listening === 'boolean') {
    const note = $('kListenNote');
    if (note) {
      note.textContent = snap.connected
        ? 'Cortico 已连接'
        : snap.listening
          ? '等待连接'
          : snap.listenError || '未监听';
    }
  }
  if (typeof snap.connected === 'boolean') {
    const cls = lampClass(snap);
    $('railLamp').className = 'lamp ' + cls;
    $('heroLamp').className = 'lamp big ' + cls;
    $('railDot').hidden = !snap.connected;
    $('railDot').className = 'navdot ' + cls;
  }
}

async function act(path, label) {
  try {
    await bridge.apiPost(path, {});
    toast(label);
    await refresh();
  } catch (e) {
    toast(e.message || String(e), true);
  }
}

function initMessages() {
  $('recordInstance').addEventListener('change', event => {
    state.msgInstance = event.target.value;
    renderRows();
  });
  $('msgFilters').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    state.msgFilter = chip.dataset.msg;
    document.querySelectorAll('#msgFilters .chip').forEach((c) => {
      c.classList.toggle('on', c === chip);
      c.setAttribute('aria-pressed', c === chip ? 'true' : 'false');
    });
    renderRows();
  });
}

/* ---------------- 工具 ---------------- */

function dirty() {
  if (state.picked.size !== state.saved.size) return true;
  for (const name of state.picked) if (!state.saved.has(name)) return true;
  return false;
}

/** 保存期间用快照对比，判断用户有没有在等响应时继续勾选。 */
function toolsSnapshot() {
  return JSON.stringify({
    picked: [...state.picked].sort(),
    wildcard: state.wildcard,
    touched: state.toolsTouched,
  });
}

function activeTools() {
  return state.tools.filter((t) => t.active);
}

function riskCount(risk) {
  return activeTools().filter((t) => t.risk === risk).length;
}

function updateSavebar() {
  const on = dirty();
  state.armClear && !on && disarmClear();
  $('savebar').hidden = !on;
  $('panel-tools').classList.toggle('dirty', on);
  $('dirtyText').textContent = `有未保存的改动 · 已选 ${state.picked.size} 个`;
}

function updateToolHero() {
  const active = activeTools();
  const total = active.length;
  const picked = active.filter((t) => state.picked.has(t.name)).length;
  $('pickedCount').textContent = picked;
  $('totalCount').textContent = total;
  $('railTools').textContent = String(picked);

  // 已选只统计真实会下发的工具（inactive 不计，`*` 下的 exec 也不在 picked）。
  const parts = { read: 0, write: 0, exec: 0 };
  for (const t of active) {
    if (state.picked.has(t.name)) parts[t.risk] = (parts[t.risk] || 0) + 1;
  }
  const sum = parts.read + parts.write + parts.exec;
  for (const [key, id] of [['read', 'mixRead'], ['write', 'mixWrite'], ['exec', 'mixExec']]) {
    $(id).style.width = sum ? `${(parts[key] / sum) * 100}%` : '0%';
  }

  const legend = $('mixLegend');
  legend.textContent = '';
  for (const key of ['read', 'write', 'exec']) {
    const chip = el('span', 'chip dot');
    chip.appendChild(el('span', `swatch ${key}`));
    chip.appendChild(document.createTextNode(`${RISK[key].label} ${parts[key]}`));
    chip.title = RISK[key].why;
    legend.appendChild(chip);
  }

  const note = $('toolNote');
  if (note) {
    note.hidden = !state.wildcard;
    note.textContent = state.wildcard
      ? '已自动授权在「已审查的工具风险」中标为只读或写入的工具。编辑勾选后，保存为手动名单。'
      : '';
  }

  $('cAll').textContent = total;
  $('cRead').textContent = riskCount('read');
  $('cWrite').textContent = riskCount('write');
  $('cExec').textContent = riskCount('exec');
}

function visibleTools() {
  const q = state.query.trim().toLowerCase();
  return state.tools.filter((t) => {
    if (state.onlySel && !state.picked.has(t.name)) return false;
    if (state.risk !== 'all' && t.risk !== state.risk) return false;
    if (!q) return true;
    return (
      t.name.toLowerCase().includes(q) ||
      (t.desc || '').toLowerCase().includes(q) ||
      (t.sig || '').toLowerCase().includes(q) ||
      (t.plugin || '').toLowerCase().includes(q) ||
      (t.plugin_label || '').toLowerCase().includes(q)
    );
  });
}

function toolRow(t) {
  const row = el('label', 'trow' + (t.active ? '' : ' off'));

  const box = document.createElement('input');
  box.type = 'checkbox';
  box.checked = state.picked.has(t.name);
  box.disabled = !t.active;
  box.setAttribute('aria-label', `${t.name}${t.active ? '' : '（已停用）'}`);

  const knob = el('span', 'tswitch');
  knob.setAttribute('aria-hidden', 'true');

  const info = el('div', 'tinfo');
  const line = el('div', 'tline');
  const name = el('code', 'tname');
  name.append(...marked(t.name, state.query));
  line.appendChild(name);

  if (t.sig) {
    const sig = el('span', 'tsig', `(${t.sig})`);
    sig.title = '参数：必填原名，可选带 ?';
    line.appendChild(sig);
  }

  const risk = el('span', `riskchip ${t.risk}`, (RISK[t.risk] || {}).label || t.risk);
  risk.title = (RISK[t.risk] || {}).why || '';
  line.appendChild(risk);

  if (t.active && state.wildcard && t.risk === 'exec' && !state.picked.has(t.name)) {
    const tag = el('span', 'offtag wildtag', '需显式加入');
    tag.title = '「*」只包含已审查的读写工具';
    line.appendChild(tag);
  }

  if (!t.active) line.appendChild(el('span', 'offtag', '已停用'));
  else if (state.used.has(t.name)) {
    const used = el('span', 'usedtag', '用过');
    used.title = '这次启动后 Coo 调用过';
    line.appendChild(used);
  }

  const desc = el('div', 'tdesc');
  desc.title = t.desc || '';
  desc.append(...marked(t.desc || '（没有说明）', state.query));

  info.append(line, desc);
  row.append(box, knob, info);

  if (t.active) {
    box.addEventListener('change', () => {
      state.toolsTouched = true;
      if (box.checked) state.picked.add(t.name);
      else state.picked.delete(t.name);
      if (state.onlySel) renderTools();
      else {
        updateToolHero();
        updateSavebar();
        updateGroupCounts();
      }
    });
  }
  return row;
}

function groupTitle(t) {
  const label = (t.plugin_label || '').trim();
  if (label) return label;
  return (t.plugin || '未知来源').replace(/^astrbot_plugin_/, '').replace(/_/g, ' ');
}

function groupDir(t) {
  const dir = (t.plugin || '').replace(/^astrbot_plugin_/, '');
  return dir && dir !== groupTitle(t) ? dir : '';
}

function groupTint(key) {
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  return ['var(--accent)', 'var(--ink-blue)', 'var(--warn)', 'var(--violet)'][hash % 4];
}

function updateGroupCounts() {
  document.querySelectorAll('.tgroup').forEach((card) => {
    const plugin = card.dataset.plugin;
    const tools = state.tools.filter((t) => t.plugin === plugin);
    const active = tools.filter((t) => t.active);
    const picked = active.filter((t) => state.picked.has(t.name)).length;
    const badge = card.querySelector('.gcount');
    if (badge) {
      badge.textContent = '';
      badge.appendChild(el('b', null, String(picked)));
      badge.appendChild(document.createTextNode(`/${active.length}`));
      badge.classList.toggle('zero', picked === 0);
    }
    const act = card.querySelector('.gact');
    if (act) {
      const all = active.length > 0 && active.every((t) => state.picked.has(t.name));
      act.textContent = all ? '取消整组' : '整组选中';
      act.disabled = active.length === 0;
    }
  });
}

function renderTools() {
  $('toolLoading').hidden = true;
  if (state.loaded) {
    $('btnClearFilters').hidden = false;
    $('toolEmpty').querySelector('.emptymark').textContent = '◍';
    $('toolEmpty').querySelector('.emptytitle').textContent = '没有符合条件的工具';
    $('toolEmpty').querySelector('.muted').textContent = '换个关键词，或把筛选放宽。';
  }
  updateToolHero();

  const shown = visibleTools();
  const groups = new Map();
  for (const t of shown) {
    if (!groups.has(t.plugin)) groups.set(t.plugin, []);
    groups.get(t.plugin).push(t);
  }

  const host = $('toolGroups');
  host.textContent = '';
  $('toolEmpty').hidden = !state.loaded || shown.length > 0;
  if (!shown.length) {
    updateSavebar();
    return;
  }

  for (const [plugin, tools] of groups) {
    const card = el('section', 'sheet tgroup');
    card.dataset.plugin = plugin;

    const head = el('div', 'ghead');
    const btn = el('button', 'gbtn');
    btn.type = 'button';
    const filtering = !!state.query.trim() || state.risk !== 'all' || state.onlySel;
    const closed = !filtering && state.collapsed.has(plugin);
    const chev = el('span', 'chev' + (closed ? ' closed' : ''));
    chev.setAttribute('aria-hidden', 'true');
    btn.appendChild(chev);
    const label = groupTitle(tools[0]);
    const glyph = el('span', 'gglyph', label.trim().slice(0, 1) || '?');
    glyph.style.setProperty('--glyph', groupTint(plugin));
    glyph.setAttribute('aria-hidden', 'true');
    btn.appendChild(glyph);
    btn.appendChild(el('span', 'gname', label));
    const dir = groupDir(tools[0]);
    if (dir) btn.appendChild(el('code', 'gdir', dir));

    const count = el('span', 'gcount');
    const active = tools.filter((t) => t.active);
    const picked = active.filter((t) => state.picked.has(t.name)).length;
    count.appendChild(el('b', null, String(picked)));
    count.appendChild(document.createTextNode(`/${active.length}`));
    if (picked === 0) count.classList.add('zero');
    btn.appendChild(count);

    btn.setAttribute('aria-expanded', closed ? 'false' : 'true');
    btn.addEventListener('click', () => {
      if (filtering) return; // 搜索/筛选时一直展开，收起会挡住刚找到的东西
      const body = card.querySelector('.gbody');
      const nowClosed = !body.hidden;
      body.hidden = nowClosed;
      chev.classList.toggle('closed', nowClosed);
      btn.setAttribute('aria-expanded', nowClosed ? 'false' : 'true');
      if (nowClosed) state.collapsed.add(plugin);
      else state.collapsed.delete(plugin);
    });

    const act = el('button', 'linkbtn gact');
    act.type = 'button';
    const all = active.length > 0 && active.every((t) => state.picked.has(t.name));
    act.textContent = all ? '取消整组' : '整组选中';
    act.disabled = active.length === 0;
    act.addEventListener('click', () => {
      const turnOn = !active.every((t) => state.picked.has(t.name));
      state.toolsTouched = true;
      for (const t of active) {
        if (turnOn) state.picked.add(t.name);
        else state.picked.delete(t.name);
      }
      renderTools();
    });

    head.append(btn, act);
    card.appendChild(head);

    const body = el('div', 'gbody');
    body.hidden = closed;
    for (const t of tools) body.appendChild(toolRow(t));
    card.appendChild(body);
    host.appendChild(card);
  }

  updateSavebar();
}

async function loadTools() {
  $('toolLoading').hidden = false;
  $('toolGroups').textContent = '';
  $('toolEmpty').hidden = true;
  let data;
  try {
    data = unwrap(await bridge.apiGet('tools'));
  } catch (e) {
    $('toolLoading').hidden = true;
    const box = $('toolEmpty');
    box.hidden = false;
    box.querySelector('.emptymark').textContent = '!';
    box.querySelector('.emptytitle').textContent = '读取工具失败';
    box.querySelector('.muted').textContent = e.message || String(e);
    $('btnClearFilters').hidden = true;
    throw e;
  }
  state.tools = Array.isArray(data.items) ? data.items : [];
  const sel = Array.isArray(data.selected) ? data.selected : [];
  const activeNames = new Set(activeTools().map((t) => t.name));
  // `granted` 才是实际下发的名字：`*` 的 read/write + 显式 exec，inactive 不在内。
  state.wildcard = !!data.all || sel.includes('*');
  const fallback = state.wildcard
    ? activeTools().filter((t) => t.risk !== 'exec').map((t) => t.name)
    : sel;
  const granted = Array.isArray(data.granted) ? data.granted : fallback;
  state.saved = new Set(granted.filter((name) => activeNames.has(name)));
  state.picked = new Set(state.saved);
  state.toolsTouched = false;
  state.loaded = true;
  $('railTools').textContent = String(state.picked.size);
  renderTools();
}

async function saveTools() {
  if (state.saving || !dirty()) return;
  state.saving = true;
  const gen = ++state.toolsSaveGen;
  const snapshot = toolsSnapshot();
  const btn = $('btnSave');
  btn.disabled = true;
  btn.textContent = '保存中…';
  const wasWildcard = state.wildcard;
  // 没手动增删时继续提交 ["*"]，未来新工具仍会自动包含；动过就只能落显式清单。
  const useWildcard = state.wildcard && !state.toolsTouched;
  const payload = useWildcard ? ['*'] : [...state.picked];
  try {
    const data = unwrap(await bridge.apiPost('tools', { tools: payload })) || {};
    if (gen !== state.toolsSaveGen) return;
    // 保存期间又勾过：以用户当前选择为准，不拿响应覆盖，也不清 dirty。
    if (toolsSnapshot() !== snapshot) {
      toast('保存期间有改动，请再保存一次', true);
      return;
    }
    const selected = Array.isArray(data.selected) ? data.selected : payload;
    const granted = Array.isArray(data.granted) ? data.granted : [...state.picked];
    const activeNames = new Set(activeTools().map((t) => t.name));
    state.wildcard = selected.includes('*');
    state.saved = new Set(granted.filter((name) => activeNames.has(name)));
    state.picked = new Set(state.saved);
    state.toolsTouched = false;
    renderTools();
    if (wasWildcard && !state.wildcard) {
      toast(`已改为显式清单：${state.picked.size} 个工具；新装的工具不会自动加入`);
    } else {
      toast(`已保存 ${state.picked.size} 个工具，已同步给 Coo`);
    }
    if (state.status) {
      state.status.toolCount = state.picked.size;
      $('mTools').textContent = state.picked.size;
    }
  } catch (e) {
    if (gen === state.toolsSaveGen) toast('保存失败：' + (e.message || e), true);
  } finally {
    if (gen === state.toolsSaveGen) {
      state.saving = false;
      btn.disabled = false;
      btn.textContent = '保存并同步';
    }
  }
}

function disarmClear() {
  state.armClear = false;
  clearTimeout(state.armTimer);
  const btn = $('btnClear');
  btn.classList.remove('armed');
  btn.textContent = '清空选择';
}

function armClear() {
  const btn = $('btnClear');
  state.armClear = true;
  btn.classList.add('armed');
  btn.textContent = '再点一次清空';
  clearTimeout(state.armTimer);
  state.armTimer = setTimeout(disarmClear, 3200);
}

/* ---------------- 群聊 / 私聊 ---------------- */

function initTools() {
  $('toolSearch').addEventListener('input', (e) => {
    state.query = e.target.value;
    renderTools();
  });

  $('riskChips').addEventListener('click', (e) => {
    const chip = e.target.closest('.segbtn');
    if (!chip) return;
    state.risk = chip.dataset.risk;
    document.querySelectorAll('#riskChips .segbtn').forEach((c) => c.classList.toggle('on', c === chip));
    renderTools();
  });

  $('onlySel').addEventListener('change', (e) => {
    state.onlySel = e.target.checked;
    renderTools();
  });

  $('btnClearFilters').addEventListener('click', () => {
    state.query = '';
    state.risk = 'all';
    state.onlySel = false;
    $('toolSearch').value = '';
    $('onlySel').checked = false;
    document.querySelectorAll('#riskChips .segbtn').forEach((c) => c.classList.toggle('on', c.dataset.risk === 'all'));
    renderTools();
  });

  $('btnSave').addEventListener('click', saveTools);

  $('btnRevert').addEventListener('click', () => {
    state.picked = new Set(state.saved);
    state.toolsTouched = false;
    renderTools();
    toast('已回到上次保存的选择');
  });

  $('btnReadOnly').addEventListener('click', () => {
    const read = state.tools.filter((t) => t.risk === 'read' && t.active);
    if (!read.length) {
      toast('这个 AstrBot 上没有只读工具');
      return;
    }
    let added = 0;
    for (const t of read) {
      if (!state.picked.has(t.name)) {
        state.picked.add(t.name);
        added += 1;
      }
    }
    if (added) state.toolsTouched = true;
    renderTools();
    toast(added ? `已加入 ${added} 个只读工具，记得保存` : '只读工具已经都在里面了');
  });

  $('btnClear').addEventListener('click', () => {
    if (!state.picked.size) {
      toast('现在就是空的');
      return;
    }
    if (!state.armClear) {
      armClear();
      return;
    }
    disarmClear();
    state.picked.clear();
    state.toolsTouched = true;
    renderTools();
    toast('已清空，保存后生效');
  });

  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    const typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';

    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      if (state.panel !== 'tools') return;
      e.preventDefault();
      saveTools();
      return;
    }

    if (e.key === '/' && !typing) {
      e.preventDefault();
      if (state.panel !== 'tools') setPanel('tools');
      $('toolSearch').focus();
      $('toolSearch').select();
      return;
    }

    if (e.key === 'Escape') {
      if (state.panel !== 'tools' || !state.query) return;
      state.query = '';
      $('toolSearch').value = '';
      renderTools();
      $('toolSearch').blur();
    }
  });
}

/* ---------------- 语音 ---------------- */

let voiceInfo = null;

function voiceMsg(text, bad) {
  const node = $('voiceMsg');
  node.textContent = text || '';
  node.className = 'msgline' + (bad ? ' bad' : '');
}

function providerText(info) {
  if (!info || !info.id) return '未配置';
  const model = info.model && info.model !== info.id ? ` · ${info.model}` : '';
  return `${info.id}${model}`;
}

function fillProviders(select, items, currentId) {
  select.textContent = '';
  const blank = document.createElement('option');
  blank.value = '';
  blank.textContent = '跟随 AstrBot 当前 Provider';
  select.appendChild(blank);
  for (const item of items) {
    const opt = document.createElement('option');
    opt.value = item.id;
    opt.textContent = item.id;
    opt.title = item.model && item.model !== item.id ? `${item.id} · ${item.model}` : item.id;
    select.appendChild(opt);
  }
  select.value = items.some((i) => i.id === currentId) ? currentId : '';
}

function renderVoice(data) {
  voiceInfo = data;
  const stt = data.stt || {};
  const tts = data.tts || {};
  const hasStt = !!stt.id;
  const hasTts = !!tts.id;
  const providers = data.providers || { tts: [], stt: [] };

  $('sttName').textContent = providerText(stt);
  if (!hasStt) {
    $('sttPill').className = 'pill bad';
    $('sttPill').textContent = '未配置';
  } else if (!data.sttEnabled) {
    $('sttPill').className = 'pill';
    $('sttPill').textContent = '已关闭';
  } else {
    $('sttPill').className = 'pill ok';
    $('sttPill').textContent = '可用';
  }

  $('ttsName').textContent = providerText(tts);
  if (!hasTts) {
    $('ttsPill').className = 'pill bad';
    $('ttsPill').textContent = '未配置';
  } else if (!data.enabled) {
    $('ttsPill').className = 'pill';
    $('ttsPill').textContent = '已关闭';
  } else {
    $('ttsPill').className = 'pill ok';
    $('ttsPill').textContent = '可用';
  }

  fillProviders($('ttsProvider'), providers.tts || [], data.ttsProviderId || '');
  fillProviders($('sttProvider'), providers.stt || [], data.sttProviderId || '');
  $('ttsProvider').disabled = !(providers.tts || []).length;
  $('sttProvider').disabled = !(providers.stt || []).length;

  $('sttEnabled').checked = !!data.sttEnabled;
  $('sttEnabled').disabled = !(providers.stt || []).length;

  $('ttsMaxChars').value = data.maxChars ?? 120;

  $('ttsEnabled').checked = !!data.enabled;
  $('ttsEnabled').disabled = !hasTts;
  $('btnVoicePreview').disabled = !hasTts;
  $('btnVoiceSend').disabled = !hasTts;
  $('ttsText').disabled = !hasTts;
}

async function saveVoiceField(payload, okText) {
  try {
    await bridge.apiPost('voice', payload);
    await loadVoice();
    toast(okText);
  } catch (e) {
    toast('保存失败：' + (e.message || e), true);
    await loadVoice();
  }
}

async function loadVoice() {
  const data = unwrap(await bridge.apiGet('voice'));
  renderVoice(data);
}

async function saveVoiceEnabled(enabled) {
  try {
    await bridge.apiPost('voice', { enabled });
    if (voiceInfo) voiceInfo.enabled = enabled;
    toast(enabled ? '已允许 Coo 用语音说话' : '已关闭语音输出');
  } catch (e) {
    $('ttsEnabled').checked = !enabled;
    toast('保存失败：' + (e.message || e), true);
  }
}

async function previewVoice() {
  const text = $('ttsText').value.trim();
  if (!text) {
    voiceMsg('先写一句要读的话。', true);
    return;
  }
  const btn = $('btnVoicePreview');
  btn.disabled = true;
  voiceMsg('合成中…');
  try {
    const data = unwrap(await bridge.apiPost('voice/preview', { text }));
    const player = $('voicePlayer');
    player.src = `data:${data.mime || 'audio/mpeg'};base64,${data.audio}`;
    player.hidden = false;
    try {
      await player.play();
      voiceMsg(`已生成 ${data.chars || text.length} 字的语音`);
    } catch (err) {
      const name = err && err.name ? err.name : '';
      if (name === 'NotAllowedError') {
        voiceMsg(`已生成 ${data.chars || text.length} 字的语音，点播放器试听。`);
      } else {
        voiceMsg('音频已生成，但浏览器不能播放这个格式；可以发到 QQ 试听。', true);
      }
    }
  } catch (e) {
    voiceMsg(e.message || String(e), true);
  } finally {
    btn.disabled = false;
  }
}

async function sendVoiceTest() {
  const text = $('ttsText').value.trim();
  if (!text) {
    voiceMsg('先写一句要读的话。', true);
    return;
  }
  const btn = $('btnVoiceSend');
  btn.disabled = true;
  voiceMsg('发送中…');
  try {
    const data = unwrap(await bridge.apiPost('voice/test', { text }));
    if (data.voice) voiceMsg('已发出试听语音。');
    else voiceMsg(data.note || '没有发出语音。', true);
  } catch (e) {
    voiceMsg(e.message || String(e), true);
  } finally {
    btn.disabled = false;
  }
}

function initVoice() {
  $('ttsEnabled').addEventListener('change', (e) => saveVoiceEnabled(e.target.checked));
  $('ttsMaxChars').addEventListener('change', async (e) => {
    if (!e.target.reportValidity()) return;
    e.target.disabled = true;
    try {
      await saveVoiceField({ max_chars: e.target.valueAsNumber }, '已保存语音字数上限');
    } finally {
      e.target.disabled = false;
    }
  });
  $('ttsProvider').addEventListener('change', (e) =>
    saveVoiceField({ tts_provider_id: e.target.value }, e.target.value ? '已指定 TTS Provider' : '已改回 AstrBot 当前 TTS Provider'),
  );
  $('sttProvider').addEventListener('change', (e) =>
    saveVoiceField({ stt_provider_id: e.target.value }, e.target.value ? '已指定 STT Provider' : '已改回 AstrBot 当前 STT Provider'),
  );
  $('sttEnabled').addEventListener('change', (e) =>
    saveVoiceField({ stt_enabled: e.target.checked }, e.target.checked ? '已打开语音转写' : '已关闭语音转写'),
  );
  $('btnVoicePreview').addEventListener('click', previewVoice);
  $('btnVoiceSend').addEventListener('click', sendVoiceTest);
}

function mountManagedSettings(panel, id, endpoint, { fill, read, confirm, afterSave }) {
  const form = $(id);
  const fields = form.querySelector('[data-settings-fields]');
  const message = form.querySelector('[data-settings-message]');
  const retry = form.querySelector('[data-settings-retry]');
  const save = form.querySelector('[type=submit]');
  const consent = form.querySelector('[data-setting-confirm]');
  const checkbox = consent?.querySelector('input');
  let baseline;
  let busy = false;
  const requiresConfirmation = draft => !!confirm?.(draft, baseline);
  const sync = () => {
    if (!baseline) return;
    const draft = read();
    const changed = JSON.stringify(draft) !== JSON.stringify(baseline);
    const needsConfirm = changed && requiresConfirmation(draft);
    if (consent) consent.hidden = !needsConfirm;
    save.disabled = busy || !changed || needsConfirm && !checkbox?.checked;
  };
  const show = data => {
    fill(data);
    baseline = read();
    if (checkbox) checkbox.checked = false;
    fields.hidden = false;
    retry.hidden = true;
  };
  const load = async () => {
    if (baseline || busy) return;
    busy = true;
    retry.hidden = true;
    message.textContent = '正在读取设置…';
    try {
      show(unwrap(await bridge.apiGet(endpoint)));
      message.textContent = '';
    } catch (error) {
      message.textContent = `读取失败：${error.message || error}`;
      retry.hidden = false;
    } finally {
      busy = false;
      sync();
    }
  };
  settingsLoaders.set(panel, load);
  retry.addEventListener('click', load);
  form.addEventListener('input', event => {
    if (checkbox && event.target !== checkbox) checkbox.checked = false;
    sync();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || save.disabled || !form.reportValidity()) return;
    busy = true;
    fields.disabled = true;
    sync();
    try {
      const data = unwrap(await bridge.apiPost(endpoint, { ...read(), expected: baseline, confirmed: checkbox?.checked === true }));
      show(data);
      message.textContent = '已保存。';
      afterSave?.(data);
      void refresh().catch(() => {});
    } catch (error) {
      message.textContent = `保存失败：${error.message || error}`;
    } finally {
      fields.disabled = false;
      busy = false;
      sync();
    }
  });
}

const settingLines = input => input.value.split('\n').map(value => value.trim()).filter(Boolean);

function selectedSettingsChip(label, remove) {
  const chip = el('button', 'chip', `${label} ×`);
  chip.type = 'button';
  chip.setAttribute('aria-label', `移除 ${label}`);
  chip.addEventListener('click', remove);
  return chip;
}

function initManagedSettings() {
  const local = '127.0.0.1';
  const remote = '0.0.0.0';
  const hostMode = $('settingHostMode');
  hostMode.addEventListener('change', () => { $('settingHostRow').hidden = hostMode.value !== 'custom'; });
  mountManagedSettings('connection', 'connectionSettings', 'settings/connection', {
    fill(data) {
      $('settingEnabled').checked = data.enabled;
      hostMode.value = data.host === local ? 'local' : data.host === remote ? 'remote' : 'custom';
      $('settingHost').value = data.host;
      $('settingHostRow').hidden = hostMode.value !== 'custom';
      $('settingPort').value = data.port;
      $('settingMaxSends').value = data.maxSends;
    },
    read: () => ({
      enabled: $('settingEnabled').checked,
      host: hostMode.value === 'local' ? local : hostMode.value === 'remote' ? remote : $('settingHost').value.trim(),
      port: $('settingPort').valueAsNumber, maxSends: $('settingMaxSends').valueAsNumber,
    }),
    confirm: (next, old) => (next.enabled !== old.enabled || next.host !== old.host || next.port !== old.port)
      && (old.enabled || next.enabled && ![local, 'localhost', '::1', '[::1]'].includes(next.host.toLowerCase())),
  });

  const imageMode = $('settingImageMode');
  const vision = $('settingVision');
  const syncVision = () => { $('settingVisionRow').hidden = !['auto', 'describe'].includes(imageMode.value); };
  imageMode.addEventListener('change', syncVision);
  mountManagedSettings('images', 'imageSettings', 'settings/images', {
    fill(data) {
      imageMode.value = data.mode;
      const providers = new Set(data.providers);
      if (data.provider) providers.add(data.provider);
      vision.replaceChildren(new Option('自动选择', ''), ...[...providers].map(id => new Option(id, id)));
      vision.value = data.provider;
      syncVision();
    },
    read: () => ({ mode: imageMode.value, provider: vision.value }),
  });

  const riskPicker = $('settingRiskTool');
  const riskForm = $('toolRiskSettings');
  let risks = new Map();
  const drawRisks = () => $('settingRiskList').replaceChildren(...[...risks].sort(([a], [b]) => a.localeCompare(b)).map(([name, risk]) =>
    selectedSettingsChip(`${name} · ${RISK[risk].label}`, () => {
      risks.delete(name);
      drawRisks();
      riskForm.dispatchEvent(new Event('input', { bubbles: true }));
    })));
  $('addRiskTool').addEventListener('click', () => {
    if (!riskPicker.value) return;
    risks.set(riskPicker.value, $('settingRiskLevel').value);
    drawRisks();
    riskForm.dispatchEvent(new Event('input', { bubbles: true }));
  });
  mountManagedSettings('tools', 'toolRiskSettings', 'tools/risks', {
    fill(data) {
      risks = new Map(Object.entries(data.risks));
      $('settingReviewedTools').checked = data.autoReviewed;
      riskPicker.replaceChildren(new Option('请选择工具', ''), ...data.tools.map(name => new Option(name, name)));
      drawRisks();
    },
    read: () => ({
      entries: [...risks].map(([name, risk]) => `${name}:${risk}`).sort(),
      autoReviewed: $('settingReviewedTools').checked,
    }),
    confirm: () => true,
    afterSave() {
      if (!state.toolsTouched) void loadTools().catch(() => {});
    },
  });
}

const instanceFields = [
  { key: 'enabled', label: '接入 Coo', type: 'check', slot: 'instanceEnable' },
  { key: 'mode', label: '接入模式', choices: { safe: '安全模式', full: '完整模式' }, slot: 'instanceModeField' },
  { key: 'owner_id', label: '主人用户 ID', type: 'owner', slot: 'instanceOwnerField' },
  { key: 'group_scope', label: '接入范围', choices: { allowlist: '仅指定会话', all: '全部群聊和频道' }, slot: 'instanceGroupFields' },
  { key: 'group_mode', label: '唤醒方式', choices: { mention: '仅被叫到', all: '全部消息' }, slot: 'instanceGroupFields' },
  { key: 'group_allow', label: '已放行的会话', type: 'list', kind: 'group', slot: 'instanceGroupFields' },
  { key: 'aliases', label: '唤醒称呼（每行一个）', type: 'list', slot: 'instanceGroupFields' },
  { key: 'private_mode', label: '私聊范围', choices: { owner: '仅主人私聊', allowlist: '仅指定私聊', all: '所有私聊', off: '关闭' }, slot: 'instancePrivateFields' },
  { key: 'private_deny', label: '未放行的私聊', choices: { astrbot: '交回 AstrBot', ignore: '静默处理' }, slot: 'instancePrivateFields' },
  { key: 'private_allow', label: '已放行的私聊', type: 'list', kind: 'private', slot: 'instancePrivateFields' },
  { key: 'desktop_tools', label: '授权桌面使用该实例的主人工具', type: 'check', slot: 'instanceDesktopFields' },
  { key: 'desktop_targets', label: '桌面主动发送目标', type: 'list', kind: 'all', slot: 'instanceDesktopFields' },
];

const adapterNames = { aiocqhttp: 'QQ', discord: 'Discord', wecom_ai_bot: '企业微信', telegram: 'Telegram' };

function adapterLabel(route) {
  return adapterNames[route.adapterType] || route.adapterType || route.channel || 'AstrBot';
}

function instanceStatus(row) {
  if (row.astrbotEnabled === false) return '机器人已停用';
  if (row.replySupported === false) return '无法原会话回复';
  if (!row.enabled) return '待接入';
  return row.mode === 'full' ? '完整模式' : row.mode === 'safe' ? '安全模式' : '已接入';
}

let instanceTab = 'access';
let requestedInstance = '';
let instanceData;
let instanceLoading = false;
let instanceDirty = false;
let instanceSaving = false;
let instanceSaved = false;
const instanceControls = new Map();

function selectedInstance() {
  return instanceData?.instances.find(row => row.id === $('instancePick').value);
}

function updateInstanceSaveStatus() {
  if (!instanceSaved || instanceDirty || instanceSaving) return;
  $('instanceFeedback').textContent = state.status?.connected
    ? '已保存 · Coo 权限已同步' : '已保存 · 等待 Coo 同步实例权限';
}

function setInstanceDirty(dirty) {
  instanceDirty = dirty;
  $('instanceSave').disabled = !dirty || instanceSaving;
  $('instanceDiscard').disabled = !dirty || instanceSaving;
  $('instancePick').disabled = dirty || instanceSaving;
  for (const button of $('instanceDirectory').querySelectorAll('button')) button.disabled = dirty || instanceSaving;
  $('instanceDraft').textContent = instanceSaving ? '正在保存…' : dirty ? '有未保存的修改' : '';
  if (dirty) instanceSaved = false;
}

function showInstanceTab(name) {
  instanceTab = name;
  for (const button of $('instanceTabs').querySelectorAll('[data-instance-tab]')) {
    const active = button.dataset.instanceTab === name;
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
    $('instance-pane-' + button.dataset.instanceTab).hidden = !active;
  }
}

function syncInstanceFields() {
  const full = instanceControls.get('mode')?.value === 'full';
  $('instanceTabs').hidden = !full;
  if (!full) showInstanceTab('access');
  $('instanceModeHint').textContent = full
    ? '已放行的会话共用 Coo 主对话。'
    : '仅接入已绑定的主人私聊。';
  instanceControls.get('group_allow')?.closest('.form-field').toggleAttribute('hidden', instanceControls.get('group_scope')?.value !== 'allowlist');
  instanceControls.get('private_allow')?.closest('.form-field').toggleAttribute('hidden', instanceControls.get('private_mode')?.value !== 'allowlist');
  for (const button of $('instanceTabs').querySelectorAll('[data-instance-tab]')) {
    button.hidden = !full && button.dataset.instanceTab !== 'access';
  }
}

function pickInstance(id) {
  if (instanceDirty || instanceSaving || !instanceData?.instances.some(row => row.id === id)) return;
  $('instancePick').value = id;
  instanceSaved = false;
  $('instanceFeedback').textContent = '';
  showInstanceTab('access');
  drawInstance();
}

function openInstance(id) {
  if (!instanceDirty && !instanceSaving) requestedInstance = id;
  setPanel('instances');
}

function drawInstanceDirectory() {
  const selected = selectedInstance();
  const rows = instanceData?.instances ?? [];
  $('instanceCount').textContent = rows.length + ' 个实例';
  $('instanceSearchRow').hidden = rows.length < 7;
  const query = rows.length < 7 ? '' : $('instanceSearch').value.trim().toLowerCase();
  const matches = rows.filter(row => !query ||
    [row.name, row.id, adapterLabel(row), row.adapterType].some(value => value?.toLowerCase().includes(query)));
  $('instanceDirectory').replaceChildren(...matches.map(row => {
    const item = el('button', 'instance-directory-item');
    item.type = 'button';
    item.disabled = instanceDirty || instanceSaving;
    item.dataset.instance = row.id;
    if (row.id === selected?.id) item.setAttribute('aria-current', 'true');
    const text = el('span', 'instance-directory-copy');
    text.append(el('strong', '', row.name || row.id),
      el('small', '', adapterLabel(row) + (row.name && row.name !== row.id ? ' · ' + row.id : '')));
    item.append(text, el('span', 'instance-state-dot' + (row.enabled ? ' on' : ''), instanceStatus(row)));
    item.addEventListener('click', () => pickInstance(row.id));
    return item;
  }));
  if (query && !matches.length) $('instanceDirectory').append(el('p', 'instance-notice', '没有匹配的实例。'));
}

function renderInstanceTargets(field, row, item, control) {
  const picker = el('select', 'kvselect');
  picker.setAttribute('aria-label', '添加到' + field.label);
  const sessions = row.sessions.filter(session => field.kind === 'all' || session.kind === field.kind);
  picker.replaceChildren(new Option(sessions.length ? '从最近会话添加' : '暂无最近会话', ''),
    ...sessions.map(session => new Option(session.umo, session.umo)));
  picker.disabled = picker.options.length < 2;
  const list = el('ul', 'instance-targets');
  list.setAttribute('aria-label', field.label);
  const draw = () => {
    const values = settingLines(control);
    list.replaceChildren(...values.map(value => {
      const entry = el('li', 'instance-target');
      const separator = value.indexOf(':', value.indexOf(':') + 1);
      const address = el('code', '', separator > 0 ? value.slice(separator + 1) : value);
      address.title = value;
      const remove = el('button', 'instance-remove', '移除');
      remove.type = 'button';
      remove.setAttribute('aria-label', '移除会话 ' + address.textContent);
      remove.addEventListener('click', () => {
        control.value = settingLines(control).filter(other => other !== value).join('\n');
        control.dispatchEvent(new Event('input', { bubbles: true }));
        const next = list.querySelector('button') || item.querySelector('summary');
        next?.focus();
      });
      entry.append(address, remove);
      return entry;
    }));
    list.hidden = values.length === 0;
  };
  picker.addEventListener('change', () => {
    if (!picker.value) return;
    control.value = [...new Set([...settingLines(control), picker.value])].join('\n');
    picker.value = '';
    control.dispatchEvent(new Event('input', { bubbles: true }));
  });
  control.addEventListener('input', draw);
  const manual = el('details', 'instance-manual');
  manual.append(el('summary', '', '手动编辑会话 UMO'), control);
  item.append(list, picker, manual);
  draw();
}

function drawInstance() {
  const row = selectedInstance();
  $('instanceForm').hidden = !row;
  for (const slot of new Set(instanceFields.map(field => field.slot))) $(slot).replaceChildren();
  instanceControls.clear();
  drawInstanceDirectory();
  setInstanceDirty(false);
  if (!row) return;
  const platform = adapterLabel(row);
  $('instanceMark').textContent = platform === 'QQ' ? 'QQ' : platform.slice(0, 1).toUpperCase();
  $('instanceTitle').textContent = row.name || row.id;
  $('instanceMeta').textContent = [platform, row.name && row.id !== row.name ? row.id : '', instanceStatus(row)].filter(Boolean).join(' · ');
  $('instanceState').textContent = row.ownerUmo ? '已绑定主人私聊'
    : row.policy.owner_id ? '等待该账号向此机器人发一条私聊。' : '指定主人后，由该账号发一条私聊完成绑定。';
  $('instanceOwnerDetails').hidden = !row.ownerUmo;
  $('instanceOwnerDetails').open = false;
  $('instanceOwnerAddress').textContent = row.ownerUmo || '';

  const capability = $('instanceCapabilities');
  capability.textContent = row.capabilities?.reply === false
    ? '这个机器人使用固定 Webhook 回复。可在 AstrBot 机器人设置中改用原会话回复。'
    : row.capabilities?.replyTextOnly ? '这个实例支持原会话文字回复。' : '';
  capability.hidden = !capability.textContent;

  for (const field of instanceFields) {
    const value = row.policy[field.key];
    const id = 'instanceField-' + field.key;
    const item = el('div', 'form-field');
    const label = el('label', '', field.label);
    label.htmlFor = id;
    let control;
    if (field.choices) {
      control = el('select', 'kvselect');
      control.replaceChildren(...Object.entries(field.choices).map(([key, text]) => new Option(text, key)));
    } else if (field.type === 'list') {
      control = el('textarea', 'kvselect mono');
      control.rows = 3;
      control.placeholder = field.kind ? '每行一个完整 UMO' : '';
      item.classList.add('instance-field-wide');
    } else {
      control = el('input', field.type === 'check' ? '' : 'kvselect');
      control.type = field.type === 'check' ? 'checkbox' : 'text';
      if (field.type === 'owner') {
        const list = el('datalist');
        list.id = 'instanceOwnerOptions';
        list.replaceChildren(...(instanceData.admins || []).map(admin => new Option(admin, admin)));
        control.setAttribute('list', list.id);
        control.autocomplete = 'off';
        item.append(list);
      }
    }
    control.id = id;
    instanceControls.set(field.key, control);
    if (field.type === 'check') {
      control.checked = value;
      label.className = 'cortico-switchrow instance-toggle';
      const graphic = el('span', 'cortico-switch');
      graphic.setAttribute('aria-hidden', 'true');
      label.replaceChildren(el('span', '', field.label), control, graphic);
      item.append(label);
      if (field.key === 'desktop_tools') item.classList.add('instance-field-wide');
    } else {
      control.value = Array.isArray(value) ? value.join('\n') : value;
      if (field.key === 'mode') label.className = 'visually-hidden';
      item.append(label);
      if (field.kind) renderInstanceTargets(field, row, item, control);
      else item.append(control);
    }
    if (field.key === 'desktop_targets' && row.capabilities?.sessionSend === false) item.hidden = true;
    $(field.slot).append(item);
  }
  showInstanceTab(instanceTab);
  syncInstanceFields();
}

async function loadInstances(force = false) {
  if (instanceLoading) return;
  if (instanceDirty && !force) {
    $('instanceFeedback').textContent = '保存当前修改后可刷新会话。';
    return;
  }
  instanceLoading = true;
  const pick = $('instancePick');
  const previous = requestedInstance || pick.value;
  try {
    instanceData = unwrap(await bridge.apiGet('instances'));
    pick.replaceChildren(...instanceData.instances.map(row => new Option(
      (row.name !== row.id ? row.name + ' · ' : '') + row.id + ' · ' + row.adapterType, row.id)));
    if (instanceData.instances.some(row => row.id === previous)) pick.value = previous;
    if (requestedInstance) showInstanceTab('access');
    requestedInstance = '';
    $('instanceFeedback').textContent = instanceData.instances.length ? '' : '请先在 AstrBot「机器人」中添加实例。';
    drawInstance();
  } catch (error) {
    $('instanceFeedback').textContent = '读取实例失败 · ' + (error.message || error);
  } finally {
    instanceLoading = false;
  }
}

function initInstances() {
  $('instanceSearch').addEventListener('input', drawInstanceDirectory);
  $('instancePick').addEventListener('change', () => pickInstance($('instancePick').value));
  for (const button of $('instanceTabs').querySelectorAll('[data-instance-tab]')) {
    button.addEventListener('click', () => showInstanceTab(button.dataset.instanceTab));
  }
  $('instanceReload').addEventListener('click', () => void loadInstances());
  $('instanceDiscard').addEventListener('click', () => {
    $('instanceFeedback').textContent = '';
    drawInstance();
  });
  $('instanceForm').addEventListener('input', () => {
    setInstanceDirty(true);
    syncInstanceFields();
  });
  $('instanceForm').addEventListener('submit', async event => {
    event.preventDefault();
    const row = selectedInstance();
    const button = $('instanceSave');
    if (!row || button.disabled) return;
    const policy = Object.fromEntries(instanceFields.map(field => {
      const c = instanceControls.get(field.key);
      return [field.key, field.type === 'check' ? c.checked : field.type === 'list' ? settingLines(c) : c.value.trim()];
    }));
    const changed = Object.keys(policy).some(key => JSON.stringify(policy[key]) !== JSON.stringify(row.policy[key]));
    if (!changed) return;
    instanceSaving = true;
    setInstanceDirty(true);
    const confirm = policy.enabled && (!row.policy.enabled || row.policy.owner_id !== policy.owner_id || policy.mode === 'full');
    try {
      if (confirm) {
        const box = $('instanceConfirm');
        $('instanceConfirmText').textContent = policy.mode === 'full'
          ? row.id + ' 放行的会话将共用 Coo 主对话，回复可能提到桌面对话。勾选的桌面授权也会生效。'
          : row.id + ' 将接入指定主人的私聊，并与 Coo 桌面共用主对话。';
        const accepted = await new Promise(resolve => {
          const events = new AbortController();
          const settle = accepted => {
            events.abort();
            if (box.open) box.close();
            resolve(accepted);
          };
          $('instanceAccept').addEventListener('click', () => settle(true), { signal: events.signal });
          $('instanceCancel').addEventListener('click', () => settle(false), { signal: events.signal });
          box.addEventListener('cancel', event => {
            event.preventDefault();
            settle(false);
          }, { signal: events.signal });
          box.showModal();
        });
        if (!accepted) return;
      }
      if (selectedInstance()?.id !== row.id) return;
      await bridge.apiPost('instances', { instance: row.id, version: row.version, policy, confirmed: confirm });
      setInstanceDirty(false);
      await loadInstances(true);
      instanceSaved = true;
      await refresh();
    } catch (error) {
      $('instanceFeedback').textContent = '保存失败 · ' + (error.message || error);
    } finally {
      instanceSaving = false;
      setInstanceDirty(instanceDirty);
      updateInstanceSaveStatus();
    }
  });
}

/* ---------------- 启动 ---------------- */

async function main() {
  try {
    await bridge.ready();
  } catch (e) {
    toast('面板桥接没就绪：' + (e.message || e), true);
    return;
  }

  // 宿主主题只做能力检测；没有就不碰用户/系统选择（UI-001）。
  adoptHostTheme();

  document.querySelectorAll('[data-panel], [data-open-panel]').forEach((btn) => {
    btn.addEventListener('click', () => setPanel(btn.dataset.panel || btn.dataset.openPanel));
  });

  $('btnRestart').addEventListener('click', () => act('restart', '已重开监听'));
  $('metricTools').addEventListener('click', () => setPanel('tools'));
  $('btnReset').addEventListener('click', async () => {
    try {
      await bridge.apiPost('reset', {});
      state.rows = [];
      state.toolLog = [];
      renderRows();
      renderToolLog();
      toast('已清空统计和流水');
      await refresh();
    } catch (e) {
      toast(e.message || String(e), true);
    }
  });

  initMessages();
  initTools();
  initToken();
  initVoice();
  initManagedSettings();
  initInstances();

  // 状态读取失败只影响状态卡，不该让整场没有实时流（UI-007）。
  try {
    await refresh();
  } catch (e) {
    toast('读取状态失败：' + (e.message || e), true);
  }
  connectStream();

  renderRows();
  renderToolLog();

  const requested = new URLSearchParams(location.search).get('panel');
  const wanted = ['qq', 'discord', 'groups', 'private'].includes(requested) ? 'instances' : requested;
  if (wanted && PANELS[wanted]) setPanel(wanted);

  // 20 秒轮询兜底：SSE 断了也能把状态和流水补回来。
  setInterval(() => {
    refresh().catch(() => {});
  }, 20000);
}

initTheme();
main();
