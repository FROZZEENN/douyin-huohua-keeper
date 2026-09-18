// 任务页渲染（从 app.js 拆出，纯搬迁，行为不变）。
// 本文件导出的 renderTasks / successStatsCard 被 app.js 引用，
// 故必须在 app.js 之前加载（见 index.html 的 <script> 顺序）。
// state / api / h / clear / toast / errText / refresh /  来自 app.js 或 common.js 的全局作用域。

async function renderTasks(root) {
  const t = state.tasks;
  if (!t) {
    root.append(h('div.card', {}, [h('div.empty', { text: '加载中…' })]));
    return;
  }

  /* 发送时间 */
  const s = t.schedule;
  const enabledSwitch = h('input', { type: 'checkbox', checked: s.enabled });
  const hourInput = h('input', { type: 'number', min: '0', max: '23', value: String(s.hour) });
  const minInput = h('input', { type: 'number', min: '0', max: '59', value: String(s.minute) });
  const jitterInput = h('input', { type: 'number', min: '0', max: '720', value: String(s.jitter_minutes) });

  const previewLine = h('div.field.hint', { text: s.describe });

  const updatePreview = () => {
    const hour = Number(hourInput.value) || 0;
    const minute = Number(minInput.value) || 0;
    const jitter = Number(jitterInput.value) || 0;
    const total = hour * 60 + minute + jitter;
    const endH = Math.floor(total / 60) % 24;
    const endM = total % 60;
    previewLine.textContent =
      jitter === 0
        ? `每天 ${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
        : `每天 ${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')} 起 ${jitter} 分钟内随机（约至 ${String(endH).padStart(2, '0')}:${String(endM).padStart(2, '0')}）`;
  };
  [hourInput, minInput, jitterInput].forEach((el) => el.addEventListener('input', updatePreview));

  const saveBtn = h('button.primary', { text: '保存时间' });
  const nextLine = h('div.small.muted.mt', { text: '' });

  saveBtn.addEventListener('click', async () => {
    saveBtn.disabled = true;
    try {
      const resp = await api.put('/api/tasks/schedule', {
        enabled: enabledSwitch.checked,
        hour: Number(hourInput.value),
        minute: Number(minInput.value),
        jitter_minutes: Number(jitterInput.value),
        timezone: s.timezone,
      });
      toast(resp.detail, 'ok');
      const nr = resp.next_run;
      if (nr?.human) nextLine.textContent = nr.human;
      else if (nr?.detail) nextLine.textContent = nr.detail;
      await refresh(['tasks', 'overview']);
    } catch (err) {
      toast(errText(err), 'err', 8000);
    } finally {
      saveBtn.disabled = false;
    }
  });

  const timeCard = h('div.card', {}, [
    h('h2', {}, [
      '发送时间',
      h('label.switch', {}, [enabledSwitch, h('span.track')]),
    ]),
    h('div.row', {}, [
      h('div.field', {}, [h('label', { text: '时' }), hourInput]),
      h('div.field', {}, [h('label', { text: '分' }), minInput]),
      h('div.field', {}, [h('label', { text: '随机范围（分钟）' }), jitterInput]),
    ]),
    previewLine,
    h('div.field.hint', {
      text:
        '「随机范围」是在设定时间之后随机挑一个时刻发。' +
        '比如 10:30 + 25，实际会在 10:30~10:55 之间随机一个点发出去。' +
        '留一点抖动比准点发送自然。',
    }),
    h('div.inline.mt', {}, [saveBtn]),
    nextLine,
  ]);
  root.append(timeCard);

  /* 时区提示 —— 错了会导致 8 小时偏差 */
  root.append(
    h('div.card', {}, [
      h('h2', { text: '时区' }),
      h('div.spread', {}, [
        h('div', {}, [
          h('div.mono', { text: s.timezone }),
          h('div.small.muted', { text: '火花按自然日计算，时区不对会直接导致某天漏发。' }),
        ]),
        h('span.pill', { class: s.timezone === 'Asia/Shanghai' ? 'ok' : 'warn', text: s.timezone === 'Asia/Shanghai' ? '正常' : '注意' }),
      ]),
      h('div.field.hint', {
        text: '时区由环境变量 HUOHUA_TZ 决定，改它需要重启服务。默认 Asia/Shanghai。',
      }),
    ])
  );

  /* 消息池 */
  const msgList = h('div.list');
  const renderMsgs = () => {
    clear(msgList);
    if (!t.messages.length) {
      msgList.append(
        h('div.empty', {}, [
          h('div.big', { text: '💬' }),
          '消息池是空的',
          h('div.small.mt', { text: '空的时候会用默认文案「早」。建议至少配一条。' }),
        ])
      );
      return;
    }
    t.messages.forEach((m, idx) => {
      const ta = h('textarea', { placeholder: '输入文案', value: m.content || '' });
      ta.addEventListener('input', () => {
        t.messages[idx].content = ta.value;
      });

      const upBtn = h('button.small.ghost', { text: '↑', title: '上移', disabled: idx === 0 });
      const downBtn = h('button.small.ghost', { text: '↓', title: '下移', disabled: idx === t.messages.length - 1 });
      const delBtn = h('button.small.ghost', { text: '✕', title: '删除' });

      upBtn.addEventListener('click', () => {
        [t.messages[idx - 1], t.messages[idx]] = [t.messages[idx], t.messages[idx - 1]];
        renderMsgs();
      });
      downBtn.addEventListener('click', () => {
        [t.messages[idx + 1], t.messages[idx]] = [t.messages[idx], t.messages[idx + 1]];
        renderMsgs();
      });
      delBtn.addEventListener('click', () => {
        t.messages.splice(idx, 1);
        renderMsgs();
      });

      msgList.append(
        h('div.msg-item', {}, [
          ta,
          h('div.tools', {}, [upBtn, downBtn, delBtn]),
        ])
      );
    });
  };
  renderMsgs();

  const addMsgBtn = h('button.small', { text: '+ 加一条' });
  addMsgBtn.addEventListener('click', () => {
    t.messages.push({ kind: 'text', content: '' });
    renderMsgs();
    const tas = $$('textarea', msgList);
    tas[tas.length - 1]?.focus();
  });

  const saveMsgsBtn = h('button.primary', { text: '保存消息池' });
  saveMsgsBtn.addEventListener('click', async () => {
    saveMsgsBtn.disabled = true;
    try {
      const resp = await api.put('/api/tasks/messages', {
        messages: t.messages
          .filter((m) => (m.content || '').trim())
          .map((m) => ({ kind: 'text', content: m.content.trim() })),
      });
      toast(resp.detail, 'ok');
      await refresh(['tasks', 'overview']);
    } catch (err) {
      toast(errText(err), 'err', 8000);
    } finally {
      saveMsgsBtn.disabled = false;
    }
  });

  root.append(
    h('div.card', {}, [
      h('h2', {}, ['消息池', h('div.actions', {}, [addMsgBtn])]),
      msgList,
      h('div.inline.mt', {}, [saveMsgsBtn]),
      h('div.field.hint', {
        text: '程序每天从池子里挑文案发出去，不重复用同一条太多次。空行会被自动忽略。',
      }),
    ])
  );

  /* 轮换策略 */
  const rotSel = h('select');
  for (const opt of t.rotation_options || []) {
    rotSel.append(h('option', { value: opt.value, text: opt.label, selected: opt.value === t.rotation }));
  }
  rotSel.addEventListener('change', async () => {
    try {
      const resp = await api.put('/api/tasks/rotation', { rotation: rotSel.value });
      toast(resp.detail, 'ok', 2000);
    } catch (err) {
      toast(errText(err), 'err');
    }
  });

  const ivMin = h('input', { type: 'number', min: '0', value: String(t.interval.minimum) });
  const ivMax = h('input', { type: 'number', min: '0', value: String(t.interval.maximum) });
  const saveIv = h('button.small', { text: '保存间隔' });
  saveIv.addEventListener('click', async () => {
    try {
      const resp = await api.put('/api/tasks/interval', {
        minimum: Number(ivMin.value),
        maximum: Number(ivMax.value),
      });
      toast(resp.detail, 'ok');
    } catch (err) {
      toast(errText(err), 'err');
    }
  });

  const rotCard = h('div.card', {}, [
    h('h2', { text: '发送方式' }),
    h('div.field', {}, [
      h('label', { text: '多个收件人时怎么用这些文案' }),
      rotSel,
      h('div.hint', { text: '「每条都发」适合给一个人发多条；「随机挑一条」适合给多人各发一条。' }),
    ]),
    h('div.row', {}, [
      h('div.field', {}, [h('label', { text: '最小间隔（秒）' }), ivMin]),
      h('div.field', {}, [h('label', { text: '最大间隔（秒）' }), ivMax]),
    ]),
    h('div.inline', {}, [saveIv]),
    h('div.field.hint', {
      text: '多个收件人之间会等一段随机时间再发下一个人，避免同一秒批量发出。',
    }),
  ]);
  root.append(rotCard);

  /* 预览 */
  const prevBtn = h('button.small', { text: '预览这次会怎么发' });
  const prevOut = h('div.mt');
  prevBtn.addEventListener('click', async () => {
    prevBtn.disabled = true;
    try {
      const p = await api.get('/api/tasks/preview');
      clear(prevOut);
      prevOut.append(
        h('dl.kv', {}, [
          h('dt', { text: '发送时间' }),
          h('dd', { text: p.schedule_text }),
          h('dt', { text: '发给谁' }),
          h('dd', { text: p.recipients.length ? p.recipients.join('、') : '（无）' }),
          h('dt', { text: '用什么文案' }),
          h('dd', { text: p.messages.map((m) => m.content || `[${m.kind}]`).join(' / ') }),
        ])
      );
      for (const w of p.warnings || []) {
        prevOut.append(h('div.small.mt', { class: 'pill warn', text: `⚠️ ${w}` }));
      }
    } catch (err) {
      clear(prevOut).append(h('div.pill.err', { text: errText(err) }));
    } finally {
      prevBtn.disabled = false;
    }
  });
  root.append(h('div.card', {}, [h('h2', { text: '检查' }), h('div.inline', {}, [prevBtn]), prevOut]));
}


/* 成功率一览。数据全部来自已经加载好的 /api/runs/days，不需要新接口。 */
function successStatsCard(windowDays) {
  const days = (state.days || []).slice(0, windowDays);
  if (!days.length) return null;

  const attempted = days.filter((d) => d.attempts > 0);
  const okDays = days.filter((d) => d.success);
  const rate = attempted.length ? Math.round((okDays.length / attempted.length) * 100) : 0;
  const streak = state.overview?.streak || {};

  const num = (label, value) =>
    h('div', {}, [h('div.small.muted', { text: label }), h('div.big-num', { text: value })]);

  const card = h('div.card', {}, [h('h2', { text: `成功率（最近 ${days.length} 天）` })]);
  card.append(
    h('div.inline', { style: { gap: '22px', flexWrap: 'wrap' } }, [
      num('成功率', `${rate}%`),
      num('成功 / 有尝试', `${okDays.length} / ${attempted.length} 天`),
      num('当前连续成功', `${streak.consecutive_successes || 0} 天`),
    ])
  );

  // 一天一根柱子，老的放左边
  const bars = h('div.chart-bars');
  for (const d of [...days].reverse()) {
    const kind = d.success ? 'ok' : d.attempts > 0 ? 'err' : 'none';
    const what = d.success ? '成功' : d.attempts > 0 ? `有失败（${d.attempts} 次尝试）` : '没有尝试';
    bars.append(h('div.bar' + '.' + kind, { title: `${d.date}：${what}` }));
  }
  card.append(bars);
  card.append(
    h('div.small.muted.mt', {
      text: '每根柱子是一天：绿=成功，红=当天有失败，灰=没有尝试。鼠标悬停看日期。',
    })
  );
  return card;
}