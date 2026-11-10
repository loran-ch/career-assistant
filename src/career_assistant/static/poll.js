/* 统一轮询实现：页面只在内容真的变化时更新，不再反复整页刷新。
 *
 * 修掉的三类问题：
 * 1. 旧实现直接比较「服务端原文」与「浏览器序列化后的 innerHTML」。浏览器会把
 *    hidden 这类布尔属性写成 hidden=""，两者永远不相等，于是每 1.5 秒重写一次
 *    DOM：页面反复闪烁、入场动画反复重播，用户已填的内容也被冲掉。
 * 2. 旧实现的整页刷新没有条件，任务状态在终态之间来回时会连续整页刷新。
 * 3. 旧实现不区分「用户正在输入」，填表过程中刷新会打断输入。
 *
 * 现在整页刷新只在「任务状态从页面加载时的状态跃迁到终态」时发生一次：
 * 模板把渲染当时的状态写进 data-reload-from，刷新后状态已是终态，与
 * data-reload-from 相同，因此不会再次触发，也就不会来回刷新。
 */
(function () {
  'use strict';

  var INTERVAL_MS = 1500;

  /* 归一化服务端 HTML 与浏览器 innerHTML 的表示差异。 */
  function normalize(html) {
    return String(html === null || html === undefined ? '' : html)
      /* 先剔除 operation 隐藏域：它是「本次表单提交」的随机标识，
         每个请求都不同。带着它比较，轮询片段每 1.5 秒都会被判定为
         「有变化」而重写 DOM——页面看起来就在不停刷新。 */
      .replace(/<input[^>]*name="operation"[^>]*>/g, '')
      .replace(/\shidden(=(""|''))?/g, ' hidden')
      .replace(/\s+/g, ' ')
      .trim();
  }

  /* 用户正在输入控件里打字时，本轮不碰页面。 */
  function isEditing(el) {
    var active = document.activeElement;
    if (!active || !active.tagName) return false;
    if (el && el.contains(active)) return true;
    var tag = active.tagName.toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || !!active.isContentEditable;
  }

  function start(el) {
    var url = el.getAttribute('data-poll');
    if (!url) return;
    var reloadOn = (el.getAttribute('data-reload-on') || '').split(',').filter(Boolean);
    var from = el.getAttribute('data-reload-from') || '';
    /* 容器里被改过就停止自动更新：未保存的输入不能被自动刷新冲掉，
       页面上的「刷新实际状态」链接仍可手动更新。 */
    var dirty = false;
    el.addEventListener('input', function () { dirty = true; });

    async function refresh() {
      if (dirty || isEditing(el)) return;
      var text;
      try {
        var response = await fetch(url, {headers: {'X-Requested-With': 'poll'}});
        if (!response.ok) return;
        text = await response.text();
      } catch (e) {
        return;                      /* 网络抖动：下一轮继续 */
      }
      if (normalize(text) === normalize(el.innerHTML)) return;   /* 没变化就别动 DOM */
      el.innerHTML = text;
      document.dispatchEvent(new CustomEvent('career:motion-refresh', {detail: {element: el}}));
      if (!reloadOn.length || !from) return;
      var statusEl = el.querySelector('[data-task-status]');
      if (!statusEl) return;
      var status = statusEl.getAttribute('data-task-status') || '';
      if (reloadOn.indexOf(status) < 0) return;    /* 还没到需要整页刷新的状态 */
      if (status === from) return;                 /* 与页面加载时相同：不是跃迁，不刷新 */
      window.location.reload();
    }

    refresh();
    window.setInterval(refresh, INTERVAL_MS);
  }

  function boot() {
    var nodes = document.querySelectorAll('[data-poll]');
    for (var i = 0; i < nodes.length; i++) start(nodes[i]);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
