(function () {
  'use strict';
  var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  if (isIOS) document.documentElement.classList.add('composer-ios');
  // 将待发送附件收进发送卡片，避免预览和提示漂浮在聊天正文上。
  function nestPendingAttachments() {
    var row = document.querySelector('#mainPanel > footer .input-row');
    var preview = document.getElementById('attachPreview');
    if (row && preview && preview.parentNode !== row) row.insertBefore(preview, row.firstChild);
  }
  nestPendingAttachments();
  new MutationObserver(nestPendingAttachments).observe(document.body, {childList:true, subtree:true});

  var css = document.createElement('style');
  css.textContent = `
/* ===== 悬浮输入区：无底座、无黑色方块，只有玻璃边 ===== */
#mainPanel > footer{background:transparent!important;border:0!important;box-shadow:none!important;position:absolute!important;left:0;right:0;bottom:0;z-index:20;padding:6px 12px calc(8px + env(safe-area-inset-bottom))!important;pointer-events:none}
#mainPanel > footer .input-row{pointer-events:auto}
/* 引用条（#quoteBar）的排版规则在 public/index.html 的内联 <style> 里 —— 本文件靠
   <script src="/composer-floating.js?v=…"> 引入，版本号不 bump 时手机 WebView 会用旧缓存，
   引用条就丢约束被排进弹性列（压住发送按钮）。别再往这里加 #quoteBar 的 grid 规则。 */
#mainPanel > footer .input-row > #attachPreview{pointer-events:auto;grid-column:1/-1;grid-row:1}
footer .input-row:has(#attachPreview[style*="display: flex"]) textarea{grid-row:2!important}
footer .input-row:has(#attachPreview[style*="display: flex"]) .attach-btn,footer .input-row:has(#attachPreview[style*="display: flex"]) .send,footer .input-row:has(#attachPreview[style*="display: flex"]) .task-model-btn,footer .input-row:has(#attachPreview[style*="display: flex"]) .task-outside-btn{grid-row:3!important}
#mainPanel > main{padding-bottom:calc(var(--composer-space,110px) + 16px)!important;scroll-padding-bottom:calc(var(--composer-space,110px) + 16px)}
footer::before,footer::after{display:none!important}
footer #attachPreview{background:transparent!important;border-top:0!important;padding:2px 2px 6px!important;width:100%;flex-wrap:nowrap!important;overflow-x:auto;overscroll-behavior-x:contain;scrollbar-width:thin;}
/* 玻璃边保留（1px 描边 + 内侧微亮），但不再填表面色 —— 这就是之前那个黑色方块的来源 */
footer .input-row{border:1px solid var(--border2)!important;border-radius:22px!important;background:var(--surface2)!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.09),0 6px 22px rgba(0,0,0,.16)!important;padding:7px 10px!important;gap:4px!important}
footer .input-row textarea{min-height:26px!important;max-height:120px!important;padding:2px 5px!important;background:transparent!important;box-shadow:none!important;border:0!important}
footer .input-row .attach-btn{height:30px!important;width:30px!important}
footer .input-row .send{height:30px!important;width:30px!important;min-height:30px!important}
footer .task-model-btn,footer .task-outside-btn{padding:4px 8px!important;font-size:12px!important}
footer .input-row textarea:focus{box-shadow:none!important}

/* ===== 回到底部按钮：抬到悬浮输入框之上，不再被压住 ===== */
html body #btnJump{z-index:40!important;bottom:calc(var(--composer-h,68px) + 26px)!important}
/* Touch devices must not retain hover emphasis after a tap. */
html body #btnJump,html body #btnJump:hover{background:var(--surface3)!important;color:var(--tdim)!important;border-color:var(--border2)!important;box-shadow:0 4px 14px rgba(0,0,0,.3)!important}
@media(hover:hover) and (pointer:fine){html body #btnJump:hover{color:var(--accent)!important;border-color:var(--accent)!important}}
html body #btnJump:active{color:var(--accent)!important;border-color:var(--accent)!important}
/* Only attachment cards get an opaque reading surface; the floating composer stays unchanged. */
html body #attachPreview .att-card{flex:0 0 auto;max-width:220px;background:var(--surface2)!important;color:var(--text)!important;opacity:1!important;backdrop-filter:none!important;-webkit-backdrop-filter:none!important;box-shadow:0 3px 12px rgba(0,0,0,.24)}
html body #btnJump.show{opacity:1;pointer-events:auto;transform:translateY(0)}

/* ===== 模型面板：下滑把手 + 跟手位移 ===== */
.task-model-sheet{position:fixed;inset:0;z-index:2000;background:rgba(0,0,0,.5);display:flex;align-items:flex-end;justify-content:center;transition:background .2s ease;touch-action:none}
.task-model-sheet[data-closing]{pointer-events:none}
.task-model-sheet>div{border:0!important;border-radius:22px 22px 0 0!important;will-change:transform;transition:transform .22s cubic-bezier(.2,.8,.2,1);max-height:80dvh;overflow-y:auto;overscroll-behavior:contain;padding-bottom:env(safe-area-inset-bottom)}
.task-sheet-grip{display:flex;align-items:center;justify-content:center;width:100%;height:30px;border:0;background:transparent;cursor:grab;touch-action:none;position:sticky;top:0;z-index:2;padding:0}
.task-sheet-grip::after{content:'';width:40px;height:4px;border-radius:8px;background:var(--tdim);opacity:.55}
.task-sheet-grip:active{cursor:grabbing}
.task-sheet-grip:focus{outline:none}
footer .input-row{transform-origin:center bottom;transition:none}

footer .input-row textarea,footer .input-row .attach-btn,footer .input-row .send,footer .task-model-btn,footer .task-outside-btn{transition:none!important}
footer:not(.composer-compact) .input-row{padding:10px 12px!important;gap:6px!important}
footer:not(.composer-compact) .input-row textarea{min-height:34px!important;padding:4px 5px!important}
footer:not(.composer-compact) .input-row .attach-btn,footer:not(.composer-compact) .input-row .send{width:34px!important;height:34px!important;min-height:34px!important}
footer:not(.composer-compact) .task-model-btn,footer:not(.composer-compact) .task-outside-btn{padding:6px 8px!important}
@media(prefers-reduced-motion:reduce){footer .input-row,footer .input-row textarea,footer .input-row .attach-btn,footer .input-row .send,footer .task-model-btn,footer .task-outside-btn{transition:none!important}.task-model-sheet,.task-model-sheet>div{transition:none}}
`;
  document.head.appendChild(css);

  /* 把面板高度写进 CSS 变量，供 #btnJump 让位；同时让正文的底部留白跟着长高。
     为什么必须跟着长：footer 是 position:absolute 悬浮的，正文的 padding-bottom
     如果还停在写死的 110px，输入框长高后就把底部内容压在底下，而且 scrollHeight
     也不再增长 —— 用户原话“对话内容还已经拉到尽头了不能再拉”。2026-10-23 */
  var lastComposerHeight = 0;
  function syncComposerHeight() {
    var f = document.querySelector('#mainPanel > footer') || document.querySelector('footer');
    if (!f) return;
    var h = Math.round(f.getBoundingClientRect().height) || 68;
    if (h === lastComposerHeight) return;
    var prev = lastComposerHeight;
    lastComposerHeight = h;
    document.documentElement.style.setProperty('--composer-h', h + 'px');
    document.documentElement.style.setProperty('--composer-space', h + 'px');
    if (prev) keepViewAboveComposer(prev, h);
  }
  /* 输入框长高后重算滚动视野：贴底就重新钉到底，正在翻历史就按多占的高度补偿，
     保证“最后一行”始终在输入框上沿之上，而不是被盖住。 */
  function keepViewAboveComposer(prevH, newH) {
    var box = document.getElementById('chatBox');
    if (!box) return;
    var stick = (typeof window.chatStick === 'boolean') ? window.chatStick : true;
    if (stick) { box.scrollTop = box.scrollHeight; return; }
    var d = newH - prevH;
    if (d > 0) box.scrollTop = Math.min(box.scrollTop + d, box.scrollHeight - box.clientHeight);
  }
  var composer = document.querySelector('#mainPanel > footer');
  var chat = document.getElementById('chatBox');
  var compact = false, lastTop = chat ? chat.scrollTop : 0;
  var frame = 0, composerAnimation = null, jumpAnimation = null;
  // 不再写死 110px：由 syncComposerHeight() 按真实面板高度设置（初始值先给一个
  // 保守值，避免首帧 padding 从 0 长起来时抖一下）。
  document.documentElement.style.setProperty('--composer-space', '110px');
  function setComposerCompact(value) {
    if (!composer || compact === value) return;
    var row = composer.querySelector('.input-row');
    var before = row ? row.getBoundingClientRect().height : 0;
    var oldHeight = composer.offsetHeight;
    if (composerAnimation) composerAnimation.cancel();
    if (jumpAnimation) jumpAnimation.cancel();
    compact = value;
    composer.classList.toggle('composer-compact', value);
    syncComposerHeight();
    var after = row ? row.offsetHeight : 0;
    var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var safari = /Safari\//.test(navigator.userAgent) && !/Chrome|Chromium|CriOS|Firefox|FxiOS|Edg|OPR|OPiOS|Android/.test(navigator.userAgent);
    if (row && before && after && !reduced && safari) {
      // iOS Safari 对 WAAPI 的 transform FLIP 偶发跳过中间帧；用强制回流 + CSS transition 保证过渡。
      row.classList.add('composer-animating');
      row.style.willChange = 'transform';
      row.style.transition = 'none';
      row.style.transform = 'scaleY(' + (before / after) + ')';
      void row.offsetHeight;
      row.style.transition = 'transform 360ms cubic-bezier(.22,1,.36,1)';
      requestAnimationFrame(function () { row.style.transform = 'scaleY(1)'; });
      setTimeout(function () {
        row.style.transition = '';
        row.style.transform = '';
        row.style.willChange = '';
        row.classList.remove('composer-animating');
      }, 380);
    } else if (row && row.animate && before && after && !reduced) {
      // 其他浏览器继续使用 compositor FLIP。
      row.classList.add('composer-animating');
      row.style.willChange = 'transform';
      composerAnimation = row.animate([
        { transform: 'scaleY(' + (before / after) + ')' },
        { transform: 'scaleY(1)' }
      ], { duration: 360, easing: 'cubic-bezier(.22,1,.36,1)' });
      composerAnimation.onfinish = function () { row.style.willChange = ''; row.classList.remove('composer-animating'); composerAnimation = null; };
      var jump = document.getElementById('btnJump');
      if (jump) {
        jumpAnimation = jump.animate([
          { translate: '0 ' + (composer.offsetHeight - oldHeight) + 'px' },
          { translate: '0 0' }
        ], { duration: 360, easing: 'cubic-bezier(.22,1,.36,1)' });
      }
    }
  }
  function updateComposerState() {
    frame = 0;
    if (!chat || !composer) return;
    var top = chat.scrollTop;
    var distance = Math.max(0, chat.scrollHeight - chat.clientHeight - top);
    if (distance <= 12) setComposerCompact(false);
    else if (top < lastTop - 1 || distance > 72) setComposerCompact(true);
    lastTop = top;
  }
  if (chat && composer) {
    chat.addEventListener('scroll', function () {
      if (!frame) frame = requestAnimationFrame(updateComposerState);
    }, { passive: true });
    new MutationObserver(function () {
      if (!frame) frame = requestAnimationFrame(updateComposerState);
    }).observe(chat, { childList: true, subtree: true });
    updateComposerState();
  }
  syncComposerHeight();
  window.addEventListener('resize', function () { syncComposerHeight(); updateComposerState(); });
  if (window.ResizeObserver) {
    try { new ResizeObserver(syncComposerHeight).observe(document.querySelector('footer')); } catch (e) {}
  }
  // ResizeObserver covers content changes; no polling or animation-frame layout writes.

  /* 统一关闭：一次点掉全部面板（含残留层），杜绝"关两次" */
  function closeAllSheets() {
    var sheets = document.querySelectorAll('.task-model-sheet:not([data-closing])');
    if (!sheets.length) {
      document.querySelectorAll('.task-model-sheet').forEach(function (n) { n.remove(); });
      return;
    }
    sheets.forEach(function (cover) {
      cover.dataset.closing = '1';
      var panel = cover.firstElementChild;
      if (panel) {
        panel.style.transition = 'transform .18s ease';
        panel.style.transform = 'translateY(100%)';
      }
      cover.style.background = 'rgba(0,0,0,0)';
      setTimeout(function () { cover.remove(); }, 190);
    });
    /* 兜底：任何未标记的残留层也一并清掉 */
    setTimeout(function () {
      document.querySelectorAll('.task-model-sheet').forEach(function (n) { n.remove(); });
    }, 240);
  }
  window.__closeModelSheets = closeAllSheets;

  function bind(cover) {
    if (cover.dataset.dragReady) return;
    if (!cover.firstElementChild) return;
    cover.dataset.dragReady = '1';
    var panel = cover.firstElementChild;

    var grip = panel.querySelector('.task-sheet-grip');
    if (!grip) {
      grip = document.createElement('button');
      grip.type = 'button';
      grip.className = 'task-sheet-grip';
      grip.setAttribute('aria-label', '下滑或点击关闭模型选择');
      panel.prepend(grip);
    }

    var startY = 0, lastY = 0, lastTime = 0, distance = 0, velocity = 0,
        pointer = null, moved = false, closing = false;

    function close() {
      if (closing) return;
      closing = true;
      closeAllSheets();
    }

    grip.addEventListener('pointerdown', function (e) {
      if (e.button !== 0 || closing) return;
      e.preventDefault();
      pointer = e.pointerId;
      startY = lastY = e.clientY;
      lastTime = performance.now();
      distance = velocity = 0;
      moved = false;
      panel.style.transition = 'none';
      try { grip.setPointerCapture(pointer); } catch (err) {}
    });

    grip.addEventListener('pointermove', function (e) {
      if (e.pointerId !== pointer) return;
      e.preventDefault();
      var now = performance.now();
      velocity = (e.clientY - lastY) / Math.max(1, now - lastTime);
      lastY = e.clientY; lastTime = now;
      distance = Math.max(0, e.clientY - startY);
      if (distance > 5) moved = true;
      panel.style.transform = 'translateY(' + distance + 'px)';
      cover.style.background = 'rgba(0,0,0,' + (.5 * Math.max(0, 1 - distance / Math.max(1, panel.offsetHeight))) + ')';
    });

    function end(e) {
      if (e.pointerId !== pointer) return;
      pointer = null;
      panel.style.transition = '';
      var fast = (performance.now() - lastTime < 120) && velocity > .5 && distance > 18;
      if (e.type !== 'pointercancel' && (distance > Math.min(110, panel.offsetHeight * .25) || fast)) close();
      else { panel.style.transform = ''; cover.style.background = ''; }
    }
    grip.addEventListener('pointerup', end);
    grip.addEventListener('pointercancel', end);
    grip.addEventListener('lostpointercapture', function (e) {
      if (pointer !== null && e.pointerId === pointer) {
        pointer = null;
        panel.style.transition = '';
        panel.style.transform = '';
        cover.style.background = '';
      }
    });
    /* 轻点把手 = 关闭；拖动后松手不触发 click 行为 */
    grip.addEventListener('click', function (e) {
      e.preventDefault(); e.stopPropagation();
      if (!moved) close();
    });
    cover.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
  }

  function scan() { document.querySelectorAll('.task-model-sheet').forEach(bind); }
  new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
  scan();
})();
