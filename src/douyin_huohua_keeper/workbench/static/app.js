/* ==========================================================================
   douyin-huohua-keeper 工作台前端
   --------------------------------------------------------------------------
   零依赖、零构建的原生 JS。改完刷新就生效，不需要 npm。

   结构：
     1. 工具函数（DOM、格式化、提示）
     2. API 客户端（令牌注入、统一错误处理）
     3. 各页面渲染函数
     4. 路由与初始化

   关于令牌：保存在 localStorage，通过 X-Huohua-Token 头发给后端。
   支持从 URL 的 ?token=xxx 自动吸收 —— 这样你在服务器上第一次打开
   可以直接用带令牌的链接，不用手输。
   ========================================================================== */

'use strict';

/* ==========================================================================
   1. 工具函数
   ========================================================================== */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** 建元素。`h('div.card', {id:'x'}, ['文字', childNode])` */
function h(spec, attrs = {}, children = []) {
  const [tagPart, ...classes] = String(spec).split('.');
  const el = document.createElement(tagPart || 'div');
  if (classes.length) el.className = classes.join(' ');

  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') {
      el.className = el.className ? `${el.className} ${value}` : String(value);
    } else if (key === 'text') {
      el.textContent = String(value);
    } else if (key === 'html') {
      el.innerHTML = String(value);
    } else if (key === 'style' && typeof value === 'object') {
      Object.assign(el.style, value);
    } else if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'dataset' && typeof value === 'object') {
      Object.assign(el.dataset, value);
    } else {
      el.setAttribute(key, String(value));
    }
  }

  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

function shortTime(iso) {
  if (!iso) return '';
  // 后端给的是本地时间字符串（YYYY-MM-DDTHH:MM:SS），直接截取即可，
  // 不要 new Date() —— 那会引入时区解释，反而容易错
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!m) return String(iso);
  return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
}

function dayLabel(dateStr, todayStr) {
  if (dateStr === todayStr) return '今天';
  const d = new Date(`${dateStr}T00:00:00`);
  const t = new Date(`${todayStr}T00:00:00`);
  const diff = Math.round((t - d) / 86400000);
  if (diff === 1) return '昨天';
  if (diff === 2) return '前天';
  return dateStr.slice(5);
}

/* --- 提示条 --------------------------------------------------------------- */

function toast(message, kind = 'ok', ms = 4000) {
  const box = $('#toaster');
  const icon = kind === 'err' ? '✕' : kind === 'warn' ? '!' : '✓';
  const el = h('div.toast', { class: kind, text: `${icon} ${message}` });
  box.append(el);
  setTimeout(() => {
    el.style.transition = 'opacity .25s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 250);
  }, ms);
}

/** 把异常转成一句人话。后端的 detail 字段优先 —— 它通常已经写得很清楚了 */
function errText(err) {
  if (!err) return '未知错误';
  if (typeof err === 'string') return err;
  if (err.detail) {
    if (typeof err.detail === 'string') return err.detail;
    // Pydantic 校验错误是数组
    if (Array.isArray(err.detail)) {
      return err.detail.map((d) => d.msg || JSON.stringify(d)).join('；');
    }
    return JSON.stringify(err.detail);
  }
  return err.message || String(err);
}

/* ==========================================================================
   2. API 客户端
   ========================================================================== */

/* 令牌存取与 HTTP 细节都在 common.js（app.js / send.js 共用一份实现）。
 * 这里只保留本页面习惯的调用形态：get/post/...，以及「401 抛 auth」。 */
const api = {
  get token() {
    return huohuaReadToken();
  },
  set token(value) {
    huohuaSaveToken(value);
  },

  async request(method, path, body) {
    return huohuaRequest(method, path, body);
  },

  get(path) {
    return this.request('GET', path);
  },
  post(path, body) {
    return this.request('POST', path, body ?? {});
  },
  put(path, body) {
    return this.request('PUT', path, body ?? {});
  },
  patch(path, body) {
    return this.request('PATCH', path, body ?? {});
  },
  del(path) {
    return this.request('DELETE', path);
  },
};

/* 全局状态 */
const state = {
  tab: 'home',
  overview: null,
  tasks: null,
  days: [],
  runs: [],
  config: null,
  check: null,
  pollTimer: null,
  qrTimer: null,
  runProgress: null,
  runProgressTimer: null,

  // 收件人分页（一次只渲染一页，翻页才取下一页）
  contactsPage: 1,
  contactsPageSize: 15,
  contactsData: { items: [], total: 0, pages: 1, enabledCount: 0 },
};

/* ==========================================================================
   3. 页面渲染
   ========================================================================== */

/* --- 首页 ----------------------------------------------------------------- */

async function renderHome(root) {
  const ov = state.overview;
  if (!ov) {
    root.append(h('div.empty', {}, [h('div.big', { text: '…' }), '正在加载']));
    return;
  }

  /* 状态横幅 —— 首屏最重要的一件事：「今天成了没有」 */
  const todayOk = ov.today_result.success;
  const authed = ov.account.state.present && !ov.account.state.expired;
  const enabledContacts = ov.contacts.enabled;

  // 「最近一次运行」的真实结果。
  // ⚠️ 为什么需要它：today_result.success 是**按天累积**的 —— 今天只要成功过一次
  // 就永远是 true。于是「早上续上了、晚上那次 15 个人全失败」会显示成一片绿，
  // 用户根本看不出刚才那一轮挂了（实测踩过）。
  const lastTotal = Number(ov.today_result.last_total || 0);
  const lastFailed = Number(ov.today_result.last_failed || 0);
  const lastUncertain = Number(ov.today_result.last_uncertain || 0);
  const lastBad = lastTotal > 0 && (lastFailed > 0 || lastUncertain > 0);

  let banner;
  if (!authed) {
    banner = bannerEl(
      'err',
      '🔑',
      '还没有绑定账号',
      '去「账号」页扫一下码就能用了。绑定之后它会每天自动帮你发消息。'
    );
  } else if (lastBad) {
    banner = bannerEl(
      'err',
      '⚠️',
      lastFailed
        ? `最近一次运行有 ${lastFailed} 个人没发出去`
        : '最近一次运行有人结果不确定',
      `${ov.today_result.last_summary || '上次执行未成功'} · ` +
        '去「历史」页看原因，或在本页点「立即发送一次」手动补发（手动发送不会被「今天已发」拦住）。'
    );
  } else if (todayOk) {
    banner = bannerEl('ok', '✅', '今天的火花已经续上了', [
      ov.today_result.first_success_at
        ? `在 ${shortTime(ov.today_result.first_success_at)} 发送成功`
        : '今日已发送成功',
      ov.today_result.sent_to?.length
        ? ` · 发给了 ${ov.today_result.sent_to.join('、')}`
        : '',
    ].join(''));
  } else if (!enabledContacts) {
    banner = bannerEl(
      'warn',
      '📇',
      '还没有启用任何收件人',
      '去「收件人」页从会话列表同步，或手工添加。至少要有一个人，任务才会执行。'
    );
  } else if (!ov.schedule.enabled) {
    banner = bannerEl('warn', '⏸', '定时发送是关闭的', '去「任务」页打开它，否则不会自动发送。');
  } else if (ov.today_result.attempts > 0) {
    banner = bannerEl(
      'err',
      '⚠️',
      '今天尝试过但还没成功',
      `${ov.today_result.last_summary || '上次执行未成功'}。去「历史」页看失败原因。`
    );
  } else {
    banner = bannerEl(
      'ok',
      '⏳',
      '今天还没发，等待中',
      `${ov.schedule.describe}${ov.scheduler.next_run_at ? ` · 下次 ${shortTime(ov.scheduler.next_run_at)}` : ''}`
    );
  }
  root.append(banner);

  /* 手动发送 */
  const manualCard = h('div.card', {}, [h('h2', { text: '手动操作' })]);
  const sendBtn = h('button.primary', { text: '🚀 立即发送一次' });
  const dryBtn = h('button', { text: '🧪 演练（不真发）' });
  const sendMsg = h('div.small.muted.mt', { text: '' });

  const runManual = async (dry) => {
    sendBtn.disabled = true;
    dryBtn.disabled = true;
    clear(sendMsg);
    sendMsg.append(h('span.spin'), dry ? ' 演练中，正在启动浏览器…' : ' 发送中，正在启动浏览器…');

    try {
      const resp = await api.post('/api/runs/now', { dry_run: dry, async: false });
      if (resp.started) {
        clear(sendMsg).append(
          h('span', {
            class: resp.ok ? 'pill ok' : 'pill err',
            text: resp.detail || '',
          })
        );
        if (resp.report) renderReportDetail(resp.report);
        await refresh(['overview', 'runs', 'days']);
        await renderCurrent();
      } else {
        clear(sendMsg).append(h('span.pill.warn', { text: resp.detail || '没有执行' }));
      }
    } catch (err) {
      clear(sendMsg).append(h('span.pill.err', { text: errText(err) }));
    } finally {
      sendBtn.disabled = false;
      dryBtn.disabled = false;
    }
  };

  sendBtn.addEventListener('click', () => runManual(false));
  dryBtn.addEventListener('click', () => runManual(true));

  manualCard.append(
    h('div.inline', {}, [sendBtn, dryBtn]),
    h('div.field.hint', {
      text:
        '「立即发送一次」会真的发出去 —— 每点一次就真实发送一次，' +
        '即使今天已经发过也不会被跳过（「当日防重复」只对每天 09:30 的定时任务生效）。',
    }),
    sendMsg
  );
  root.append(manualCard);

  /* 任务进行状态（实时刷新，4 秒一次，由 boot 的轮询驱动） */
  const progressSlot = h('div', { id: 'runProgressSlot' });
  root.append(progressSlot);
  // 先填一次占位，后续由 4s 轮询持续更新
  api
    .get('/api/system/run-progress')
    .then((p) => {
      state.runProgress = p;
      renderRunProgress(progressSlot, p);
    })
    .catch(() => {});

  /* 当前状态卡片 */
  const info = h('div.card', {}, [h('h2', { text: '当前状态' })]);
  info.append(
    h('dl.kv', {}, [
      h('dt', { text: '下次运行' }),
      h('dd', { text: ov.scheduler.human || '—' }),
      h('dt', { text: '发送时间' }),
      h('dd', { text: ov.schedule.describe }),
      h('dt', { text: '收件人' }),
      h('dd', { text: `${ov.contacts.enabled} 个启用 / 共 ${ov.contacts.total} 个` }),
      h('dt', { text: '消息池' }),
      h('dd', {
        text: ov.messages.empty
          ? '空（会使用默认文案「早」）'
          : `${ov.messages.count} 条（${ov.messages.kinds.join('、')}）`,
      }),
      h('dt', { text: '登录态' }),
      h('dd', { text: ov.account.state.detail }),
      h('dt', { text: '连续成功' }),
      h('dd', { text: `${ov.streak.consecutive_successes} 天` }),
      h('dt', { text: '通知渠道' }),
      h('dd', { text: ov.config.notify_describe }),
      h('dt', { text: '演练模式' }),
      h('dd', { text: ov.config.dry_run ? '已开启（不会真正发送）' : '关闭' }),
    ])
  );

  if (ov.problems && ov.problems.length) {
    info.append(
      h('div.mt', {}, [
        h('div.small.warn', { text: `⚠️ 发现 ${ov.problems.length} 个配置问题：` }),
        h('ul.small.muted', { style: { margin: '6px 0 0', paddingLeft: '20px' } },
          ov.problems.map((p) => h('li', { text: p }))
        ),
      ])
    );
  }
  root.append(info);

  /* 最近记录卡片 */
  const heat = h('div.card', {}, [h('h2', { text: '最近记录' })]);
  if (state.days.length === 0) {
    heat.append(h('div.empty', {}, [h('div.big', { text: '📭' }), '还没有任何运行记录']));
  } else {
    const byDate = Object.fromEntries(state.days.map((d) => [d.date, d]));
    const grid = h('div.calendar');
    const today = ov.today;
    for (let i = 13; i >= 0; i--) {
      const d = new Date(`${today}T00:00:00`);
      d.setDate(d.getDate() - i);
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      const entry = byDate[key];
      let cls = 'cal-cell';
      let title = `${key}：无记录`;
      if (entry) {
        if (entry.success) {
          cls += ' ok';
          title = `${key}：成功${entry.sent_to?.length ? ` → ${entry.sent_to.join('、')}` : ''}`;
        } else if (entry.attempts > 0) {
          cls += ' miss';
          title = `${key}：失败了 ${entry.attempts} 次`;
        }
      }
      if (key === today) cls += ' today';
      grid.append(h('div', { class: cls, title, text: String(d.getDate()) }));
    }
    heat.append(grid, h('div.small.muted.mt', { text: '绿=成功，红=失败，灰=无记录。鼠标悬停看详情。' }));
  }
  root.append(heat);
}

function bannerEl(kind, icon, title, sub) {
  return h('div.banner', { class: kind }, [
    h('div.icon', { text: icon }),
    h('div.body', {}, [h('div.title', { text: title }), h('div.sub', { text: sub })]),
  ]);
}

/* 任务进行状态卡片。data 来自 /api/system/run-progress 的快照。 */
function renderRunProgress(slot, p) {
  if (!slot) return;
  clear(slot);

  if (!p || !p.active) {
    slot.append(
      h('div.card', {}, [
        h('h2', { text: '任务进行状态' }),
        h('div.empty.small', {}, [h('div.big', { text: '💤' }), '当前没有任务在运行']),
      ])
    );
    return;
  }

  const kindLabel = { scheduled: '定时任务', manual: '手动发送', broadcast: '群发' }[p.kind] || '发送任务';
  const total = p.total || 0;
  const pct = total ? Math.round((p.current_index / total) * 100) : p.active ? 100 : 0;

  const card = h('div.card', {}, [
    h('h2', {}, ['任务进行状态', h('span.pill', { class: 'warn', text: '进行中' })]),
  ]);

  card.append(
    h('div.spread.mb', {}, [
      h('div', {}, [
        h('div', { style: { fontWeight: '600' }, text: `${kindLabel}${p.dry_run ? '（演练）' : ''}` }),
        h('div.small.muted', { text: p.stage || '' }),
      ]),
    ])
  );

  // 进度条（内联样式，避免依赖额外 CSS）
  const bar = h(
    'div',
    {
      style: {
        height: '8px',
        background: 'var(--border)',
        borderRadius: '4px',
        overflow: 'hidden',
        marginTop: '8px',
      },
    },
    [
      h('div', {
        style: {
          height: '100%',
          width: `${pct}%`,
          background: 'linear-gradient(90deg, var(--fire-2), var(--fire-1))',
          transition: 'width .4s',
        },
      }),
    ]
  );
  card.append(bar);

  const countBits = [`第 ${p.current_index}/${total} 个`, `成功 ${p.sent}`, `失败 ${p.failed}`];
  if (p.skipped) countBits.push(`跳过 ${p.skipped}`);
  if (p.uncertain) countBits.push(`不确定 ${p.uncertain}`);
  card.append(h('div.small.muted.mt', { text: countBits.join(' · ') }));

  if (p.current_target) {
    card.append(
      h('div.mt', {}, [h('span.small.muted', { text: '正在给：' }), h('strong', { text: p.current_target })])
    );
  }

  slot.append(card);
}

/* --- 账号页 --------------------------------------------------------------- */

async function renderAccount(root) {
  const ov = state.overview;
  const acct = ov?.account;

  const card = h('div.card', {}, [h('h2', { text: '账号状态' })]);

  if (!acct) {
    card.append(h('div.empty', { text: '加载中…' }));
    root.append(card);
    return;
  }

  const st = acct.state;
  const healthy = st.present && !st.expired;
  card.append(
    h('div.spread.mb', {}, [
      h('div', {}, [
        h('div', { style: { fontWeight: '600' }, text: healthy ? '已绑定' : st.present ? '需要重新登录' : '尚未绑定' }),
        h('div.small.muted', { text: st.detail }),
      ]),
      h('span.pill', {
        class: healthy ? 'ok' : st.present ? 'warn' : 'err',
        text: healthy ? '正常' : st.present ? '快过期' : '未绑定',
      }),
    ])
  );

  if (st.present) {
    card.append(
      h('dl.kv.mb', {}, [
        h('dt', { text: '剩余约' }),
        h('dd', { text: st.expires_in_days !== null ? `${st.expires_in_days} 天` : '未知' }),
        h('dt', { text: '文件年龄' }),
        h('dd', { text: st.file_age_days !== null ? `${st.file_age_days} 天` : '—' }),
        h('dt', { text: 'sessionid' }),
        h('dd', { text: st.session_present ? '存在' : '缺失（不正常）' }),
      ])
    );
  }

  const qrBtn = h('button.primary', { text: st.present ? '🔄 重新扫码登录' : '📱 扫码登录' });
  qrBtn.addEventListener('click', () => openQrModal());

  // 「复制扫码链接」：手机上是**扫不了自己屏幕上的码**的 ——
  // 把带令牌的 /scan.html 链接复制出来发到另一台设备（电脑）打开即可。
  // 这是登录态失效时唯一能救急的路径。
  const linkBtn = h('button.small.ghost', { text: '🔗 复制扫码链接' });
  linkBtn.addEventListener('click', async () => {
    const url = `${location.origin}/scan.html?token=${encodeURIComponent(api.token)}`;
    try {
      await navigator.clipboard.writeText(url);
      toast('链接已复制 —— 发到另一台设备打开，就能扫码了', 'ok', 5000);
    } catch (err) {
      // 剪贴板 API 只在 https / localhost 下可用；取不到就让用户手抄
      window.prompt('复制这个链接，在另一台设备的浏览器里打开：', url);
    }
  });

  card.append(h('div.inline', {}, [qrBtn, linkBtn]));

  card.append(
    h('div.field.hint', {
      text:
        '登录态存在服务端本地文件里（等同密码，权限已收紧到 600）。' +
        '抖音的登录态通常 7~30 天失效一次，失效后会推送提醒你重新扫。',
    })
  );

  if (st.present) {
    const outBtn = h('button.danger.small', { text: '退出登录并清空登录态' });
    outBtn.addEventListener('click', async () => {
      if (!confirm('确定要清空登录态吗？\n\n清空后需要重新扫码才能继续发送。')) return;
      try {
        const resp = await api.post('/api/accounts/logout');
        toast(resp.detail || '已退出登录', 'ok');
        await refresh(['overview']);
        await renderCurrent();
      } catch (err) {
        toast(errText(err), 'err');
      }
    });
    card.append(h('div.mt', {}, [outBtn]));
  }

  root.append(card);

  /* 扫码步骤说明 —— 事先说清楚要做什么，比让用户自己猜好 */
  root.append(
    h('div.card', {}, [
      h('h2', { text: '扫码怎么做' }),
      h('ol.small.muted', { style: { margin: 0, paddingLeft: '20px', lineHeight: '1.9' } }, [
        h('li', { text: '点上面的「扫码登录」，等二维码出现（首次要启动浏览器，约 5-10 秒）' }),
        h('li', { text: '打开手机抖音 App → 右下角「我」→ 右上角扫一扫' }),
        h('li', { text: '扫这个二维码并确认登录' }),
        h('li', { text: '页面上会自动显示「登录成功」，登录态自动保存' }),
      ]),
      h('div.field.hint', {
        text: '二维码约 2 分钟失效，过期了点「刷新二维码」即可，不用重开关掉弹窗。',
      }),
    ])
  );
}

/* --- 二维码弹窗 ------------------------------------------------------------ */

let qrModalEl = null;
let qrRelayTimer = null;
// 由 openQrModal 填进来的「显示登录页操作面板」函数（startQrPoll 要用它）
let qrShowRelay = null;
// 扫码轮询是否正在请求中。浏览器只有一个线程，慢操作会让请求排队；
// 不挡一下，请求越堆越多，界面看起来就是「卡住了」。
//
// ⚠️ 但必须带**超时兜底**：万一某个请求永远不返回（线程卡死、连接被中间层
// 悄悄掐断而没有响应），纯 boolean 标记会永远为 true → 轮询被自己锁死、
// 再也发不出请求 —— 那比堆积更糟。超过 25 秒就认为它已经死了，放行新的。
let qrPollBusy = false;
let qrPollBusyAt = 0;

async function openQrModal() {
  if (qrModalEl) return;

  const imgBox = h('div.qr-box', {}, [h('span.spin')]);
  const statusLine = h('div.sub', { text: '正在启动浏览器，请稍候…' });
  const refreshBtn = h('button.small', { text: '刷新二维码', disabled: true });
  const cancelBtn = h('button.small.ghost', { text: '取消' });

  // --- 「登录页操作」面板 ---
  //
  // 抖音在新设备 / 新 IP 上登录时经常要求二次验证（选择验证方式 → 填短信验证码）。
  // 这个页面在无头浏览器里既看不见也点不到，用户会卡在「手机确认了却一直登不进去」。
  // 这一块把登录页截出来，让用户直接在上面点、直接输入验证码。
  const relayImg = h('img', { class: 'relay-shot', alt: '登录页实时截图' });
  const fullImg = h('img', { class: 'relay-shot-full', alt: '登录页实时截图（放大）' });
  const relayHint = h('div.relay-status', { text: '' });
  const relayRead = h('div.relay-read', { text: '' });
  const relayInput = h('input', {
    type: 'text',
    placeholder: '验证码 / 要输入的内容',
    autocomplete: 'off',
  });
  const relayTarget = h('select', {
    class: 'relay-target',
    title: '文字要填进页面上的哪个输入框',
  });
  const relayZoomBtn = h('button.small.ghost', { text: '🔍 放大看清' });
  const relayFocusBtn = h('button.small', { text: '① 聚焦这个框' });
  const relayTypeBtn = h('button.small.primary', { text: '② 输入内容' });
  const relayClearBtn = h('button.small.ghost', { text: '清空' });
  const relayEnterBtn = h('button.small', { text: '回车 ↵' });
  const relayReloadBtn = h('button.small.ghost', {
    text: '重载登录页（二维码会重置）',
    title: '重新加载抖音登录页。二维码会重新生成，之前的扫码作废 —— 非必要不用点',
  });
  const relayZoomOutBtn = h('button.small.ghost', { text: '✕ 缩小' });

  // 放大模式：铺满屏幕，尽量看得清（手机上尤其需要）
  const relayZoom = h('div.relay-zoom', { style: { display: 'none' } }, [
    h('div.relay-zoom-bar', {}, [
      h('span', { text: '点截图上的位置 = 点页面对应位置' }),
      relayZoomOutBtn,
    ]),
    h('div.relay-zoom-scroll', {}, [fullImg]),
  ]);

  const relayPanel = h('div.relay', { style: { display: 'none' } }, [
    h('div.relay-title', { text: '🔧 登录页实时画面（正在发生什么，看这里）' }),
    h('div.small.muted', {
      text:
        '上面那张二维码是打开时截下来的，不会自己更新。' +
        '扫码后「请在手机上确认」、以及二次验证页，都会出现在下面这张实时画面里。',
    }),
    relayHint,
    h('div.relay-shot-wrap', {}, [relayImg]),
    relayRead,
    h('div.inline.mt', {}, [relayZoomBtn, relayFocusBtn]),
    // 操作区「吸底」—— 截图再高，输入框和按钮也始终在屏幕上（见 style.css 注释）
    h('div.relay-actions', {}, [
      h('div.relay-target-row', {}, [
        h('span.small.muted', { text: '要填进：' }),
        relayTarget,
      ]),
      h('div.inline.mt', {}, [relayInput, relayTypeBtn, relayClearBtn, relayEnterBtn]),
      h('div.inline.mt', {}, [relayReloadBtn]),
    ]),
  ]);

  const modal = h('div.modal', {}, [
    h('h3', { text: '扫码登录抖音' }),
    statusLine,
    imgBox,
    h('ol.steps', {}, [
      h('li', { text: '手机打开抖音 App，进「我」页面' }),
      h('li', { text: '点右上角扫一扫，扫描上面的二维码' }),
      h('li', { text: '手机上确认登录' }),
      h('li', {
        text: '扫完码后的情况（包括二次验证页）都看下面的「登录页实时画面」，在里面点选、填验证码',
      }),
    ]),
    relayPanel,
    h('div.inline', { style: { justifyContent: 'center' } }, [refreshBtn, cancelBtn]),
  ]);

  qrModalEl = h('div.modal-backdrop', {}, [modal, relayZoom]);
  document.body.append(qrModalEl);

  const close = async () => {
    stopQrPoll();
    if (qrRelayTimer) {
      clearInterval(qrRelayTimer);
      qrRelayTimer = null;
    }
    document.removeEventListener('keydown', onRelayKey);
    qrShowRelay = null;
    qrModalEl?.remove();
    qrModalEl = null;
    try {
      await api.post('/api/accounts/qr/cancel');
    } catch {
      /* 关掉了就不用管取消失败 */
    }
    // 关掉浏览器后引擎状态变了，刷新一下
    await refresh(['overview']);
    await renderCurrent();
  };

  cancelBtn.addEventListener('click', close);
  qrModalEl.addEventListener('click', (e) => {
    if (e.target === qrModalEl) close();
  });

  /* --- 「登录页操作」的行为 --- */
  //
  // 浏览器只有一个线程，轮询 / 截图 / 操作全部排队；不挡一下的话请求会堆积，
  // 而且过期响应会覆盖新响应（旧截图盖掉新截图）。
  //
  // ⚠️ 但**自动刷新**和**用户操作**必须区别对待：
  //   自动截图每 6 秒一次、每次 1~3 秒。如果用户操作也被那个 busy 标记挡住
  //   然后直接 return —— 只要点的时候正好有一次截图在飞，点击就**毫无反应**。
  //   这就是「点了没用」的成因之一。
  //   所以：自动刷新可以被跳过；用户操作一律进**串行队列，绝不丢弃**。
  let relayBusy = false;
  let relayBusyAt = 0;
  let relayLastActionAt = 0;
  let relayChain = Promise.resolve();

  const relayAutoBusy = () => relayBusy && Date.now() - relayBusyAt < 25000;
  const lockAuto = () => {
    relayBusy = true;
    relayBusyAt = Date.now();
  };
  /** 用户操作排队执行：前一个请求无论成败都继续下一个，绝不丢弃。 */
  const relayQueue = (fn) => {
    relayChain = relayChain.then(fn, fn);
    return relayChain;
  };
  /** 当前下拉框选中的输入框序号；没有可选项时返回 undefined（交给后端自动挑）。 */
  const targetIndex = () => {
    const raw = relayTarget.value;
    return raw === '' ? undefined : Number(raw);
  };

  const setRelayStatus = (text, kind) => {
    relayHint.textContent = text || '';
    relayHint.className = 'relay-status' + (kind ? ' ' + kind : '');
  };

  /** 把「页面上各输入框的当前内容」显示出来 —— 用户据此确认字真的进去了。 */
  const renderInputs = (inputs) => {
    if (!Array.isArray(inputs) || !inputs.length) {
      relayRead.textContent = '页面上的输入框：暂时没检测到（可能还没点出验证码框）';
      return;
    }
    const parts = inputs.map((item) => {
      const value = String(item.value || '').trim();
      const label = item.placeholder ? `${item.placeholder}：` : '';
      return label + (value || '（空）');
    });
    relayRead.textContent = '页面输入框当前内容 → ' + parts.join('  |  ');
  };

  /**
   * 重建「要填进哪个框」的下拉框。
   *
   * 这是修「输不进验证码」的关键：抖音登录页上**同时**有「+86 国家码」
   * 「手机号」「验证码」三个可见输入框，而验证码框排最后。
   * 以前后端总是挑「第一个」→ 验证码被写进了国家码框 → 用户看到「点了没用」。
   * 现在把选择权交给用户，默认选中后端识别出的「最像验证码」的那个。
   */
  const rebuildTargets = (resp) => {
    const inputs = Array.isArray(resp.inputs) ? resp.inputs : [];
    const previous = relayTarget.value;
    clear(relayTarget);

    if (!inputs.length) {
      relayTarget.append(h('option', { value: '', text: '（还没检测到输入框）' }));
      relayTarget.disabled = true;
      return;
    }

    inputs.forEach((item, i) => {
      const label = item.placeholder || `输入框 ${i + 1}`;
      relayTarget.append(h('option', { value: String(i), text: `第 ${i + 1} 个：${label}` }));
    });
    relayTarget.disabled = false;

    // 用户手动选过就尊重他的选择，否则用后端推荐的序号
    const code = resp.code_index === null || resp.code_index === undefined
      ? inputs.length - 1
      : resp.code_index;
    const wanted = previous !== '' && previous !== String(code) ? previous : String(code);
    relayTarget.value = wanted;
  };

  const applyRelay = (resp) => {
    if (!resp || !resp.ok) return false;
    if (resp.image) {
      relayImg.src = resp.image;
      fullImg.src = resp.image;
    }
    relayImg.dataset.w = String(resp.width || '');
    relayImg.dataset.h = String(resp.height || '');
    renderInputs(resp.inputs);
    rebuildTargets(resp);
    return true;
  };

  /** 自动刷新用：可以被跳过（用户操作永远优先） */
  const fetchRelay = async () => {
    if (relayAutoBusy()) return;
    lockAuto();
    try {
      const resp = await api.get('/api/accounts/qr/page');
      if (!applyRelay(resp)) setRelayStatus((resp && resp.detail) || '拿不到登录页截图', 'err');
    } catch (err) {
      setRelayStatus(errText(err), 'err');
    } finally {
      relayBusy = false;
    }
  };

  /** 用户操作用：进队列，**绝不丢弃** */
  const relayAct = (payload) =>
    relayQueue(async () => {
      relayLastActionAt = Date.now();
      setRelayStatus('处理中…');
      try {
        const resp = await api.post('/api/accounts/qr/act', payload);
        applyRelay(resp);
        // 后端会回读页面上的值来确认字真的写进去了；没写进去就明确报红，不谎报成功
        const bad = resp && resp.verified === false;
        setRelayStatus((resp && resp.detail) || '完成', bad ? 'err' : 'ok');
      } catch (err) {
        setRelayStatus(errText(err), 'err');
      }
    });

  const showRelay = () => {
    if (relayPanel.style.display !== 'none') return;
    relayPanel.style.display = '';
    // 截图要看得清才点得准 —— 展示时把弹窗放宽
    modal.classList.add('wide');
    setRelayStatus('正在读取登录页…');
    fetchRelay();
    // 每 6 秒换一张新截图；这几种情况跳过，免得干扰用户或叠加请求
    if (qrRelayTimer) clearInterval(qrRelayTimer);
    qrRelayTimer = setInterval(() => {
      if (!qrModalEl) return;
      if (relayAutoBusy()) return;
      // 刚操作过就先别刷：别把用户刚看到的结果顶掉，也别把下一次点击挤到队尾
      if (Date.now() - relayLastActionAt < 8000) return;
      if (document.activeElement === relayInput) return;
      if (relayZoom.style.display !== 'none') return; // 放大时用户自己点刷新
      fetchRelay();
    }, 6000);
  };
  // 让 startQrPoll（模块级函数）也能触发显示
  qrShowRelay = showRelay;

  /** 截图坐标 → 页面坐标。两张截图（内嵌 / 放大）共用这一套换算。 */
  const shotToPage = (img, event) => {
    const rect = img.getBoundingClientRect();
    const vw = Number(relayImg.dataset.w || 0);
    const vh = Number(relayImg.dataset.h || 0);
    if (!rect.width || !rect.height || !vw || !vh) return null;
    return {
      // 截图像素 / 显示像素 —— 两个方向分别换算
      x: Math.round(((event.clientX - rect.left) / rect.width) * vw),
      y: Math.round(((event.clientY - rect.top) / rect.height) * vh),
    };
  };
  const bindShot = (img) => {
    img.addEventListener('click', (event) => {
      const point = shotToPage(img, event);
      if (point) relayAct({ action: 'click', x: point.x, y: point.y });
    });
  };
  bindShot(relayImg);
  bindShot(fullImg);

  const openZoom = () => {
    if (!fullImg.src && relayImg.src) fullImg.src = relayImg.src;
    relayZoom.style.display = '';
    fetchRelay();
  };
  const closeZoom = () => {
    relayZoom.style.display = 'none';
  };
  const onRelayKey = (event) => {
    if (event.key === 'Escape') closeZoom();
  };
  document.addEventListener('keydown', onRelayKey);
  relayZoomBtn.addEventListener('click', openZoom);
  relayZoomOutBtn.addEventListener('click', closeZoom);

  relayFocusBtn.addEventListener('click', () =>
    relayAct({ action: 'focus', index: targetIndex() })
  );
  relayClearBtn.addEventListener('click', () =>
    relayAct({ action: 'clear', index: targetIndex() })
  );
  relayEnterBtn.addEventListener('click', () => relayAct({ action: 'key', key: 'Enter' }));
  relayReloadBtn.addEventListener('click', () => relayAct({ action: 'reload' }));
  relayTypeBtn.addEventListener('click', () => {
    const text = relayInput.value.trim();
    if (!text) {
      setRelayStatus('先在下面那个框里填验证码，再点「输入内容」', 'err');
      relayInput.focus();
      return;
    }
    // 带上「要填哪个框」—— 这才是验证码能真正进对地方的关键
    relayAct({ action: 'type', text, index: targetIndex() });
    relayInput.value = '';
  });
  relayInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      relayTypeBtn.click();
    }
  });

  refreshBtn.addEventListener('click', async () => {
    refreshBtn.disabled = true;
    clear(imgBox).append(h('span.spin'));
    statusLine.textContent = '正在刷新二维码…';
    try {
      const resp = await api.post('/api/accounts/qr/start', { force_new: true });
      if (!resp.ok) throw resp;
      showQr(resp);
    } catch (err) {
      clear(imgBox).append(h('div.small.err', { text: errText(err) }));
      statusLine.textContent = '获取二维码失败';
      refreshBtn.disabled = false;
    }
  });

  const showQr = (resp) => {
    clear(imgBox).append(h('img', { src: resp.image, alt: '登录二维码' }));
    statusLine.textContent = '用手机抖音扫一扫（约 2 分钟内有效）';
    refreshBtn.disabled = false;
    startQrPoll();
  };

  try {
    const resp = await api.post('/api/accounts/qr/start');
    if (!resp.ok) throw resp;
    showQr(resp);
  } catch (err) {
    clear(imgBox).append(h('div.small.err', { text: errText(err) }));
    statusLine.textContent = '获取二维码失败';
    refreshBtn.disabled = false;
  }
}

function startQrPoll() {
  stopQrPoll();
  state.qrTimer = setInterval(async () => {
    if (!qrModalEl) return stopQrPoll();
    // 上一次轮询还没回来就跳过这一拍 —— 避免请求堆积（那是「卡住」的主因之一）。
    // 但超过 25 秒就当作它已经死了，放行新的，避免把自己锁死。
    if (qrPollBusy && Date.now() - qrPollBusyAt < 25000) return;
    qrPollBusy = true;
    qrPollBusyAt = Date.now();
    try {
      const resp = await api.get('/api/accounts/qr/poll');

      if (resp.state === 'success') {
        stopQrPoll();
        clear(qrModalEl).append(
          h('div.modal', {}, [
            h('h3', { text: '✅ 登录成功' }),
            h('div.sub', { text: '登录态已保存，现在可以自动发送了。' }),
            h('div', {}, [
              h('button.primary', {
                text: '好',
                onclick: () => {
                  qrModalEl?.remove();
                  qrModalEl = null;
                  refresh(['overview']).then(renderCurrent);
                },
              }),
            ]),
          ])
        );
        toast('登录成功，账号已绑定', 'ok');
        return;
      }

      if (resp.state === 'expired') {
        stopQrPoll();
        const sub = $('.sub', qrModalEl);
        if (sub) sub.textContent = '二维码已失效 —— 点「刷新二维码」再试一次';
        return;
      }

      if (resp.state === 'idle') {
        stopQrPoll();
        const sub = $('.sub', qrModalEl);
        if (sub) sub.textContent = resp.detail || '扫码会话已结束';
        return;
      }

      // ⚠️ 其余所有状态（awaiting_scan / scanned / verifying / initializing /
      // needs_verify / retrying / **error** / 任何没见过的）都**继续轮询**。
      //
      // 以前这里对 `error` 调了 stopQrPoll()，结果是：一次瞬时失败
      // （浏览器忙、操作超时、网络抖一下）就把轮询永久停掉 ——
      // 用户扫了码、手机也确认了，界面却永远停在「等待扫码」，再没任何反应。
      // 单次检查失败必须能自动重试。
      const sub = $('.sub', qrModalEl);
      if (sub && resp.detail) sub.textContent = resp.detail;

      // 「登录页实时画面」面板**始终打开**，不再靠状态判断来决定是否显示。
      // 原因：二次验证页的文案/结构我们只能靠猜，一旦没猜中，
      // 用户就完全没有入口了（实测踩过：扫码后无任何地方可操作）。
      // 现在无论处于什么状态，用户都能看到页面真实样子并亲手操作。
      qrShowRelay?.();
    } catch (err) {
      // 轮询出错不关弹窗，继续试 —— 网络抖一下不该打断扫码
      console.warn('轮询失败', err);
    } finally {
      qrPollBusy = false;
    }
  }, 1500);
}

function stopQrPoll() {
  if (state.qrTimer) {
    clearInterval(state.qrTimer);
    state.qrTimer = null;
  }
  // 「登录页操作」的自动刷新截图也一起停掉（别让它在弹窗关掉后继续跑）
  if (qrRelayTimer) {
    clearInterval(qrRelayTimer);
    qrRelayTimer = null;
  }
}

/* --- 火花图标（SVG 模板在 common.js，app.js / send.js 共用）----------------- */

/** 火花状态节点：火焰 + 天数（或「重燃中 2/3」恢复胶囊）。文本为空则返回 null。 */
function streakNode(text) {
  if (!text) return null;
  const revive = String(text).includes('重燃');
  return h('span.streak', { class: revive ? 'revive' : '', title: '火花状态' }, [
    h('span', { html: huohuaFlameSvg() }),
    h('span.num', { text }),
  ]);
}

/** 头像节点：有头像走按需接口 /api/contacts/avatar?name=，加载失败回落到名字首字。 */
function avatarNode(contact, small = false) {
  const name = contact.name || '?';
  const initial = name.slice(0, 1).toUpperCase();
  if (!contact.has_avatar) {
    return h('div', { class: small ? 'avatar-fallback sm' : 'avatar-fallback', text: initial });
  }
  const img = h('img', {
    class: small ? 'friend-avatar sm' : 'friend-avatar',
    src: '/api/contacts/avatar?name=' + encodeURIComponent(name),
    alt: '',
    referrerpolicy: 'no-referrer',
  });
  img.addEventListener('error', () => {
    const fb = h('div', { class: small ? 'avatar-fallback sm' : 'avatar-fallback', text: initial });
    img.replaceWith(fb);
  });
  return img;
}

/* --- 收件人页 ------------------------------------------------------------- */

async function renderContacts(root) {
  const card = h('div.card', {}, [
    h(
      'h2',
      {},
      [
        '收件人',
        h('div.actions', {}, [
          h('button.small', { text: '🔍 从抖音同步', id: 'btn-sync' }),
          h('button.small', { text: '+ 手工添加', id: 'btn-add' }),
        ]),
      ]
    ),
  ]);

  root.append(card);

  // 列表放在独立容器里，翻页时只重画它
  const body = h('div');
  card.append(body);
  await paintContacts(body);

  /* 名字匹配说明 —— 这是最容易踩坑的地方，必须说清楚 */
  root.append(
    h('div.card', {}, [
      h('h2', { text: '名字要写对' }),
      h('div.small.muted', {
        text:
          '名字必须和抖音里显示的一字不差。程序刻意不做模糊匹配 —— ' +
          '「张三」和「张三丰」是两个人，发错人比发不出去严重得多。',
      }),
      h('div.field.hint', {
        text:
          '强烈建议用「从抖音同步」而不是手打：会话列表里的名字就是程序会用来匹配的名字。' +
          '如果好友改名了，重新同步一次。' +
          '同步进来的好友默认是「停用」状态（只有启用的人才会被定时任务发送），需要发给谁就单独打开那个开关。',
      }),
    ])
  );

  $('#btn-sync', root)?.addEventListener('click', () => syncContacts(() => paintContacts(body)));
  $('#btn-add', root)?.addEventListener('click', () => addContactDialog());
}

/* 收件人分页：一次只取/渲染一页，翻页才拿下一页 */
async function loadContactsPage(page) {
  // 页码兜底：后端 page 是 int，传 NaN / 空会直接 422
  const wanted = Number(page);
  const safe = Number.isInteger(wanted) && wanted >= 1 ? wanted : 1;
  const r = await api.get(`/api/contacts?page=${safe}&page_size=${state.contactsPageSize}`);
  state.contactsPage = r.page || 1;
  state.contactsData = {
    items: r.contacts || [],
    // ⚠ page 一定要存 —— pager() 靠它算「上一页 / 下一页」，
    // 漏了它 data.page - 1 会变成 NaN，请求就成了 ?page=NaN（后端 422）
    page: r.page || 1,
    pages: r.pages || 1,
    total: r.count || 0,
    enabledCount: r.enabled_count || 0,
  };
  return state.contactsData;
}

async function paintContacts(body) {
  clear(body);

  let data;
  try {
    data = await loadContactsPage(state.contactsPage);
  } catch (err) {
    if (err?.auth) throw err;
    body.append(h('div.empty', { text: '加载收件人失败：' + errText(err) }));
    return;
  }

  if (!data.total) {
    body.append(
      h('div.empty', {}, [
        h('div.big', { text: '📇' }),
        '还没有收件人',
        h('div.small.mt', { text: '点「从抖音同步」读取你的会话列表，或手工添加。' }),
      ])
    );
    return;
  }

  // 已启用的会被后端排在前面（打开开关即置顶），这里只负责翻页
  const reload = () => paintContacts(body);
  const list = h('div.list');
  for (const c of data.items) list.append(contactRow(c, reload));
  body.append(list);

  body.append(
    h('div.small.muted.mt', {
      text: `共 ${data.total} 个，其中 ${data.enabledCount} 个已启用。`,
    })
  );

  body.append(
    pager(data, (p) => {
      state.contactsPage = p;
      return reload();
    })
  );
}

function pager(data, go) {
  if (data.pages <= 1) return h('div');

  const btn = (label, page, opts = {}) =>
    h('button.small' + (opts.active ? '.primary' : ''), {
      text: label,
      disabled: opts.disabled || false,
      // 兜一层：页码必须是非负整数，别把 NaN / undefined 拼进 URL
      onclick: () => {
        const target = Number(page);
        if (Number.isInteger(target) && target >= 1) go(target);
      },
    });

  const box = h('div.pager');
  box.append(btn('‹ 上一页', data.page - 1, { disabled: data.page <= 1 }));

  const start = Math.max(1, data.page - 2);
  const end = Math.min(data.pages, start + 4);
  for (let p = start; p <= end; p += 1) {
    box.append(btn(String(p), p, { active: p === data.page }));
  }

  box.append(btn('下一页 ›', data.page + 1, { disabled: data.page >= data.pages }));
  box.append(h('span.pager-info', { text: `${data.page} / ${data.pages} 页` }));
  return box;
}

function contactRow(c, reload) {
  const toggle = h('input', {
    type: 'checkbox',
    checked: c.enabled,
    title: c.enabled ? '已启用' : '已停用',
  });

  toggle.addEventListener('change', async () => {
    try {
      await api.patch(`/api/contacts/${encodeURIComponent(c.name)}`, {
        name: c.name,
        conversation_id: c.conversation_id,
        is_group: c.is_group,
        note: c.note,
        enabled: toggle.checked,
        weight: c.weight,
      });
      c.enabled = toggle.checked;
      toast(`${c.name} 已${toggle.checked ? '启用' : '停用'}`, 'ok', 2000);
      // 重新取当前页：刚启用的人会被后端排到最前
      await reload?.();
      await refresh(['overview']);
    } catch (err) {
      toggle.checked = !toggle.checked;
      toast(errText(err), 'err');
    }
  });

  const pills = [];
  if (c.sent_today) pills.push(h('span.pill.ok', { text: '今天已发' }));
  if (c.is_group) pills.push(h('span.pill', { text: '群聊' }));

  const delBtn = h('button.small.ghost', { text: '删除', title: '从名单里移除' });
  delBtn.addEventListener('click', async () => {
    if (!confirm(`确定移除「${c.name}」吗？`)) return;
    try {
      const resp = await api.del(`/api/contacts/${encodeURIComponent(c.name)}`);
      toast(resp.detail, 'ok');
      await reload?.();
      await refresh(['overview']);
    } catch (err) {
      toast(errText(err), 'err');
    }
  });

  // 头像：有真实头像走按需接口，否则用名字首字兜底
  const avatar = avatarNode(c, true);

  const grow = [
    h('div.name-row', {}, [
      h('span.name', { title: c.name, text: c.name }),
      streakNode(c.streak),
    ]),
  ];
  // 备注有才显示 —— 「已启用 / 已停用」不再用文字啰嗦，左边的开关已经说明了一切
  if (c.note) grow.push(h('div.meta', { text: c.note }));

  return h('div.list-item', {}, [
    h('label.switch', {}, [toggle, h('span.track')]),
    avatar,
    h('div.grow', {}, grow),
    h('div.inline', {}, pills),
    h('div.tools', {}, [delBtn]),
  ]);
}

async function syncContacts(reload) {
  const btn = $('#btn-sync');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '同步中…';
  }
  try {
    const resp = await api.post('/api/contacts/sync', { limit: 400, merge: true });
    if (resp.ok) toast(`同步完成：${resp.detail}`, 'ok');
    else toast(resp.detail, 'warn', 6000);
    state.contactsPage = 1; // 同步后回到第一页
    await refresh(['overview']);
    if (reload) await reload();
  } catch (err) {
    toast(errText(err), 'err', 8000);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '🔍 从抖音同步';
    }
  }
}

function addContactDialog() {
  const nameInput = h('input', { type: 'text', placeholder: '要和抖音里显示的一模一样' });
  const noteInput = h('input', { type: 'text', placeholder: '选填，比如「大学同学」' });

  const modal = h('div.modal', {}, [
    h('h3', { text: '添加收件人' }),
    h('div.field', {}, [h('label', { text: '名字' }), nameInput]),
    h('div.field', { style: { textAlign: 'left' } }, [h('label', { text: '备注' }), noteInput]),
    h('div.inline', { style: { justifyContent: 'center' } }, [
      h('button', { text: '取消', onclick: () => backdrop.remove() }),
      h('button.primary', { text: '添加', onclick: submit }),
    ]),
  ]);

  const backdrop = h('div.modal-backdrop', {}, [modal]);
  document.body.append(backdrop);
  setTimeout(() => nameInput.focus(), 50);

  async function submit() {
    const name = nameInput.value.trim();
    if (!name) return toast('名字不能为空', 'warn');
    try {
      const resp = await api.post('/api/contacts', {
        name,
        note: noteInput.value.trim() || null,
        enabled: true,
        weight: 1,
      });
      toast(resp.detail, 'ok');
      backdrop.remove();
      state.contactsPage = 1; // 新加的人默认启用，会被排到第一页最前
      await refresh(['overview']);
      await renderCurrent();
    } catch (err) {
      toast(errText(err), 'err');
    }
  }
}

/* --- 历史页 --------------------------------------------------------------- */

async function renderRuns(root) {
  /* 成功率一览 */
  const stats = successStatsCard(30);
  if (stats) root.append(stats);

  /* 按天 */
  const daysCard = h('div.card', {}, [h('h2', { text: '按天' })]);
  if (!state.days.length) {
    daysCard.append(h('div.empty', {}, [h('div.big', { text: '📭' }), '还没有运行记录']));
  } else {
    const list = h('div.list');
    for (const d of state.days) {
      const statusPill = d.success
        ? h('span.pill.ok', { text: '成功' })
        : d.attempts > 0
          ? h('span.pill.err', { text: `失败 ${d.attempts} 次` })
          : h('span.pill', { text: '无尝试' });

      list.append(
        h('div.list-item', {}, [
          h('div.grow', {}, [
            h('div.name', { text: dayLabel(d.date, state.overview?.today || '') }),
            h('div.meta', {
              text: [
                d.date,
                d.first_success_at ? `首次成功 ${shortTime(d.first_success_at)}` : '',
                d.sent_to?.length ? `→ ${d.sent_to.join('、')}` : '',
              ]
                .filter(Boolean)
                .join(' · '),
            }),
          ]),
          statusPill,
        ])
      );
    }
    daysCard.append(list);
  }
  root.append(daysCard);

  /* 按次 */
  const runsCard = h('div.card', {}, [h('h2', { text: '按次（最近 50 条）' })]);
  if (!state.runs.length) {
    runsCard.append(h('div.empty', { text: '还没有单次记录' }));
  } else {
    const list = h('div.list');
    for (const r of state.runs) {
      const ok = r.failed === 0 && r.succeeded > 0;
      const detail = h('div.hidden.mt');

      const item = h('div.list-item', { style: { cursor: 'pointer' } }, [
        h('div.grow', {}, [
          h('div.name', { text: r.summary || '—' }),
          h('div.meta', {
            text: `${shortTime(r.finished_at)} · ${r.total} 个收件人${r.dry_run ? ' · 演练' : ''}`,
          }),
        ]),
        h('span.pill', { class: ok ? 'ok' : r.failed ? 'err' : '', text: ok ? '全成功' : r.failed ? `${r.failed} 失败` : '已跳过' }),
      ]);

      item.addEventListener('click', async () => {
        if (!detail.classList.contains('hidden')) {
          detail.classList.add('hidden');
          return;
        }
        try {
          const full = await api.get(`/api/runs/${encodeURIComponent(r.run_id)}`);
          clear(detail);
          detail.append(renderReportDetail(full));
          detail.classList.remove('hidden');
        } catch (err) {
          clear(detail).append(h('div.pill.err', { text: errText(err) }));
          detail.classList.remove('hidden');
        }
      });

      const wrapper = h('div', {}, [item, detail]);
      list.append(wrapper);
    }
    runsCard.append(list);
  }
  root.append(runsCard);
}

/** 渲染一次运行的明细。首页和历史页共用 */
function renderReportDetail(report) {
  const box = h('div', { style: { marginTop: '10px' } });

  box.append(
    h('div.small.muted.mb', {
      text: `${shortTime(report.finished_at)} · ${report.summary || ''}${report.dry_run ? ' · 演练模式' : ''}`,
    })
  );

  if (!report.outcomes?.length) {
    box.append(h('div.small.warn', { text: '这次没有给任何人发送。' }));
    return box;
  }

  for (const o of report.outcomes) {
    const cls = o.status === 'success' ? 'ok' : o.status === 'failed' ? 'err' : o.status === 'uncertain' ? 'warn' : '';
    const icon = o.status === 'success' ? '✅' : o.status === 'failed' ? '❌' : o.status === 'uncertain' ? '❓' : '⏭';

    box.append(
      h('div', { style: { padding: '7px 0', borderBottom: '1px solid var(--border)' } }, [
        h('div.inline', {}, [
          h('span', { text: icon }),
          h('strong', { text: o.name }),
          h('span.pill', { class: cls, text: o.status_label || o.status }),
          o.failure_kind_label ? h('span.pill', { text: o.failure_kind_label }) : null,
        ]),
        o.detail ? h('div.small.muted', { style: { marginTop: '3px' }, text: o.detail }) : null,
        o.failure_hint ? h('div.small', { style: { marginTop: '3px', color: 'var(--warn)' }, text: `建议：${o.failure_hint}` }) : null,
      ])
    );
  }
  return box;
}

/* --- 设置页 --------------------------------------------------------------- */

async function renderSettings(root) {
  const cfg = state.config;

  /* 自检 */
  const checkCard = h('div.card', {}, [h('h2', { text: '系统自检' })]);
  const checkBtn = h('button.primary', { text: '跑一次自检' });
  const checkOut = h('div.mt');

  const runCheck = async () => {
    checkBtn.disabled = true;
    clear(checkOut).append(h('span.spin'), ' 检查中…');
    try {
      const resp = await api.post('/api/system/check');
      state.check = resp;
      renderCheckResult(checkOut, resp);
    } catch (err) {
      clear(checkOut).append(h('div.pill.err', { text: errText(err) }));
    } finally {
      checkBtn.disabled = false;
    }
  };
  checkBtn.addEventListener('click', runCheck);

  checkCard.append(h('div.inline', {}, [checkBtn]), checkOut);
  if (state.check) renderCheckResult(checkOut, state.check);
  root.append(checkCard);

  /* 通知测试 */
  const notifyCard = h('div.card', {}, [h('h2', { text: '通知通道' })]);
  const notifyOut = h('div.mt');

  if (cfg) {
    notifyCard.append(
      h('dl.kv.mb', {}, [
        h('dt', { text: '已配置' }),
        h('dd', { text: cfg.notify.channels.length ? cfg.notify.channels.join('、') : '（无）' }),
        h('dt', { text: '告警门槛' }),
        h('dd', { text: `连续失败 ${cfg.notify.warn_threshold} 天警告 / ${cfg.notify.critical_threshold} 天严重` }),
      ])
    );
  }

  const testBtn = h('button', { text: '发送测试通知' });
  testBtn.addEventListener('click', async () => {
    testBtn.disabled = true;
    clear(notifyOut).append(h('span.spin'), ' 发送中…');
    try {
      const resp = await api.post('/api/system/notify/test');
      clear(notifyOut);
      if (!resp.results?.length) {
        notifyOut.append(h('div.pill.warn', { text: resp.detail }));
        if (resp.hint) notifyOut.append(h('div.small.muted.mt', { text: resp.hint }));
      } else {
        for (const r of resp.results) {
          notifyOut.append(
            h('div.inline.mt', {}, [
              h('span.pill', { class: r.ok ? 'ok' : 'err', text: r.channel }),
              h('span.small.muted', { text: r.ok ? '发送成功' : r.detail }),
            ])
          );
        }
      }
    } catch (err) {
      clear(notifyOut).append(h('div.pill.err', { text: errText(err) }));
    } finally {
      testBtn.disabled = false;
    }
  });
  notifyCard.append(h('div.inline', {}, [testBtn]), notifyOut);
  notifyCard.append(
    h('div.field.hint', {
      text: '点一下就能确认通道是否配好 —— 比等到真出问题才发现通知发不出去强。',
    })
  );
  root.append(notifyCard);

  /* 配置概览 */
  if (cfg) {
    const cfgCard = h('div.card', {}, [h('h2', { text: '运行配置' })]);
    cfgCard.append(
      h('dl.kv', {}, [
        h('dt', { text: '浏览器' }),
        h('dd', { text: cfg.browser.describe }),
        h('dt', { text: '发送' }),
        h('dd', { text: cfg.send.describe }),
        h('dt', { text: '调度器' }),
        h('dd', { text: cfg.scheduler.describe }),
        h('dt', { text: '数据目录' }),
        h('dd', { text: cfg.paths.data_dir }),
        h('dt', { text: '日志目录' }),
        h('dd', { text: cfg.paths.log_dir }),
        h('dt', { text: '访问令牌' }),
        h('dd', { text: cfg.workbench.token_set ? '已设置' : '⚠ 未设置（无鉴权）' }),
        h('dt', { text: 'IP 白名单' }),
        h('dd', { text: cfg.workbench.allowed_ips.length ? cfg.workbench.allowed_ips.join(', ') : '未限制' }),
      ])
    );
    if (cfg.problems?.length) {
      cfgCard.append(
        h('div.mt', {}, [
          h('div.small', { style: { color: 'var(--warn)' }, text: `⚠️ ${cfg.problems.length} 个配置问题：` }),
          h('ul.small.muted', { style: { margin: '6px 0 0', paddingLeft: '20px' } }, cfg.problems.map((p) => h('li', { text: p }))),
        ])
      );
    }
    root.append(cfgCard);
  }

  /* 日志 */
  const logCard = h('div.card', {}, [h('h2', { text: '日志' })]);
  const logSel = h('select');
  const logPre = h('pre.logs', { text: '（点「刷新日志」查看）' });
  const logBtn = h('button.small', { text: '刷新日志' });

  const loadLogs = async () => {
    logBtn.disabled = true;
    logPre.textContent = '读取中…';
    try {
      const resp = await api.get(`/api/system/logs?lines=250${logSel.value ? `&file=${encodeURIComponent(logSel.value)}` : ''}`);
      logPre.textContent = resp.lines?.length ? resp.lines.join('\n') : '（日志为空）';
      logPre.scrollTop = logPre.scrollHeight;

      if (resp.available?.length && logSel.options.length !== resp.available.length) {
        clear(logSel);
        for (const f of resp.available) {
          logSel.append(h('option', { value: f.name, text: `${f.name}（${f.size_human}）` }));
        }
      }
    } catch (err) {
      logPre.textContent = `读取失败：${errText(err)}`;
    } finally {
      logBtn.disabled = false;
    }
  };
  logBtn.addEventListener('click', loadLogs);
  logSel.addEventListener('change', loadLogs);

  /* 导出：把当前日志打包成 txt / csv / json 下载，方便丢给 AI 分析 */
  const expTxt = h('button.small.ghost', { text: '导出 TXT' });
  const expCsv = h('button.small.ghost', { text: '导出 CSV' });
  const expJson = h('button.small.ghost', { text: '导出 JSON' });
  expTxt.addEventListener('click', () => downloadLogs('txt', logSel.value));
  expCsv.addEventListener('click', () => downloadLogs('csv', logSel.value));
  expJson.addEventListener('click', () => downloadLogs('json', logSel.value));

  logCard.append(
    h('div.inline.mb', {}, [logSel, logBtn]),
    h('div.inline.mb', {}, [
      expTxt,
      expCsv,
      expJson,
      h('span.small.muted', { text: '（导出后可直接丢给 AI 分析）' }),
    ]),
    logPre
  );
  root.append(logCard);
}

/** 把当前日志文件下载成本地文件。format: txt / csv / json。 */
async function downloadLogs(format, file) {
  let resp;
  try {
    resp = await api.get(`/api/system/logs?lines=5000${file ? `&file=${encodeURIComponent(file)}` : ''}`);
  } catch (err) {
    toast('读取日志失败：' + errText(err), 'err');
    return;
  }
  const lines = resp.lines || [];
  const stamp = (resp.modified_at || '').replace(/[: ]/g, '-') || 'log';
  const base = `huohua-${(file || 'log')}-${stamp}`;

  let content;
  let ext;
  let mime;
  if (format === 'json') {
    content = JSON.stringify(
      {
        file: resp.file,
        line_count: resp.line_count,
        size_bytes: resp.size_bytes,
        modified_at: resp.modified_at,
        lines,
      },
      null,
      2
    );
    ext = 'json';
    mime = 'application/json';
  } else if (format === 'csv') {
    const rows = ['line'];
    for (const ln of lines) rows.push(csvCell(ln));
    content = rows.join('\n');
    ext = 'csv';
    mime = 'text/csv';
  } else {
    content = lines.join('\n');
    ext = 'txt';
    mime = 'text/plain';
  }

  const blob = new Blob([content], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${base}.${ext}`;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`已导出 ${lines.length} 行日志（${ext.toUpperCase()}）`, 'ok');
}

/** CSV 单字段转义：含逗号/引号/换行的用双引号包裹，内部引号翻倍。 */
function csvCell(value) {
  const v = String(value == null ? '' : value);
  if (/[",\n\r]/.test(v)) return '"' + v.replace(/"/g, '""') + '"';
  return v;
}

function renderCheckResult(container, resp) {
  clear(container);
  const s = resp.summary;
  container.append(
    h('div.mb', {}, [
      h('span.pill', {
        class: s.fail === 0 ? 'ok' : 'err',
        text: s.fail === 0 ? `健康（${s.ok} 项通过${s.warn ? `，${s.warn} 项提醒` : ''}）` : `${s.fail} 项失败`,
      }),
    ])
  );

  for (const c of resp.checks) {
    const icon = c.status === 'ok' ? '✅' : c.status === 'warn' ? '⚠️' : '❌';
    container.append(
      h('div.check-item', {}, [
        h('div.ci', { text: icon }),
        h('div', {}, [
          h('div.cn', { text: c.name }),
          c.detail ? h('div.cd', { text: c.detail }) : null,
          c.hint ? h('div.ch', { text: c.hint }) : null,
        ]),
      ])
    );
  }
}

/* ==========================================================================
   4. 路由与初始化
   ========================================================================== */

const TABS = [
  { id: 'home', label: '首页', icon: '🏠', render: renderHome },
  { id: 'account', label: '账号', icon: '👤', render: renderAccount },
  { id: 'contacts', label: '收件人', icon: '📇', render: renderContacts },
  { id: 'tasks', label: '任务', icon: '⏰', render: renderTasks },
  { id: 'runs', label: '历史', icon: '📊', render: renderRuns },
  { id: 'settings', label: '设置', icon: '⚙️', render: renderSettings },
];

/** 拉取数据。传具体名字可以只刷新一部分 */
async function refresh(what) {
  const want = (name) => !what || what.includes(name);
  const jobs = [];

  if (want('overview') || want('config')) {
    jobs.push(
      api.get('/api/system/overview').then((r) => {
        state.overview = r;
      })
    );
  }
  if (want('config')) {
    jobs.push(
      api.get('/api/system/config').then((r) => {
        state.config = r;
      })
    );
  }
  if (want('contacts')) {
    jobs.push(loadContactsPage(state.contactsPage));
  }
  if (want('tasks')) {
    jobs.push(
      api.get('/api/tasks').then((r) => {
        state.tasks = r;
      })
    );
  }
  if (want('days')) {
    jobs.push(
      api.get('/api/runs/days?limit=60').then((r) => {
        state.days = r.days || [];
      })
    );
  }
  if (want('runs')) {
    jobs.push(
      api.get('/api/runs?limit=50').then((r) => {
        state.runs = r.runs || [];
      })
    );
  }

  const results = await Promise.allSettled(jobs);
  const failed = results.filter((r) => r.status === 'rejected');
  if (failed.length) {
    const first = failed[0].reason;
    // 401 交给外层统一处理，不要在这里弹两次
    if (!first?.auth) throw first;
    throw first;
  }
}

async function renderCurrent() {
  const main = $('#main');
  clear(main);
  const tab = TABS.find((t) => t.id === state.tab) || TABS[0];

  try {
    await tab.render(main);
  } catch (err) {
    if (err?.auth) {
      showTokenGate('令牌不正确或已失效，请重新输入。');
      return;
    }
    clear(main).append(
      h('div.card', {}, [
        h('h2', { text: '出错了' }),
        h('div.pill.err', { text: errText(err) }),
        h('div.mt', {}, [
          h('button', {
            text: '重试',
            onclick: () => renderCurrent(),
          }),
        ]),
      ])
    );
  }
  updateTabs();
}

function updateTabs() {
  for (const btn of $$('#tabs button')) {
    btn.classList.toggle('active', btn.dataset.tab === state.tab);
  }
}

function switchTab(id) {
  state.tab = id;
  location.hash = id;
  renderCurrent();
}

function showTokenGate(message) {
  document.body.innerHTML = '';
  const input = h('input', { type: 'password', placeholder: '粘贴你的访问令牌' });

  const body = h('div.app', {}, [
    h('div.card', { style: { marginTop: '64px', maxWidth: '440px', margin: '64px auto 0' } }, [
      h('h2', { text: '需要访问令牌' }),
      h('div.small.muted.mb', {
        text: message || '这个工作台受令牌保护。令牌就是你 .env 里 HUOHUA_TOKEN 的值。',
      }),
      h('div.field', {}, [input]),
      h('div.inline', {}, [
        h('button.primary', {
          text: '进入',
          onclick: () => {
            const v = input.value.trim();
            if (!v) return toast('令牌不能为空', 'warn');
            api.token = v;
            location.reload();
          },
        }),
      ]),
      h('div.field.hint', {
        text: '令牌存在浏览器本地（localStorage），不会发给除本服务外的任何地方。',
      }),
    ]),
  ]);

  document.body.append(body);
  setTimeout(() => input.focus(), 50);
}

async function boot() {
  /* 从 URL 里吸收令牌 —— 方便首次部署时直接用带令牌的链接打开 */
  const params = new URLSearchParams(location.search);
  const urlToken = params.get('token');
  if (urlToken) {
    api.token = urlToken;
    // 立刻把令牌从地址栏抹掉，避免被截图或分享时泄露
    params.delete('token');
    const qs = params.toString();
    history.replaceState(null, '', location.pathname + (qs ? `?${qs}` : '') + location.hash);
  }

  /* 顶栏 */
  const versionEl = h('span.version', { text: '' });
  const header = h('header.top', {}, [
    h('h1', {}, ['🔥', '火花守护']),
    h('div.inline', {}, [versionEl]),
  ]);

  /* 底部导航 */
  const nav = h('nav.tabs', { id: 'tabs' });
  for (const tab of TABS) {
    const btn = h('button', { dataset: { tab: tab.id } }, [
      h('span.ti', { text: tab.icon }),
      h('span', { text: tab.label }),
    ]);
    btn.addEventListener('click', () => switchTab(tab.id));
    nav.append(btn);
  }

  const main = h('main', { id: 'main' });
  const toaster = h('div', { id: 'toaster' });

  document.body.append(h('div.app', {}, [header, nav, main]), toaster);

  /* 初始 tab 从 hash 来 */
  const hash = location.hash.replace('#', '');
  if (TABS.some((t) => t.id === hash)) state.tab = hash;

  /* 从 ?token= 吸收令牌（「复制扫码链接 / 打开群发页」都靠它） */
  huohuaAbsorbTokenFromUrl();

  /* 没有令牌就直接显示门禁，不要先打一堆 401 */
  if (!api.token) {
    showTokenGate();
    return;
  }

  try {
    await refresh(['overview', 'config', 'tasks', 'days', 'runs']);
  } catch (err) {
    if (err?.auth) return showTokenGate(errText(err));
    clear(main).append(
      h('div.card', {}, [
        h('h2', { text: '无法加载' }),
        h('div.pill.err', { text: errText(err) }),
        h('div.mt', {}, [h('button', { text: '重试', onclick: () => location.reload() })]),
      ])
    );
    return;
  }

  if (state.overview) versionEl.textContent = `v${state.overview.version}`;

  await renderCurrent();

  /* 定时刷新概览 —— 手机上放着不动时也能自动更新 */
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    if (document.hidden) return;
    if (qrModalEl) return; // 扫码时不干扰
    try {
      await refresh(['overview', 'days']);
      if (state.tab === 'home' || state.tab === 'runs') await renderCurrent();
      else if (state.overview) versionEl.textContent = `v${state.overview.version}`;
    } catch {
      /* 静默失败：这是后台轮询，弹提示太吵 */
    }
  }, 30000);

  /* 任务进度高频轮询（仅首页）：让「正在给谁发」实时跳动 */
  if (state.runProgressTimer) clearInterval(state.runProgressTimer);
  state.runProgressTimer = setInterval(async () => {
    if (document.hidden) return;
    if (state.tab !== 'home') return;
    try {
      const p = await api.get('/api/system/run-progress');
      state.runProgress = p;
      const slot = document.getElementById('runProgressSlot');
      if (slot) renderRunProgress(slot, p);
    } catch {
      /* 静默失败 */
    }
  }, 4000);

  window.addEventListener('hashchange', () => {
    const id = location.hash.replace('#', '');
    if (TABS.some((t) => t.id === id) && id !== state.tab) switchTab(id);
  });
}

document.addEventListener('DOMContentLoaded', boot);
