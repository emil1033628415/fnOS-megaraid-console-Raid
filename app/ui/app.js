/*
 * megaraid-console -- LSI / Broadcom MegaRAID monitoring console for fnOS.
 * Copyright (C) 2026 emilzhang
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Free software under the GNU GPL v3 or later; see the LICENSE file.
 */
/* megaraid-console — front end (no framework, no build step) */

(function () {
  'use strict';

  var BASE = (window.RW_BASE || '').replace(/\/+$/, '');
  var API = BASE + '/api/';

  var ACTIONS = {};
  var POLL = 10000;
  var TIMER = null;
  var LAST = null;
  // A summary is ~6 storcli calls; they serialise behind one lock server-side,
  // so overlapping refreshes just queue up and make every one of them slower.
  // Only one refresh runs at a time, and a request made while busy is
  // remembered so the view still ends up fresh.
  var REFRESHING = false;
  var REFRESH_AGAIN = false;
  // True while the controller already has a background job (rebuild/copyback/
  // CC). The vendor rule is not to overlap state changes, so the buttons that
  // would do so are rendered disabled rather than failing on submit.
  var BLOCKED_NOW = false;
  var HISTORY = {};          // slot -> [rate, ...]
  var MAX_POINTS = 60;

  // ------------------------------------------------------------------ utils

  function $(id) { return document.getElementById(id); }

  // ------------------------------------------------------- progress state
  //
  // storcli takes a moment (and some commands take several seconds), and
  // nothing used to tell the operator that. Anything that can make the user
  // wait goes through these helpers so there is always a visible "执行中".

  var INFLIGHT = 0;          // HTTP requests in flight
  var TASKS = [];            // server-side background jobs, newest last
  var TASK_POLL = null;      // polls /api/job while a job runs
  var TASK_TICK = null;      // re-renders the elapsed counter every 500 ms

  function setLoadbar() {
    var on = INFLIGHT > 0 || TASKS.some(function (t) {
      return t.state === 'queued' || t.state === 'running';
    });
    $('loadbar').className = on ? 'loadbar on' : 'loadbar';
  }

  function beginRequest() { INFLIGHT += 1; setLoadbar(); }

  function endRequest() {
    INFLIGHT = Math.max(0, INFLIGHT - 1);
    setLoadbar();
  }

  function fmtElapsed(seconds) {
    var s = Math.max(0, Math.floor(Number(seconds) || 0));
    if (s < 60) return s + ' 秒';
    if (s < 3600) return Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒';
    return Math.floor(s / 3600) + ' 小时 ' + Math.floor((s % 3600) / 60) + ' 分';
  }

  function taskById(id) {
    for (var i = 0; i < TASKS.length; i++) {
      if (TASKS[i].id === id) return TASKS[i];
    }
    return null;
  }

  function taskActive(t) { return t.state === 'queued' || t.state === 'running'; }

  function newTasks() {
    return TASKS.filter(function (t) { return taskActive(t); });
  }

  function visibleTasks() {
    return TASKS.filter(function (t) {
      return taskActive(t) || (t.doneAt && (Date.now() - t.doneAt) < 9000);
    });
  }

  function renderTasks() {
    var bar = $('taskbar');
    var list = visibleTasks();
    if (!list.length) {
      bar.className = 'taskbar hidden';
      clear(bar);
      setLoadbar();
      return;
    }
    bar.className = 'taskbar';
    clear(bar);
    list.forEach(function (t) {
      var row = el('div', 'task-row ' + (t.state === 'done' ? 'ok' : (t.state === 'failed' ? 'bad' : '')));
      row.appendChild(el('span', 'spinner'));
      row.appendChild(el('span', 'task-mark'));

      var text = el('div', 'task-text');
      text.appendChild(el('div', 'task-label', t.summary || t.label || t.name));
      var meta;
      if (t.state === 'done') {
        meta = '执行完成 · 用时 ' + fmtElapsed(t.elapsed);
      } else if (t.state === 'failed') {
        meta = '执行失败：' + (t.error || '未知错误');
      } else if (t.state === 'queued') {
        meta = '已排队：控制器正在执行上一条命令…';
      } else {
        meta = '正在执行… 已用 ' + fmtElapsed(secondsSince(t));
      }
      text.appendChild(el('div', 'task-meta ' + (t.state === 'failed' ? 'bad' : ''), meta));
      row.appendChild(text);
      bar.appendChild(row);
    });
    setLoadbar();
  }

  // Elapsed is computed locally so the counter ticks between polls; the
  // authoritative value comes back with the final job record.
  function secondsSince(task) {
    if (task.state === 'done' || task.state === 'failed') {
      return task.elapsed !== undefined ? task.elapsed : (Date.now() - task.startedMs) / 1000;
    }
    return (Date.now() - task.startedMs) / 1000;
  }

  function ensureTaskTimers() {
    var ticking = visibleTasks().length > 0;
    if (ticking && !TASK_TICK) TASK_TICK = setInterval(renderTasks, 500);
    if (!ticking && TASK_TICK) { clearInterval(TASK_TICK); TASK_TICK = null; }
    if (newTasks().length && !TASK_POLL) TASK_POLL = setInterval(pollJobs, 900);
    if (!newTasks().length && TASK_POLL) { clearInterval(TASK_POLL); TASK_POLL = null; }
  }

  function applyJob(job) {
    var task = taskById(job.id);
    if (!task) {
      task = { id: job.id, startedMs: Date.now() };
      TASKS.push(task);
    }
    task.label = job.label || task.label;
    task.summary = job.summary || job.label || job.name || task.summary;
    task.name = job.name || task.name;
    task.state = job.state || task.state;
    task.error = job.error || null;
    if (job.elapsed !== undefined) task.elapsed = job.elapsed;

    if (taskActive(task)) {
      renderTasks();
      ensureTaskTimers();
      return task;
    }

    // Settle the task exactly once.
    if (!task.settled) {
      task.settled = true;
      task.doneAt = Date.now();
      task.elapsed = job.elapsed !== undefined ? job.elapsed : secondsSince(task);
      if (task.state === 'done') {
        var desc = (job.result && job.result.description) || '';
        toast('✓ ' + (task.summary || '操作') + ' 执行成功（用时 ' + fmtElapsed(task.elapsed) + '）'
          + (desc ? '\n' + desc : ''), 'ok');
        refresh();
      } else {
        toast('✗ ' + (task.summary || '操作') + ' 执行失败：' + (task.error || '未知错误'), 'bad');
      }
      loadAudit();
    }
    renderTasks();
    ensureTaskTimers();
    return task;
  }

  async function pollJobs() {
    var active = newTasks();
    if (!active.length) { ensureTaskTimers(); return; }
    for (var i = 0; i < active.length; i++) {
      try {
        var res = await api('job?id=' + encodeURIComponent(active[i].id));
        if (res && res.job) applyJob(res.job);
      } catch (e) {
        // 404 means the daemon restarted and lost the registry.
        if (e && e.status === 404 && !active[i].settled) {
          applyJob({
            id: active[i].id, label: active[i].label, summary: active[i].summary,
            state: 'failed', error: '任务状态已丢失（服务可能重启过），请刷新确认真实结果'
          });
        }
      }
    }
  }

  async function loadJobs() {
    try {
      var res = await api('jobs');
      (res.jobs || []).forEach(function (job) {
        if (job.state === 'queued' || job.state === 'running') applyJob(job);
      });
    } catch (e) { /* nothing to reconcile */ }
  }

  // Consequences the operator should not have to infer themselves: an
  // unprotected CacheVault, write-back cache on a degraded array, or auxiliary
  // data (PHY counters / rebuild progress) that could not be read this round.
  function renderAdvisories(data) {
    var host = $('advisories');
    clear(host);
    var list = [].concat(data.advisories || []);
    (data.warnings || []).forEach(function (w) {
      list.push({ level: 'warn', title: '部分数据暂不可用', body: w });
    });
    if (!list.length) { host.className = 'advisories hidden'; return; }
    host.className = 'advisories';
    list.forEach(function (a) {
      var cls = a.level === 'bad' ? 'bad' : (a.level === 'warn' ? 'warn' : '');
      var box = el('div', 'advisory ' + cls);
      var text = el('div');
      text.appendChild(el('span', 'adv-title', a.title || ''));
      text.appendChild(document.createTextNode(a.body || ''));
      box.appendChild(text);
      host.appendChild(box);
    });
  }

  // The server knows whether the controller is busy and raises the poll
  // interval accordingly; follow it so polling never competes with a rebuild.
  function applyPollHint(seconds) {
    var hint = Number(seconds);
    if (!isFinite(hint) || hint <= 0) return;
    var next = hint * 1000;
    if (next !== POLL) { POLL = next; schedule(); }
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function num(value, digits) {
    if (value === null || value === undefined || value === '') return '-';
    var n = Number(value);
    if (!isFinite(n)) return String(value);
    return digits === undefined ? String(n) : n.toFixed(digits);
  }

  function rate(value) {
    if (value === null || value === undefined) return '—';
    var n = Number(value);
    if (!isFinite(n)) return '—';
    if (n >= 1000) return n.toFixed(0);
    if (n >= 10) return n.toFixed(1);
    return n.toFixed(2);
  }

  function healthClass(health) {
    if (health === 'bad') return 'bad';
    if (health === 'warn') return 'warn';
    return 'ok';
  }

  function rateClass(value, thresholds) {
    if (value === null || value === undefined) return '';
    if (value >= (thresholds.phy_bad_rate || 50)) return 'bad';
    if (value >= (thresholds.phy_warn_rate || 5)) return 'warn';
    return 'ok';
  }

  function toast(message, kind) {
    var node = $('toast');
    node.className = 'toast ' + (kind || '');
    node.textContent = message;
    clearTimeout(node._timer);
    node._timer = setTimeout(function () { node.className = 'toast hidden'; }, kind === 'bad' ? 9000 : 4500);
  }

  function banner(kind, title, bodyHTML) {
    var node = $('banner');
    if (!kind) { node.className = 'banner hidden'; return; }
    node.className = 'banner ' + (kind === 'warn' ? 'warn' : '');
    clear(node);
    var head = el('div', 'banner-title', title);
    node.appendChild(head);
    var body = el('div');
    body.innerHTML = bodyHTML;
    node.appendChild(body);
  }

  // -------------------------------------------------------------------- api

  async function api(path, options) {
    var opts = Object.assign({ credentials: 'same-origin' }, options || {});
    beginRequest();
    try {
      var res = await fetch(API + path, opts);
      var text = await res.text();
      var data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) { data = { ok: false, error: text }; }
      if (!res.ok) {
        var msg = (data && data.error) ? data.error : ('HTTP ' + res.status);
        var err = new Error(msg);
        err.status = res.status;
        throw err;
      }
      return data;
    } finally {
      endRequest();
    }
  }

  // ---------------------------------------------------------------- renders

  function renderOverview(data) {
    var c = data.controller || {};
    var vds = data.vds || [];
    var drives = data.drives || [];
    var cv = data.cachevault || {};
    var thresholds = data.thresholds || {};

    var degraded = vds.filter(function (v) { return v.degraded; });
    var badDrives = drives.filter(function (d) { return d.health === 'bad'; });
    var warnDrives = drives.filter(function (d) { return d.health === 'warn'; });

    var host = $('overview');
    clear(host);

    function card(label, value, sub, cls, valueCls) {
      var box = el('div', 'card ' + (cls || ''));
      box.appendChild(el('div', 'card-label', label));
      var v = el('div', 'card-value ' + (valueCls || ''), value);
      if (String(value).length > 16) v.classList.add('small');
      box.appendChild(v);
      if (sub) box.appendChild(el('div', 'card-sub', sub));
      host.appendChild(box);
    }

    card('控制器', c.product || '-', (c.serial || '') + ' · ' + (c.pci || ''), '', '');
    card('固件版本', c.fw_version || '-', '包 ' + (c.fw_package || '-') + ' · BIOS ' + (c.bios_version || ''), '', '');

    // The 9361-8i usually only exposes the ROC sensor; say so rather than
    // silently showing a number whose origin is unclear.
    var temp = c.temp;
    var tempCls = !meaningful(temp) ? '' : (temp >= 60 ? 'bad' : (temp >= 50 ? 'warn' : 'ok'));
    card('控制器温度', meaningful(temp) ? (temp + '°C') : '-',
      (c.temp_source ? c.temp_source + ' 传感器' : '') +
      (c.cache_size ? ' · 缓存 ' + c.cache_size : ''),
      tempCls, tempCls);

    card('虚拟盘', vds.length + ' 个', degraded.length ? (degraded.length + ' 个降级') : '全部正常',
      degraded.length ? 'bad' : 'ok',
      degraded.length ? 'bad' : 'ok');

    card('物理盘', drives.length + ' 块',
      badDrives.length ? (badDrives.length + ' 块异常') : (warnDrives.length ? (warnDrives.length + ' 块需留意') : '全部在线'),
      badDrives.length ? 'bad' : (warnDrives.length ? 'warn' : 'ok'),
      badDrives.length ? 'bad' : (warnDrives.length ? 'warn' : 'ok'));

    // Worst PHY rate across drives — the early-warning number.  A clean link
    // prints 0, and a wall of zeroes trains the operator to ignore the row, so
    // the card only exists once something is actually happening.
    var worst = null;
    drives.forEach(function (d) {
      var r = d.phy && d.phy.inval_per_s;
      if (r === null || r === undefined || r <= 0) return;
      if (!worst || r > worst.rate) worst = { rate: r, slot: d.short };
    });
    if (worst) {
      card('最高链路误码', rate(worst.rate) + ' /秒', '来自 ' + worst.slot,
        rateClass(worst.rate, thresholds), rateClass(worst.rate, thresholds));
    }

    // The card follows the hardware: a supercapacitor on the 9361-8i class, a
    // battery on a PERC H840.  An empty `kind` plus no state means the card
    // reports neither, and saying so beats a row of dashes.
    var isBbu = String(cv.kind || '').toLowerCase() === 'bbu';
    var cvKnown = meaningful(cv.state) || meaningful(cv.model);
    var cvOk = String(cv.state || '').trim().toLowerCase() === 'optimal';
    // Some OEM firmware (the H840 among them) answers "OK" for voltage and
    // temperature instead of a number.  Put those readings on the card only
    // when they carry a value, otherwise the subtitle reads "BBU · OK · OK".
    var reading = function (v) {
      return meaningful(v) && /\d/.test(String(v));
    };
    if (!cvKnown) {
      card('电源备份', '未检测到', '控制器未上报 CacheVault / 电池', '', '');
    } else {
      card(isBbu ? '电池 (BBU)' : 'CacheVault', cv.state || '-',
        [cv.model, reading(cv.temp) ? cv.temp : '',
         isBbu && reading(cv.voltage) ? cv.voltage : '']
          .filter(meaningful).join(' · '),
        cvOk ? 'ok' : 'warn', cvOk ? 'ok' : 'warn');
    }

    // Media errors are cumulative per drive; the card exists only when one of
    // them has counted something.
    var worstMedia = null;
    drives.forEach(function (d) {
      var n = d.media_errors;
      if (typeof n !== 'number' || n <= 0) return;
      if (!worstMedia || n > worstMedia.count) worstMedia = { count: n, slot: d.short };
    });
    // Same rule as the PHY card: a healthy array shows no counter at all, so
    // the overview only carries rows that currently mean something.
    if (worstMedia) {
      card('最高磁盘介质错误', worstMedia.count + ' 个', '来自 ' + worstMedia.slot,
        'warn', 'warn');
    }

    // SMART / predictive-failure drives, listed by name.  Both are the same
    // early-warning family and both mean "replace this disk", so they share one
    // card; an unremarkable array simply has no card here.
    var smartHits = [];
    drives.forEach(function (d) {
      var flagged = /^(yes|true)$/i.test(String(d.smart_alert == null ? '' : d.smart_alert).trim());
      var predictive = typeof d.predictive_failures === 'number' && d.predictive_failures > 0;
      if (flagged || predictive) {
        smartHits.push({ short: d.short, predictive: !flagged && predictive });
      }
    });
    if (smartHits.length) {
      var names = smartHits.map(function (h) {
        return h.short + (h.predictive ? '(预测)' : '');
      });
      var shown = names.slice(0, 4).join('、')
        + (names.length > 4 ? ' 等 ' + names.length + ' 块' : '');
      card('SMART 告警磁盘', smartHits.length + ' 块', shown, 'bad', 'bad');
    }

    card('控制器状态', c.status || '-', '内存错误 可纠正 ' + num(c.memory_correctable) + ' / 不可纠正 ' + num(c.memory_uncorrectable),
      (c.status || '').toLowerCase() === 'optimal' ? 'ok' : 'warn',
      (c.status || '').toLowerCase() === 'optimal' ? 'ok' : 'warn');

    // ---- banner ----
    if (degraded.length || badDrives.length) {
      var parts = [];
      degraded.forEach(function (v) {
        parts.push('虚拟盘 <code>' + v['DG/VD'] + '</code> 处于 <code>' + v.State + '</code>（已失去冗余）');
      });
      badDrives.forEach(function (d) {
        parts.push('物理盘 <code>' + d.short + '</code> 状态 <code>' + d.State + '</code>，问题：' + (d.issues || []).join('、'));
      });
      banner('bad', '⚠ 阵列需要处理', parts.join('<br>'));
    } else if (warnDrives.length) {
      var w = warnDrives.map(function (d) {
        return '<code>' + d.short + '</code>：' + (d.issues || []).join('、');
      });
      banner('warn', '需要注意', w.join('<br>'));
    } else {
      banner(null);
    }
  }

  // storcli pads unavailable values with dashes and dates with zeroes; those
  // carry no information.  A plain "0" is kept -- for counters it is a real
  // reading (e.g. "ECC 桶计数 = 0" means "no ECC errors").
  function meaningful(value) {
    var text = (value === null || value === undefined) ? '' : String(value).trim();
    if (!text || text === '-' || text === 'N/A' || text === 'NA') return false;
    if (/^0+(\/0+)+$/.test(text)) return false;   // 00/00/00, 0/0
    return true;
  }

  function renderControllerInfo(data) {
    var c = (data && data.controller) || {};
    var host = $('controller-info');
    clear(host);
    $('ctrl-hint').textContent = [c.product, c.sas_address ? 'SAS ' + c.sas_address : '']
      .filter(Boolean).join(' · ');

    function group(title, rows) {
      var pairs = rows.filter(function (r) { return meaningful(r[1]); });
      if (!pairs.length) return;
      var box = el('div', 'ctrl-group');
      box.appendChild(el('h4', '', title));
      var kv = el('div', 'kv');
      pairs.forEach(function (r) {
        var row = el('div', 'kv-row');
        row.appendChild(el('span', 'k', r[0]));
        var v = el('span', 'v ' + (r[2] || ''), String(r[1]));
        if (String(r[1]).length > 34 && !(r[2] || '').match(/wide/)) v.classList.add('wide');
        row.appendChild(v);
        kv.appendChild(row);
      });
      box.appendChild(kv);
      host.appendChild(box);
    }

    group('标识', [
      ['控制器型号', c.product],
      ['控制器序列号', c.serial],
      ['SAS 地址', c.sas_address],
      ['PCI 地址', c.pci],
      ['板卡修订', c.revision],
      ['生产日期', c.mfg_date],
      ['返修日期', c.rework_date],
      ['厂商 / 设备 ID', [c.vendor_id, c.device_id].filter(meaningful).join(' / ')],
      ['子厂商 / 子设备 ID', [c.subvendor_id, c.subdevice_id].filter(meaningful).join(' / ')],
      ['总线号', c.bus_number],
      ['OEM', c.oem_id],
      ['安全 ID', c.safe_id]
    ]);

    group('固件与驱动', [
      ['固件版本', c.fw_version],
      ['固件包版本', c.fw_package],
      ['BIOS 版本', c.bios_version],
      ['CPLD 版本', c.cpld_version],
      ['HII 版本', c.hii_version],
      ['Ctrl-R 版本', c.ctlr_version],
      ['Preboot CLI', c.preboot_cli],
      ['NVDATA 版本', c.nvdata_version],
      ['Boot Block', c.boot_block_version],
      ['芯片修订', c.chip_revision],
      ['驱动', c.driver],
      ['驱动版本', c.driver_version]
    ]);

    group('硬件与缓存', [
      ['缓存容量', c.cache_size],
      ['CacheVault Flash', c.cv_flash_size],
      ['NVRAM 容量', c.nvram_size],
      ['Flash 容量', c.flash_size],
      ['FW 缓存占用 (MB)', c.fw_cache_size],
      ['备用电源', c.backup_units],
      ['TPM', c.tpm],
      ['主机接口', c.host_interface],
      ['设备接口', c.device_interface],
      ['后端端口数', c.backend_ports],
      ['当前模式', c.personality],
      ['支持磁盘', c.supported_drives],
      ['最大虚拟盘数', c.max_vds],
      ['默认条带', c.default_strip],
      ['支持的 RAID 级别', c.raid_levels, 'wide']
    ]);

    group('状态与温度', [
      ['控制器状态', c.status],
      ['控制器温度', meaningful(c.temp) ? (c.temp + ' °C') : '-'],
      ['温度来源', c.temp_source],
      ['ROC 温度', meaningful(c.temp_roc) ? (c.temp_roc + ' °C') : '-'],
      ['ROC 温度传感器', c.temp_sensor_roc],
      ['控制器温度传感器', c.temp_sensor_ctrl],
      ['ECC 桶计数', c.ecc_bucket],
      ['内存可纠正错误', c.memory_correctable],
      ['内存不可纠正错误', c.memory_uncorrectable],
      ['离线 VD 缓存', c.cache_preserved],
      ['安全模式启动', c.safe_mode],
      ['告警蜂鸣器', c.alarm]
    ]);

    var enc = (data && data.enclosures) || [];
    if (enc.length) {
      var rows = [];
      enc.forEach(function (e) {
        rows.push(['机框 ' + (e.EID !== undefined ? e.EID : '-') + ' 状态', e.State]);
        rows.push(['槽位 / 已装盘', [e.Slots, e.PD].filter(meaningful).join(' / ')]);
        rows.push(['电源 / 风扇', [e.PS, e.Fans].filter(meaningful).join(' / ')]);
        rows.push(['温度传感器 / 告警', [e.TSs, e.Alms].filter(meaningful).join(' / ')]);
        rows.push(['ProdID / 端口', [e.ProdID, e['Port#']].filter(meaningful).join(' / ')]);
      });
      group('机框', rows);
    }
  }

  function renderVds(vds) {
    var host = $('vds');
    clear(host);
    if (!vds || !vds.length) {
      host.appendChild(el('div', 'empty', '没有虚拟盘'));
      return;
    }
    vds.forEach(function (vd) {
      var state = String(vd.State || '');
      var cls = vd.degraded ? (/(dgr|degr|failed|offl)/i.test(state) ? 'bad' : 'warn') : 'ok';
      var box = el('div', 'vd-card ' + cls);

      var head = el('div', 'vd-head');
      var name = el('div', 'vd-name');
      name.appendChild(document.createTextNode(vd.Name && vd.Name !== '-' ? vd.Name : '虚拟盘'));
      name.appendChild(el('small', '', vd['DG/VD'] || ''));
      head.appendChild(name);
      head.appendChild(el('span', 'tag ' + cls, state));
      box.appendChild(head);

      var meta = el('div', 'vd-meta');
      [
        ['RAID 级别', vd.TYPE],
        ['容量', vd.Size],
        ['成员盘', vd.drive_count],
        ['访问模式', vd.Access],
        ['一致性', vd.Consist],
        ['缓存策略', vd.Cache],
        ['计划校验', vd.sCC]
      ].forEach(function (pair) {
        var k = el('span', 'k', pair[0]);
        var v = el('span', 'v', pair[1] || '-');
        meta.appendChild(k);
        meta.appendChild(v);
      });
      box.appendChild(meta);

      // Rebuild progress lives on the drive rows; show it here too when present.
      var rebuilding = (LAST && LAST.drives || []).filter(function (d) {
        return d.rebuild && /progress/i.test(d.rebuild.status || '');
      });
      if (rebuilding.length && vd.degraded) {
        rebuilding.forEach(function (d) {
          var pct = parseFloat(String(d.rebuild.progress).replace('%', '')) || 0;
          var bar = el('div', 'progress');
          var fill = el('i');
          fill.style.width = Math.max(0, Math.min(100, pct)) + '%';
          bar.appendChild(fill);
          box.appendChild(bar);
          box.appendChild(el('div', 'progress-label',
            d.short + ' 重建中 ' + d.rebuild.progress + ' · 剩余 ' + d.rebuild.eta));
        });
      }

      var actions = el('div', 'drive-actions');
      actions.style.marginTop = '12px';
      addActionButton(actions, 'cc_start', vd['DG/VD'] ? '/c0/v' + String(vd['DG/VD']).split('/')[1] : '', '开始一致性校验');
      addActionButton(actions, 'cc_stop', vd['DG/VD'] ? '/c0/v' + String(vd['DG/VD']).split('/')[1] : '', '停止校验');
      box.appendChild(actions);

      host.appendChild(box);
    });
  }

  function drawSpark(canvas, values, color) {
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || 260;
    var h = 30;
    canvas.width = Math.max(1, Math.round(w * dpr));
    canvas.height = Math.round(h * dpr);
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    ctx.strokeStyle = 'rgba(148,178,210,0.16)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, h - 0.5);
    ctx.lineTo(w, h - 0.5);
    ctx.stroke();

    if (!values.length) return;
    var max = Math.max.apply(null, values.concat([1]));
    var step = values.length > 1 ? w / (values.length - 1) : w;

    ctx.beginPath();
    values.forEach(function (v, i) {
      var x = i * step;
      var y = h - 2 - (v / max) * (h - 6);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.stroke();

    ctx.lineTo((values.length - 1) * step, h);
    ctx.lineTo(0, h);
    ctx.closePath();
    ctx.fillStyle = color.replace('rgb(', 'rgba(').replace(')', ',0.14)');
    ctx.fill();
  }

  function colorForRate(cls) {
    if (cls === 'bad') return 'rgb(251,113,133)';
    if (cls === 'warn') return 'rgb(251,191,36)';
    return 'rgb(52,211,153)';
  }

  function renderDrives(drives, thresholds) {
    var host = $('drives');
    clear(host);
    if (!drives || !drives.length) {
      host.appendChild(el('div', 'empty', '没有检测到物理盘'));
      return;
    }

    drives.slice().sort(function (a, b) {
      return String(a.short).localeCompare(String(b.short), undefined, { numeric: true });
    }).forEach(function (d) {
      var cls = healthClass(d.health);
      var box = el('div', 'drive-card ' + cls);

      var head = el('div', 'drive-head');
      head.appendChild(el('span', 'slot-badge', d.short));
      head.appendChild(el('span', 'drive-model', d.model || d.Model || '-'));
      head.appendChild(el('span', 'tag ' + cls, d.State || '-'));
      box.appendChild(head);
      box.appendChild(el('div', 'drive-sn', (d.sn || '-') + ' · WWN ' + (d.wwn || '-')));

      if (d.issues && d.issues.length) {
        var list = el('ul', 'issue-list');
        d.issues.forEach(function (i) {
          list.appendChild(el('li', i.indexOf(':warn') >= 0 ? 'warn' : '', i));
        });
        box.appendChild(list);
      }

      var phyRate = d.phy ? d.phy.inval_per_s : null;
      var prClass = rateClass(phyRate, thresholds || {});
      var tWarn = (thresholds && thresholds.temp_warn) || 50;
      var tBad = (thresholds && thresholds.temp_bad) || 58;
      var tCls = (d.temperature || 0) >= tBad ? 'bad' : ((d.temperature || 0) >= tWarn ? 'warn' : 'ok');

      var rows = el('div', 'drive-rows');
      [
        ['接口 / 速率', (d.Intf || '-') + ' ' + (d.link_speed || ''), ''],
        ['容量', d.Size || '-', ''],
        ['温度', d.temperature ? (d.temperature + ' °C') : '-', tCls],
        ['固件', d.firmware || '-', ''],
        ['链路误码/秒', rate(phyRate), prClass],
        ['累计误码', (d.phy ? num(d.phy.inval, 0) : '-'), ''],
        ['丢同步', (d.phy ? num(d.phy.lost_sync_total !== undefined ? d.phy.lost_sync_total : d.phy.los, 0) : '-'), (d.phy && d.phy.los_delta) ? 'bad' : ''],
        ['介质错误', num(d.media_errors, 0), d.media_errors ? 'bad' : ''],
        ['其他错误', num(d.other_errors, 0), d.other_errors ? 'warn' : ''],
        ['断言计数', num(d.predictive_failures, 0), d.predictive_failures ? 'warn' : ''],
        ['位置', d.position || '-', '']
      ].forEach(function (t) {
        var cell = el('div');
        cell.appendChild(el('span', 'k', t[0]));
        cell.appendChild(el('span', 'v ' + t[2], t[1]));
        rows.appendChild(cell);
      });
      box.appendChild(rows);

      box.appendChild(driveDetail(d));

      // PHY rate sparkline
      var canvas = el('canvas', 'spark');
      canvas.id = 'spark-' + d.short;
      box.appendChild(canvas);

      var actions = el('div', 'drive-actions');
      addActionButton(actions, 'locate_start', d.slot_path, '定位灯');
      addActionButton(actions, 'locate_stop', d.slot_path, '熄灭');
      addActionButton(actions, 'rebuild_start', d.slot_path, '重建');
      addActionButton(actions, 'rebuild_stop', d.slot_path, '停止重建');
      addActionButton(actions, 'drive_online', d.slot_path, '上线');
      addActionButton(actions, 'drive_offline', d.slot_path, '离线', 'danger');
      addActionButton(actions, 'drive_good', d.slot_path, '置为 Good');
      box.appendChild(actions);

      host.appendChild(box);

      // history + draw
      var hist = HISTORY[d.short] || (HISTORY[d.short] = []);
      if (phyRate !== null && phyRate !== undefined) {
        hist.push(Number(phyRate));
        if (hist.length > MAX_POINTS) hist.shift();
      }
      drawSpark(canvas, hist, colorForRate(prClass));
    });
  }

  // Everything storcli knows about the drive, folded into a collapsed block so
  // the card stays readable but nothing is hidden.
  function driveDetail(d) {
    var wrap = el('details', 'drive-more');
    wrap.appendChild(el('summary', '', '完整参数'));

    var rows = [
      ['EID:Slt', d['EID:Slt']],
      ['序列号', d.sn],
      ['WWN', d.wwn],
      ['型号', d.model || d.Model],
      ['厂商', d.manufacturer],
      ['NAND 厂商', d.nand_vendor],
      ['固件版本', d.firmware],
      ['固件发布号', d.firmware_release],
      ['容量', d.Size],
      ['强制容量', d.coerced_size],
      ['原始容量', d.raw_size],
      ['逻辑 / 物理扇区', [d.sector_logical, d.sector_physical].filter(meaningful).join(' / ')],
      ['链路速率', d.link_speed],
      ['设备速率', d.device_speed],
      ['接口 / 介质', [d.Intf, d.Med].filter(meaningful).join(' / ')],
      ['磁盘组 / 类型', [d.DG, d.Type].filter(meaningful).join(' / ')],
      ['旋转状态', d.Sp],
      ['连接器', d.connector],
      ['连接端口', d.port],
      ['位置', d.position],
      ['机框位置', d.enclosure_position],
      ['序列号序号', d.sequence],
      ['温度 (原始)', d.temperature_raw],
      ['介质错误', d.media_errors],
      ['其他错误', d.other_errors],
      ['断言计数', d.predictive_failures],
      ['上次断言事件序号', d.last_predictive_seq],
      ['Shield 计数', d.shield_counter],
      ['SMART 告警', d.smart_alert],
      ['写缓存', d.write_cache],
      ['FDE 类型', d.fde_type],
      ['SED 能力 / 已启用', [d.sed_capable, d.sed_enabled].filter(meaningful).join(' / ')],
      ['加密 / 锁定', [d.secured, d.locked].filter(meaningful).join(' / ')],
      ['加密擦除支持', d.crypto_erase],
      ['净化支持', d.sanitize],
      ['PI 就绪 / 已格式化 / 类型',
        [d.pi_eligible, d.pi_formatted, d.pi_type].filter(meaningful).join(' / ')],
      ['认证盘', d.certified],
      ['宽端口 / 多路径', [d.wide_port, d.multipath].filter(meaningful).join(' / ')],
      ['备用盘 / 应急备用', [d.spare, d.emergency_spare].filter(meaningful).join(' / ')]
    ];

    var kv = el('div', 'kv');
    rows.forEach(function (r) {
      if (!meaningful(r[1])) return;
      var row = el('div', 'kv-row');
      row.appendChild(el('span', 'k', r[0]));
      row.appendChild(el('span', 'v ' + (String(r[1]).length > 30 ? 'wide' : ''), String(r[1])));
      kv.appendChild(row);
    });
    wrap.appendChild(kv);
    return wrap;
  }

  function renderSettings(settings) {
    var host = $('settings');
    clear(host);
    var rows = [
      ['重建速率 Rebuild Rate', settings.rebuild_rate, ''],
      ['校验速率 CC Rate', settings.cc_rate, ''],
      ['巡检速率 PR Rate', settings.pr_rate, ''],
      ['自动重建 Auto Rebuild', settings.auto_rebuild,
        String(settings.auto_rebuild).toLowerCase() === 'on' ? 'warn' : 'ok'],
      ['PD 失效历史 Maintain PD Fail History', settings.maintain_pd_fail_history,
        String(settings.maintain_pd_fail_history).toLowerCase() === 'on' ? 'bad' : 'ok'],
      ['活动灯定位', settings.activity_for_locate, ''],
      ['背板类型', settings.auto_detect_backplane, ''],
      ['暴露背板设备', settings.expose_enclosure, '']
    ];
    rows.forEach(function (r) {
      var row = el('div', 'kv-row');
      row.appendChild(el('span', 'k', r[0]));
      row.appendChild(el('span', 'v ' + r[2], r[1] || '-'));
      host.appendChild(row);
    });

    var actions = $('settings-actions');
    clear(actions);
    addActionButton(actions, 'set_rebuildrate', '', '改重建速率');
    addActionButton(actions, 'set_ccrate', '', '改校验速率');
    addActionButton(actions, 'set_autorebuild', '', '切换自动重建');
    addActionButton(actions, 'set_maintainpdfailhistory', '', '切换失效历史');
    addActionButton(actions, 'set_activityforlocate', '', '切换活动灯定位');
  }

  // "ABSENT" from storcli means the card has no buzzer at all, which is not a
  // fault -- say so rather than showing a bare uppercase token.
  function alarmText(value) {
    var text = String(value === null || value === undefined ? '' : value).trim();
    if (!text || text === '-') return '-';
    if (/^absent$/i.test(text)) return 'ABSENT（控制器未提供蜂鸣器）';
    return text;
  }

  function alarmClass(value) {
    var text = String(value === null || value === undefined ? '' : value).trim();
    if (/^absent$/i.test(text)) return '';
    return text.toLowerCase().indexOf('disable') >= 0 ? 'warn' : 'ok';
  }

  function logLine(log) {
    if (!log || typeof log.bytes !== 'number') return '-';
    var mb = (log.bytes / 1048576).toFixed(1);
    return mb + ' MB / 上限 ' + (log.limit_mb || '-') + ' MB · 已轮转 '
      + (log.rotations || 0) + ' 次';
  }

  function renderMisc(cv, schedule, controller, storcliLog) {
    var host = $('misc');
    clear(host);
    var isBbu = String(cv.kind || '').toLowerCase() === 'bbu';
    var known = meaningful(cv.state) || meaningful(cv.model);
    var stateOk = /^optimal$/i.test(String(cv.state || '').trim());
    var nvcacheOk = !cv.nvcache_state || String(cv.nvcache_state).toUpperCase() === 'OK';
    var replaceNeeded = /^(yes|y|true)$/i.test(String(cv.replacement_required || ''));

    var title = $('misc-title');
    if (title) {
      title.textContent = isBbu ? '电池（BBU）与计划任务'
        : (known ? 'CacheVault 与计划任务' : '电源备份与计划任务');
    }

    // Fourth element marks "only draw when the value is actually available",
    // so a controller without any power-backup object does not produce a wall
    // of dashes.  A battery card reports a different set of fields than a
    // supercapacitor card, hence the two lists.
    var rows;
    if (!known) {
      rows = [
        ['电源备份', '控制器未上报 CacheVault / 电池', 'warn'],
        ['说明', '此控制器可能未安装备电模块，或固件不通过 storcli 暴露该对象', '']
      ];
    } else if (isBbu) {
      rows = [
        ['电池类型', cv.model, ''],
        ['电池状态', cv.state, stateOk ? 'ok' : 'warn'],
        ['电池温度', cv.temp, ''],
        ['电压', cv.voltage, '', 1],
        ['电流', cv.current, '', 1],
        ['保持时间 (Retention Time)', cv.retention, '', 1],
        ['设计容量', cv.design_capacity, '', 1],
        ['模块固件', cv.firmware, '', 1],
        ['厂商', cv.manufacturer, '', 1],
        ['序列号', cv.serial, '', 1],
        ['生产日期', cv.mfg_date, ''],
        ['需要更换', cv.replacement_required, replaceNeeded ? 'bad' : 'ok', 1],
        ['学习周期', cv.learn_period, '', 1],
        ['下次学习', cv.next_learn, '', 1],
        ['学习模式', cv.learn_mode, '', 1]
      ];
    } else {
      rows = [
        ['CacheVault 型号', cv.model, ''],
        ['CacheVault 状态', cv.state, stateOk ? 'ok' : 'warn'],
        ['模块温度', cv.temp, ''],
        ['包能量 (Pack Energy)', cv.pack_energy, '', 1],
        ['设计容量', cv.design_capacity, '', 1],
        ['电容值', cv.capacitance, '', 1],
        ['NVCache 状态', cv.nvcache_state, nvcacheOk ? 'ok' : 'bad', 1],
        ['需要更换', cv.replacement_required, replaceNeeded ? 'bad' : 'ok', 1],
        ['模块固件', cv.firmware, '', 1],
        ['厂商', cv.manufacturer, '', 1],
        ['模块序列号', cv.serial, '', 1],
        ['生产日期', cv.mfg_date, ''],
        ['电芯生产日期', cv.cell_mfg_date, '', 1],
        ['Flash 容量', cv.flash_size, '', 1],
        ['PCB 组件号', cv.pcb_assy, '', 1],
        ['电池包组件号', cv.batpack_assy, '', 1],
        ['学习周期', cv.learn_period, '', 1],
        ['下次学习', cv.next_learn, '', 1],
        ['学习模式', cv.learn_mode, '', 1]
      ];
    }

    rows = rows.concat([
      ['一致性校验周期', schedule.cc_interval, ''],
      ['下次一致性校验', schedule.cc_next, ''],
      ['巡检周期', schedule.pr_interval, ''],
      ['下次巡检', schedule.pr_next, ''],
      ['下次电池学习', schedule.battery_next, ''],
      ['告警蜂鸣器', alarmText(controller.alarm), alarmClass(controller.alarm)],
      ['storcli 日志', logLine(storcliLog), '']
    ]);
    rows.forEach(function (r) {
      if (r[3] && !meaningful(r[1])) return;
      var row = el('div', 'kv-row');
      row.appendChild(el('span', 'k', r[0]));
      var value = String(r[1] === null || r[1] === undefined ? '-' : r[1]);
      var cls = r[2] || '';
      if (value.length > 30) cls += ' wide';
      row.appendChild(el('span', 'v ' + cls, value));
      host.appendChild(row);
    });

    var actions = $('misc-actions');
    clear(actions);
    addActionButton(actions, 'alarm_silence', '', '静音蜂鸣器');
    addActionButton(actions, 'alarm_off', '', '关闭蜂鸣器');
    addActionButton(actions, 'alarm_on', '', '开启蜂鸣器');
    addActionButton(actions, 'foreign_import', '', '导入外来配置', 'danger');
  }

  var SEV_BAD = /(fail|error|degraded|offline|shield|inconsistent|not syncing|removed|missing|unexpected sense)/i;
  var SEV_WARN = /(rebuild|foreign|inserted|reset|timeout|predictive|smart|power on)/i;

  function renderEvents(events) {
    var host = $('events');
    clear(host);
    var head = el('thead');
    var hr = el('tr');
    ['时间', '事件', '级别'].forEach(function (t) { hr.appendChild(el('th', '', t)); });
    head.appendChild(hr);
    host.appendChild(head);

    var body = el('tbody');
    if (!events || !events.length) {
      var tr = el('tr');
      var td = el('td', 'dim', '没有事件记录');
      td.colSpan = 3;
      tr.appendChild(td);
      body.appendChild(tr);
    } else {
      events.forEach(function (ev) {
        var tr = el('tr');
        tr.appendChild(el('td', 'mono', ev.time || '-'));
        tr.appendChild(el('td', '', ev.desc || '-'));
        var sev = SEV_BAD.test(ev.desc || '') ? 'bad' : (SEV_WARN.test(ev.desc || '') ? 'warn' : 'ok');
        var cell = el('td');
        cell.appendChild(el('span', 'sev ' + sev, sev === 'bad' ? '错误' : (sev === 'warn' ? '注意' : '信息')));
        tr.appendChild(cell);
        body.appendChild(tr);
      });
    }
    host.appendChild(body);
    $('events-count').textContent = events && events.length ? ('（最新 ' + events.length + ' 条）') : '';
  }

  function renderAudit(records) {
    var host = $('audit');
    clear(host);

    var head = el('thead');
    var hr = el('tr');
    ['时间', '用户', '操作', '目标', '结果'].forEach(function (t) { hr.appendChild(el('th', '', t)); });
    head.appendChild(hr);
    host.appendChild(head);

    var body = el('tbody');
    if (!records || !records.length) {
      var tr = el('tr');
      var td = el('td', 'dim', '还没有操作记录');
      td.colSpan = 5;
      tr.appendChild(td);
      body.appendChild(tr);
    } else {
      records.forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', 'mono', r.time || '-'));
        tr.appendChild(el('td', '', r.user || '-'));
        tr.appendChild(el('td', 'mono', r.action || '-'));
        tr.appendChild(el('td', 'mono dim', r.target || '-'));
        var cell = el('td');
        cell.appendChild(el('span', 'sev ' + (r.result === 'ok' ? 'ok' : 'bad'),
          r.result === 'ok' ? '成功' : '失败'));
        tr.appendChild(cell);
        body.appendChild(tr);
      });
    }
    host.appendChild(body);
  }

  // --------------------------------------------------------------- actions

  // What the controller can physically do, as reported by /api/summary.  A card
  // with no buzzer (Alarm ABSENT) or direct-attached drives with no SES channel
  // cannot satisfy some buttons, and letting the operator fill in a dialog only
  // to be refused -- or hit a storcli error -- teaches the wrong thing.
  var CAPS = null;
  var ACTION_NEEDS = {
    alarm_on: 'alarm', alarm_off: 'alarm', alarm_silence: 'alarm',
    locate_start: 'locate', locate_stop: 'locate'
  };
  var CAPS_REASON = {
    alarm: '该控制器没有蜂鸣器（storcli 报 Alarm ABSENT），此项无可操作对象',
    locate: '直连拓扑没有 SES 机框，控制器无法驱动定位灯'
  };

  function addActionButton(host, name, target, labelOverride, forceClass) {
    var meta = ACTIONS[name];
    if (!meta) return;
    var btn = el('button', 'btn tiny ' + (forceClass || (meta.risk === 'danger' ? 'danger' : 'ghost')),
      labelOverride || meta.label);
    var need = ACTION_NEEDS[name];
    if (CAPS && need && !CAPS[need]) {
      btn.disabled = true;
      btn.classList.add('blocked');
      btn.title = CAPS_REASON[need];
    } else if (BLOCKED_NOW && meta.blocked_when_busy) {
      // Refuse it in the UI too, and say why, instead of letting the operator
      // fill in a password only to be rejected by the server.
      btn.disabled = true;
      btn.classList.add('blocked');
      btn.title = '控制器正在执行后台任务（重建 / 回拷 / 校验），按厂商规范此时不接受该操作';
    } else {
      btn.addEventListener('click', function () { openAction(name, target, labelOverride || meta.label); });
    }
    host.appendChild(btn);
  }

  var pending = null;

  function openAction(name, target, label) {
    var meta = ACTIONS[name];
    if (!meta) return;
    pending = { name: name, target: target || '', meta: meta, label: label || meta.label };
    setModalBusy(false);

    $('modal-title').textContent = '确认操作：' + (label || meta.label);
    var lines = ['操作：' + (label || meta.label)];
    if (target) lines.push('目标：' + target);
    if (meta.risk === 'danger') {
      lines.push('');
      lines.push('⚠ 这是高危操作，可能影响数据可用性或触发长时间重建。');
    }

    // Removing a member of an already-degraded array costs the last redundancy.
    // The server refuses it without the target name typed out; mirror that here
    // so the operator sees why before submitting.
    var extraWrap = $('modal-extra');
    var extraInput = $('modal-extra-input');
    extraInput.value = '';
    var needsExtra = !!(meta.last_redundancy_risk && LAST && LAST.redundancy
      && (LAST.redundancy.critical_slots || []).indexOf(target) >= 0);
    pending.needsExtra = needsExtra;
    if (needsExtra) {
      var short = String(target || '').split('/').pop();
      lines.push('');
      lines.push('⚠ 阵列已降级：这块盘是阵列成员，移除后阵列将彻底失去冗余，数据不可恢复。');
      extraWrap.className = 'modal-confirm';
      $('modal-extra-label').textContent = '确认：输入 ' + short;
      $('modal-extra-note').textContent =
        '只有在你已确认该盘确实损坏、且已另有可用备份时才继续。';
    } else {
      extraWrap.className = 'modal-confirm hidden';
    }

    $('modal-body').textContent = lines.join('\n');

    var valueWrap = $('modal-value');
    clear(valueWrap);
    if (meta.value === 'percent') {
      var l1 = el('label', '', '速率（0-100）');
      var i1 = el('input');
      i1.type = 'number';
      i1.id = 'modal-input';
      i1.min = '0';
      i1.max = '100';
      i1.value = '30';
      valueWrap.appendChild(l1);
      valueWrap.appendChild(i1);
    } else if (meta.value === 'toggle') {
      var l2 = el('label', '', '取值');
      var s2 = document.createElement('select');
      s2.id = 'modal-input';
      s2.className = 'modal-select';
      ['on', 'off'].forEach(function (v) {
        var o = el('option', '', v);
        o.value = v;
        s2.appendChild(o);
      });
      valueWrap.appendChild(l2);
      valueWrap.appendChild(s2);
    }
    valueWrap.className = meta.value === 'none' ? 'modal-value hidden' : 'modal-value';

    var confirmWrap = $('modal-password');
    var passwordInput = $('modal-password-input');
    var targetBox = $('modal-target');
    passwordInput.value = '';

    if (meta.risk === 'danger') {
      // Show exactly what is about to be affected -- a password proves identity,
      // this proves the operator is looking at the right object.
      var drive = null;
      (LAST && LAST.drives || []).forEach(function (d) {
        if (d.slot_path === target) drive = d;
      });
      clear(targetBox);
      if (drive) {
        targetBox.appendChild(el('div', 'mt-title', drive.short + ' · ' + (drive.model || '-')));
        targetBox.appendChild(el('div', 'mt-line', 'SN ' + (drive.sn || '-') + ' · ' + (drive.position || '-')));
        targetBox.appendChild(el('div', 'mt-line',
          '状态 ' + (drive.State || '-') + ' · 温度 ' + (drive.temperature || '-') + '°C · 误码/秒 ' + rate(drive.phy && drive.phy.inval_per_s)));
      } else {
        targetBox.appendChild(el('div', 'mt-title', target || '控制器'));
      }
      targetBox.className = 'modal-target';

      var who = (LAST && LAST.user) ? LAST.user : '当前用户';
      $('modal-password-label').textContent = '账户密码（' + who + '）';
      confirmWrap.className = 'modal-confirm';
    } else {
      targetBox.className = 'modal-target hidden';
      confirmWrap.className = 'modal-confirm hidden';
    }

    $('modal').className = 'modal';
    setTimeout(function () {
      var first = $('modal-input') || (meta.risk === 'danger' ? passwordInput : null);
      if (first && first.focus) first.focus();
    }, 40);
  }

  function closeModal() {
    $('modal').className = 'modal hidden';
    $('modal-password-input').value = '';
    $('modal-extra-input').value = '';
    $('modal-extra').className = 'modal-confirm hidden';
    pending = null;
  }

  var MODAL_HINT = '命令由控制器执行，通常需要几秒；较慢的操作可以在关闭本窗口后继续在顶部跟踪进度。';

  // While a command is in flight the dialogue switches to a progress state and
  // stops being a blocker -- the job keeps running server-side and stays
  // visible in the task bar, so the operator is never left guessing.
  function setModalBusy(on, text) {
    $('modal-progress').className = on ? 'modal-progress' : 'modal-progress hidden';
    $('modal-ok').disabled = !!on;
    // Only `danger` actions get the red button -- a write-level action such as
    // silencing the buzzer should not be dressed up as destructive.
    var danger = !pending || pending.meta.risk === 'danger';
    $('modal-ok').className = 'btn ' + (danger ? 'danger' : '') + (on ? ' loading' : '');
    $('modal-cancel').textContent = on ? '关闭' : '取消';
    $('modal-hint').textContent = on
      ? '命令已下发，正在等待控制器响应。可以关闭本窗口，进度会持续显示在页面顶部。'
      : MODAL_HINT;
    if (on) $('modal-progress-text').textContent = text || '正在执行…';
  }

  async function submitAction() {
    if (!pending) return;
    var meta = pending.meta;
    var value = null;
    var input = $('modal-input');
    if (meta.value !== 'none' && input) value = input.value;

    var password = null;
    if (meta.risk === 'danger') {
      password = $('modal-password-input').value;
      if (!password) {
        toast('该操作需要输入账户密码', 'bad');
        $('modal-password-input').focus();
        return;
      }
    }

    var confirmValue = null;
    if (pending.needsExtra) {
      confirmValue = $('modal-extra-input').value;
      if (!confirmValue) {
        toast('请输入确认内容', 'bad');
        $('modal-extra-input').focus();
        return;
      }
    }

    var label = pending.label || meta.label || pending.name;
    // closeModal() below clears `pending` (it is the single source of truth for
    // the open dialogue), so everything read after the request must come from
    // locals.  Reading pending.target after closeModal threw
    // "Cannot read properties of null (reading 'target')" and the catch below
    // reported a perfectly successful command -- locate LED, buzzer silence and
    // every other write action -- as "执行失败".
    var target = pending.target;
    setModalBusy(true, meta.risk === 'danger' ? '正在验证账户密码…' : '正在下发命令…');
    try {
      var res = await api('action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: pending.name,
          target: target,
          value: value,
          password: password,
          confirm: confirmValue
        })
      });
      setModalBusy(false);
      closeModal();
      var job = res && res.job;
      if (job) {
        // Fire-and-track: the command itself runs in the background.
        job.summary = label + (target ? ' · ' + shortTarget(target) : '');
        applyJob(job);
      } else {
        toast('✓ ' + label + ' 执行成功', 'ok');
        refresh();
        loadAudit();
      }
    } catch (e) {
      setModalBusy(false);
      toast('✗ ' + label + ' 执行失败：' + e.message, 'bad');
      loadAudit();
    }
  }

  function shortTarget(target) {
    var m = String(target || '').match(/\/s(\d+)$/);
    if (m) return 's' + m[1];
    return String(target || '');
  }

  // ------------------------------------------------------------------ load

  function setSubtitle(data) {
    var sub = $('subtitle');
    var c = data.controller || {};
    var who = data.user ? (' · ' + data.user + (data.isadmin ? ' (管理员)' : ' (只读)')) : '';
    var interval = data.interval ? (' · 采样间隔 ' + data.interval + 's') : '';
    sub.textContent = (c.product || 'RAID 控制器') + ' · ' + (c.fw_version || '') + ' · 更新于 ' + (data.timestamp || '') + interval + who;
  }

  function setHealth(data) {
    var dot = $('health-dot');
    dot.className = 'pulse ' + (data.overall || 'ok');
    dot.title = data.overall === 'bad' ? '存在需处理的异常'
      : (data.overall === 'warn' ? '有项目需要留意' : '全部正常');
  }

  async function refresh() {
    if (REFRESHING) { REFRESH_AGAIN = true; return; }
    REFRESHING = true;
    var btn = $('btn-refresh');
    var first = !LAST;
    if (first) $('boot').className = 'boot';
    btn.disabled = true;
    btn.classList.add('loading');
    $('sync-chip').className = 'sync';
    try {
      var data = await api('summary');
      if (!data || !data.ok) throw new Error((data && data.error) || '返回数据异常');
      LAST = data;
      BLOCKED_NOW = !!data.background_active;
      CAPS = data.capabilities || null;
      setHealth(data);
      setSubtitle(data);
      renderOverview(data);
      renderAdvisories(data);
      applyPollHint(data.poll_hint);
      renderControllerInfo(data);
      renderVds(data.vds);
      renderDrives(data.drives, data.thresholds);
      renderSettings(data.settings || {});
      renderMisc(data.cachevault || {}, data.schedule || {}, data.controller || {},
        data.storcli_log || null);
      loadEvents();
    } catch (e) {
      $('health-dot').className = 'pulse bad';
      banner('bad', '无法读取控制器状态', '错误：' + escapeHTML(e.message)
        + '<br>请确认 storcli 已安装，或在本应用设置中指定其路径。');
      toast('刷新失败：' + e.message, 'bad');
    } finally {
      $('boot').className = 'boot hidden';
      $('sync-chip').className = 'sync hidden';
      btn.classList.remove('loading');
      btn.disabled = false;
      REFRESHING = false;
      if (REFRESH_AGAIN) { REFRESH_AGAIN = false; setTimeout(refresh, 0); }
    }
  }

  function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  async function loadEvents() {
    try {
      var data = await api('events?limit=60');
      renderEvents(data.events || []);
    } catch (e) {
      renderEvents([]);
    }
  }

  async function loadAudit() {
    try {
      var data = await api('audit?limit=60');
      renderAudit(data.audit || []);
    } catch (e) {
      renderAudit([]);
    }
  }

  async function loadActions() {
    try {
      var data = await api('actions');
      (data.actions || []).forEach(function (a) { ACTIONS[a.name] = a; });
    } catch (e) {
      /* buttons simply will not render */
    }
  }

  async function loadConfig() {
    try {
      var data = await api('config');
      var cfg = data.config || {};
      if (cfg.poll_interval) POLL = Number(cfg.poll_interval) * 1000;
      return cfg;
    } catch (e) {
      return {};
    }
  }

  // ------------------------------------------------------------------ boot

  function schedule() {
    if (TIMER) { clearInterval(TIMER); TIMER = null; }
    if ($('auto').checked) {
      // Never hand setInterval a NaN or a sub-second value: a junk
      // poll_interval would otherwise turn into a request flood.
      var interval = Number(POLL);
      if (!isFinite(interval) || interval < 3000) interval = 3000;
      TIMER = setInterval(refresh, interval);
    }
  }

  async function boot() {
    $('btn-refresh').addEventListener('click', function () { refresh(); });

    $('auto').addEventListener('change', function () {
      try { localStorage.setItem('rw.auto', $('auto').checked ? '1' : '0'); } catch (e) {}
      schedule();
    });

    $('btn-audit').addEventListener('click', function () {
      var panel = $('audit-panel');
      var hidden = panel.classList.contains('hidden');
      panel.classList.toggle('hidden', !hidden);
      if (hidden) loadAudit();
    });

    $('btn-settings').addEventListener('click', async function () {
      var cfg = await loadConfig();
      var answer = window.prompt(
        '刷新间隔（秒，3-300）\n'
        + '当前 storcli：' + (cfg.storcli || '(自动探测)')
        + '（如需更换请编辑 etc/config.json，且必须是 root 拥有、他人不可写的可执行文件）\n'
        + '注：数组有后台任务运行时，服务端会自动放慢刷新以让出控制器。',
        String(cfg.poll_interval || 10));
      if (answer === null) return;
      var secs = parseInt(answer, 10);
      if (!isFinite(secs) || secs < 3 || secs > 300) {
        toast('请输入 3-300 之间的整数', 'bad');
        return;
      }

      // storcli appends to its own log on every call; the cap bounds it and
      // keeps one rotated file behind (storcli.log.1).
      var logAnswer = window.prompt(
        'storcli 日志上限（MB，1-4096）\n'
        + '超过后轮转为 storcli.log.1，只保留 1 份，占用上限约为两倍。',
        String(cfg.storcli_log_max_mb || 128));
      var payload = { poll_interval: secs };
      if (logAnswer !== null && String(logAnswer).trim() !== '') {
        var logMb = parseInt(logAnswer, 10);
        if (!isFinite(logMb) || logMb < 1 || logMb > 4096) {
          toast('日志上限需为 1-4096 之间的整数', 'bad');
          return;
        }
        payload.storcli_log_max_mb = logMb;
      }

      try {
        await api('config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ config: payload })
        });
        POLL = secs * 1000;
        schedule();
        toast('已保存：刷新间隔 ' + secs + ' 秒'
          + (payload.storcli_log_max_mb ? '，日志上限 ' + payload.storcli_log_max_mb + ' MB' : ''),
          'ok');
      } catch (e) {
        toast('保存失败：' + e.message, 'bad');
      }
    });

    $('modal-cancel').addEventListener('click', closeModal);
    $('modal-ok').addEventListener('click', submitAction);
    $('modal').addEventListener('click', function (ev) {
      if (ev.target === $('modal')) closeModal();
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && !$('modal').classList.contains('hidden')) closeModal();
      if (ev.key === 'Enter' && !$('modal').classList.contains('hidden')) {
        if (ev.target && (ev.target.id === 'modal-input' || ev.target.id === 'modal-password-input')) {
          submitAction();
        }
      }
    });

    window.addEventListener('resize', function () {
      if (LAST) renderDrives(LAST.drives || [], LAST.thresholds || {});
    });

    try {
      var saved = localStorage.getItem('rw.auto');
      if (saved === '0') $('auto').checked = false;
    } catch (e) {}

    renderTasks();
    await loadActions();
    await loadConfig();
    await loadJobs();      // pick up a command that was still running
    await refresh();
    schedule();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
