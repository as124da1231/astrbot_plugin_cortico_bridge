/**
 * Cortico 桥 · 控制台
 *
 * 约束：页面跑在受限 iframe 里（sandbox 无 same-origin），拿不到 Dashboard 的
 * cookie / localStorage，也不能用 alert/confirm。所有后端调用都走
 * window.AstrBotPluginView，所有确认都在页面里自己做。
 */
const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

const PANELS = {
  status: {
    eyebrow: 'CORTICO BRIDGE · 概览',
    title: '概览',
    desc: 'Cortico 连着没有，消息都在往哪儿走。',
  },
  tools: {
    eyebrow: 'CORTICO BRIDGE · 工具',
    title: '工具',
    desc: '决定 Coo 能借用 AstrBot 的哪些手；保存后立刻同步给 Coo。',
  },
  voice: {
    eyebrow: 'CORTICO BRIDGE · 语音',
    title: '语音',
    desc: '听和说都借 AstrBot 的语音能力；Coo 只在值得开口时用语音。',
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
  toolLog: [],
  tools: [],
  picked: new Set(),
  saved: new Set(),
  query: '',
  risk: 'all',
  onlySel: false,
  collapsed: new Set(),
  used: new Set(),
  saving: false,
  loaded: false,
  armClear: false,
  armTimer: 0,
};

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

function setPanel(name) {
  state.panel = name;
  if (state.armClear && name !== 'tools') disarmClear();
  const meta = PANELS[name];

  document.querySelectorAll('.navitem').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.panel === name);
    btn.setAttribute('aria-selected', btn.dataset.panel === name ? 'true' : 'false');
  });
  $('panel-status').hidden = name !== 'status';
  $('panel-tools').hidden = name !== 'tools';
  $('panel-voice').hidden = name !== 'voice';
  $('actionsStatus').hidden = name !== 'status';
  $('actionsTools').hidden = name !== 'tools';

  $('eyebrow').textContent = meta.eyebrow;
  $('pageTitle').textContent = meta.title;
  $('pageDesc').textContent = meta.desc;
  $('scroll').scrollTop = 0;

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
}

/* ---------------- 概览 ---------------- */

function setPill(node, text, on) {
  node.textContent = '';
  const pill = el('span', 'pill ' + (on ? 'on' : 'off'), text);
  node.appendChild(pill);
}

function applyStatus(s) {
  state.status = s;

  const cls = lampClass(s);
  $('railLamp').className = 'lamp ' + cls;
  $('heroLamp').className = 'lamp big ' + cls;
  $('railDot').hidden = !s.connected;
  $('railDot').className = 'navdot ' + cls;

  const text = s.connected
    ? 'Cortico 已连接'
    : s.listening
      ? '等待 Cortico 连接'
      : s.listenError
        ? '监听失败'
        : '监听未开启';
  $('connText').textContent = text;
  $('railState').textContent = s.connected ? '已连接' : s.listening ? '等待连接' : s.listenError ? '监听失败' : '未监听';

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
    parts.push('去配置页打开「接管对话」');
  }
  $('connSub').textContent = parts.join(' · ');

  $('railAddr').textContent = `${s.host || '0.0.0.0'}:${s.port || '—'}`;
  $('railAddr').title = 'Cortico 扩展里要填 ws://<这台机器的地址>:' + (s.port || '');

  const st = s.stats || {};
  $('mPushed').textContent = st.pushed || 0;
  $('mSent').textContent = st.sent || 0;
  $('mFailed').textContent = st.failed || 0;
  $('mTools').textContent = s.toolCount || 0;
  $('railTools').textContent = s.toolCount ? s.toolCount : '—';

  setPill($('kEnabled'), s.enabled ? '开' : '关', !!s.enabled);

  $('kObserve').textContent = s.observeAll
    ? '开 · 群里每条都交给 Coo'
    : '关 · 只回被叫到的';

  const groups = s.groups || [];
  $('kGroups').textContent = groups.length ? groups.join('、') : '全部群';
  $('kGroups').title = groups.length ? '只接管这些群' : '没有限制，所有群都按 B 模式处理';

  const aliases = s.aliases || [];
  $('kAliases').textContent = aliases.length ? aliases.join('、') : '只认 @';

  $('kMax').textContent = s.maxSends ? `${s.maxSends} 条 / 分钟` : '不限';

  const imageMode =
    s.imageMode === 'image'
      ? '原图'
      : s.imageMode === 'off'
        ? '关闭'
        : s.imageMode === 'auto'
          ? s.peerImages
            ? '原图（自动）'
            : '转述（自动）'
          : '转述';
  $('kImage').textContent = imageMode;
  $('kImage').title =
    s.imageMode === 'auto'
      ? 'auto：对端模型能读图就传原图，否则让视觉模型转述'
      : '在插件「配置」页的 image_mode 里调整';

  const last = s.lastMessage;
  if (last) {
    const [lkind, lid] = String(last.conv || '').split(':');
    const chName = lkind === 'private' ? 'QQ私聊' : 'QQ群';
    const who = last.dir === 'out' ? 'Coo' : (last.sender || '未知');
    $('chLast').textContent =
      `${chName} · ${last.convName || lid || ''} · ${who}${last.owner ? ' · 主人' : ''}`;
    $('chReply').textContent = s.replyWith || '—';
  } else {
    $('chLast').textContent = '—';
    $('chReply').textContent = '—';
  }

  $('kVoiceIn').textContent = s.sttProvider
    ? `${s.sttProvider}${s.sttEnabled ? '' : ' · 未启用'}`
    : '未配置 STT';
  $('kVoiceIn').title = s.sttProvider
    ? s.sttEnabled
      ? 'QQ 语音先用这个 STT Provider 转成文字，再交给 Coo'
      : 'STT Provider 已就绪，但「转写群里的语音」还关着'
    : '去 AstrBot 的模型提供商页添加一个 STT Provider（Whisper / SenseVoice 等）';
  $('kVoiceOut').textContent = s.ttsProvider
    ? `${s.ttsProvider}${s.ttsEnabled ? '' : ' · 未启用'}`
    : '未配置 TTS';
  $('kVoiceOut').title = s.ttsProvider
    ? s.ttsEnabled
      ? 'Cortico 可以让某条回复用语音发出'
      : 'TTS Provider 已就绪，但插件里的「允许语音」还关着'
    : '去 AstrBot 的模型提供商页添加一个 TTS Provider（Edge TTS / OpenAI TTS 等）';

  setPill($('kToken'), s.tokenSet ? '已设置' : '未设置', !!s.tokenSet);
  $('kToken').title = s.tokenSet
    ? '桥只接受带这个 token 的握手'
    : '没有 token：任何能连到监听端口的人都能接管桥，建议设置';

  const admins = s.admins || [];
  $('kOwner').textContent = admins.length ? admins.join('、') : '未配置';
  $('kOwner').title = admins.length
    ? '来自 AstrBot 管理员名单：截图权限和「主人」身份都从这里来'
    : '去 AstrBot 的配置里填管理员 ID';

  const convs = s.conversations || [];
  $('kConvs').textContent = convs.length ? `${convs.length} 个` : '还没有';
  $('kConvs').title = convs
    .map((c) => `${c.umo || c.key} · ${ago(c.age_sec)}`)
    .join('\n');

  renderToolLog();
}

function renderRows() {
  const host = $('rows');
  host.textContent = '';
  const filter = state.msgFilter;
  const shown = state.rows.filter((row) => {
    if (filter === 'in') return row.dir !== 'out';
    if (filter === 'out') return row.dir === 'out';
    if (filter === 'at') return !!row.mentioned;
    return true;
  });

  for (const row of shown) {
    const item = el('div', 'msg' + (row.dir === 'out' ? ' out' : ''));

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
    const [kind, id] = String(row.conv || '').split(':');
    meta.appendChild(
      el('b', null, row.dir === 'out' ? 'Coo' : row.notice ? '群通知' : (row.sender || '未知')),
    );
    if (row.owner && !row.notice && row.dir !== 'out') {
      meta.appendChild(el('span', 'who owner', '· 主人'));
    }
    const ch = kind === 'private' ? 'QQ私聊' : 'QQ群';
    meta.appendChild(
      el('span', 'who', `· ${kind === 'private' ? ch : `${ch} ${convLabel(kind, id, row.convName)}`}`),
    );
    const text = el('div', 'mtext');
    text.textContent = row.text || '';

    body.append(meta, text);
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

async function refresh() {
  const s = unwrap(await bridge.apiGet('status'));
  applyStatus(s);
}

function connectStream() {
  return bridge.subscribeSSE('events', {
    onMessage(ev) {
      const d = ev.parsed;
      if (!d || typeof d !== 'object') return;

      if (d.type === 'snapshot') {
        applySnapshot(d);
        return;
      }
      if (d.type === 'message') {
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
      if (d.type === 'connected' || d.type === 'disconnected' || d.type === 'listening' || d.type === 'info') {
        toast(d.text || d.type, d.type === 'disconnected');
        refresh().catch(() => {});
      }
    },
    onError() {
      $('msgCount').title = '实时流断开，正在靠定时刷新兜底';
    },
  });
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
  $('msgFilters').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    state.msgFilter = chip.dataset.msg;
    document.querySelectorAll('#msgFilters .chip').forEach((c) => c.classList.toggle('on', c === chip));
    renderRows();
  });
}

/* ---------------- 工具 ---------------- */

function dirty() {
  if (state.picked.size !== state.saved.size) return true;
  for (const name of state.picked) if (!state.saved.has(name)) return true;
  return false;
}

function riskCount(risk) {
  return state.tools.filter((t) => t.risk === risk).length;
}

function pickedByRisk(risk) {
  return state.tools.filter((t) => t.risk === risk && state.picked.has(t.name)).length;
}

function updateSavebar() {
  const on = dirty();
  state.armClear && !on && disarmClear();
  $('savebar').hidden = !on;
  $('panel-tools').classList.toggle('dirty', on);
  $('dirtyText').textContent = `有未保存的改动 · 已选 ${state.picked.size} 个`;
}

function updateToolHero() {
  const total = state.tools.length;
  const picked = state.tools.filter((t) => state.picked.has(t.name)).length;
  $('pickedCount').textContent = picked;
  $('totalCount').textContent = total;
  $('railTools').textContent = String(picked);

  const parts = { read: 0, write: 0, exec: 0 };
  for (const t of state.tools) {
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
    const picked = tools.filter((t) => state.picked.has(t.name)).length;
    const badge = card.querySelector('.gcount');
    if (badge) {
      badge.textContent = '';
      badge.appendChild(el('b', null, String(picked)));
      badge.appendChild(document.createTextNode(`/${tools.length}`));
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
    const picked = tools.filter((t) => state.picked.has(t.name)).length;
    count.appendChild(el('b', null, String(picked)));
    count.appendChild(document.createTextNode(`/${tools.length}`));
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
  const every = !!data.all || sel.includes('*');
  state.saved = new Set(every ? state.tools.map((t) => t.name) : sel);
  state.picked = new Set(state.saved);
  state.loaded = true;
  $('railTools').textContent = String(state.tools.filter((t) => state.picked.has(t.name)).length);
  renderTools();
}

async function saveTools() {
  if (state.saving || !dirty()) return;
  state.saving = true;
  const btn = $('btnSave');
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    await bridge.apiPost('tools', { tools: [...state.picked] });
    state.saved = new Set(state.picked);
    updateSavebar();
    toast(`已保存 ${state.picked.size} 个工具，已同步给 Coo`);
    if (state.status) {
      state.status.toolCount = state.picked.size;
      $('mTools').textContent = state.picked.size;
      $('railTools').textContent = state.picked.size || '—';
    }
  } catch (e) {
    toast('保存失败：' + (e.message || e), true);
  } finally {
    state.saving = false;
    btn.disabled = false;
    btn.textContent = '保存并同步';
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

  const limit = data.maxChars || 0;
  $('ttsMeta').textContent = hasTts
    ? limit
      ? `超过 ${limit} 字改发文字`
      : '不限字数'
    : '先添加 TTS Provider';

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
    if (data.voice) voiceMsg('已用语音发到最近的 QQ 会话。');
    else voiceMsg(`已发出，但退回了文字${data.note ? '：' + data.note : ''}`, true);
  } catch (e) {
    voiceMsg(e.message || String(e), true);
  } finally {
    btn.disabled = false;
  }
}

function initVoice() {
  $('ttsEnabled').addEventListener('change', (e) => saveVoiceEnabled(e.target.checked));
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

/* ---------------- 启动 ---------------- */

async function main() {
  try {
    await bridge.ready();
  } catch (e) {
    toast('面板桥接没就绪：' + (e.message || e), true);
    return;
  }

  document.querySelectorAll('.navitem').forEach((btn) => {
    btn.addEventListener('click', () => setPanel(btn.dataset.panel));
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
  initVoice();

  try {
    await refresh();
    connectStream().catch(() => {});
  } catch (e) {
    toast('读取状态失败：' + (e.message || e), true);
  }

  renderRows();
  renderToolLog();

  const wanted = new URLSearchParams(location.search).get('panel');
  if (wanted && PANELS[wanted]) setPanel(wanted);

  setInterval(() => {
    if (state.panel === 'status') refresh().catch(() => {});
  }, 20000);
}

main();
