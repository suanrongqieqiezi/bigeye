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
footer .input-row textarea{min-height:26px!important;max-height:var(--composer-input-max,240px)!important;overflow-y:auto;overscroll-behavior:contain;padding:2px 5px!important;background:transparent!important;box-shadow:none!important;border:0!important}
footer .input-row .attach-btn{height:30px!important;width:30px!important}
footer .input-row .send{height:30px!important;width:30px!important;min-height:30px!important}
footer .task-model-btn,footer .task-outside-btn{padding:4px 8px!important;font-size:12px!important}
footer .input-row textarea:focus{box-shadow:none!important}

/* ===== 回到底部按钮：中间正下方 · 与 ☰/右岛/输入框同一套 --mat-* 材质（§88 回到底部浮键）=====
   这一层带 !important，是真正压过 index.html 基础规则的那份 —— 改材质/位置必须改这里。
   🔴 transform 必须带 translateX(-50%)：基础态在 index.html 里是 translate(-50%,8px)，
      这里 .show 少写一半，按钮就在显示瞬间横移半个身位。 */
html body #btnJump{left:50%!important;right:auto!important;z-index:40!important;
  bottom:calc(var(--composer-h,68px) + 14px)!important;
  background:var(--mat-fill)!important;
  -webkit-backdrop-filter:var(--mat-blur)!important;backdrop-filter:var(--mat-blur)!important;
  border:1px solid var(--border2)!important;
  box-shadow:inset 0 1px 0 var(--mat-top),var(--mat-lift)!important;
  color:var(--text)!important}
/* Touch devices must not retain hover emphasis after a tap —— 所以 hover 只给真有指针的设备，
   且只叠一层提亮（写 background 简写会把 --mat-fill 一起换掉，§84 坑 2）。 */
@media(hover:hover) and (pointer:fine){html body #btnJump:hover{
  color:var(--text)!important;
  background-color:var(--mat-fill)!important;
  background-image:linear-gradient(var(--hover),var(--hover))!important}}
html body #btnJump:active{color:var(--text)!important;transform:translate(-50%,0) scale(.94)!important}
/* Only attachment cards get an opaque reading surface; the floating composer stays unchanged. */
html body #attachPreview .att-card{flex:0 0 auto;max-width:220px;background:var(--surface2)!important;color:var(--text)!important;opacity:1!important;backdrop-filter:none!important;-webkit-backdrop-filter:none!important;box-shadow:0 3px 12px rgba(0,0,0,.24)}
html body #btnJump.show{opacity:1;pointer-events:auto;transform:translate(-50%,0)}

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

/* ===== 长文本自动增长：只在换行时改变一次高度，最高约半屏 =====
   不再提供手动拉高：拖拽会连续改变悬浮层、玻璃绘制区与滚动位置，手机端代价过高。
   达到 --composer-input-max 后仅 textarea 内部滚动，不再推挤聊天内容。 */
`;
  document.head.appendChild(css);

  /* ===== 平滑跟底：把「内容突然变高」的单帧跳变摊到多帧 =====
     两种节奏来源不同，不能混用：
     · 'pin'  —— 每帧 scrollTop 跟着 scrollHeight 走。高度变化本身由 CSS transition 驱动
                 （.proc-box 的 max-height，现 .42s），节奏交给 CSS，我们只负责不让视口掉队。
                 用「增量」而不是「直接等于 max」：启动那一帧可能已经晚了 16~33ms，
                 直接赋值会把这段增量吃掉，视觉上就是开头顿一下。
     · 'ease' —— 自己插值。高度是**瞬时**变的（footer 长高：textarea 的行数必须立刻
                 反映用户打的字，不能给它加过渡），没有 CSS 曲线可搭，只能自造节奏。
     🔴 只在调用方确认「贴底」时启动。用户正在看历史时绝不能动他的视口（§118/§119）。
     🔴 手指/滚轮一动就撤：跟底是「帮用户钉住」，不是「抢用户的滚动」。 */
  var followRaf = 0, followMode = '', followT0 = 0, followDur = 0, followFrom = 0, followBase = 0;
  var noMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function stopFollow() {
    if (followRaf) { cancelAnimationFrame(followRaf); followRaf = 0; }
    followMode = '';
  }
  function followMax() { return chat ? (chat.scrollHeight - chat.clientHeight) : 0; }
  function followTick(ts) {
    followRaf = 0;
    if (!followMode || !chat) return;
    if (!followT0) followT0 = ts;
    var elapsed = ts - followT0;
    var max = followMax();
    if (followMode === 'pin') {
      chat.scrollTop = followFrom + (max - followBase);
    } else {
      /* 正弦缓出：起点速度是平均的 π/2 ≈ 1.57 倍。三次缓出是 3 倍 —— 61px 的位移第一帧
         就走 15px，看上去仍是「顿一下」；正弦把单帧峰值压到约 7px，总时长不变。 */
      var k = Math.min(1, elapsed / followDur);
      chat.scrollTop = followFrom + (max - followFrom) * Math.sin(k * Math.PI / 2);
    }
    if (elapsed < followDur) followRaf = requestAnimationFrame(followTick);
    else followMode = '';
  }
  function startFollow(mode, dur) {
    if (!chat) return;
    /* 关掉动效的用户也必须跟底 —— 直接瞬时落位，别把他卡在「底部留白长好了但视口没动」的
       半贴底状态里（那才是真的难用）。2026-10-28 */
    if (noMotion) { chat.scrollTop = followMax(); return; }
    /* pin 不能重置基准：它靠 followBase 算增量，一重置就把已经跟过的位移重复计入。
       必须续用同一个基准，只把时长拉到足够。 */
    if (followMode === 'pin' && followRaf) { followDur = Math.max(followDur, dur); return; }
    /* ease 反过来 —— 必须从当前位置重新起算：连续换行时如果沿用旧起点，k 已接近 1，
       新长出来的那段位移会被塞进曲线尾部（斜率≈0）里，等于瞬间补到位。 */
    followMode = mode;
    followDur = dur;
    followT0 = 0;
    followFrom = chat.scrollTop;
    followBase = followMax();
    if (!followRaf) followRaf = requestAnimationFrame(followTick);
  }
  function atBottomNow() {
    if (!chat) return false;
    return (chat.scrollHeight - chat.scrollTop - chat.clientHeight) < 8;
  }
  ['wheel', 'touchstart', 'pointerdown', 'keydown'].forEach(function (ev) {
    document.addEventListener(ev, function (e) {
      if (!followMode) return;
      /* 输入框里的操作不算「用户在看历史」—— 打字时思考框还得继续让位。
         🔴 这里只能问 composer.contains(target)：写成 e.target.contains(...) 恒为 true，
         等于把 stopFollow 永久短路掉，用户上滚也拉不回来跟底。2026-10-28 */
      var t = e.target;
      if (composer && t && t.nodeType === 1 && composer.contains(t)) return;
      stopFollow();
    }, { passive: true, capture: true });
  });

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
    /* 🔴 变化**之前**先判一次「是否真的贴底」，口径与 index.html 的 chatAtBottom 一致（<8px）。
       绝不能改用 chatStick：它的容差是 120px，而上滚一格正好 120px ⇒「用户已经离开底部」
       被读成「还在跟底」，于是高度一变就被拽回底部。2026-10-28 */
    var box = document.getElementById('chatBox');
    var atBottom = false;
    if (box) atBottom = (box.scrollHeight - box.scrollTop - box.clientHeight) < 8;
    var prevH = lastComposerHeight;
    lastComposerHeight = h;
    document.documentElement.style.setProperty('--composer-h', h + 'px');
    document.documentElement.style.setProperty('--composer-space', h + 'px');
    /* 聊天区那颗三点图形的「被输入框压住」边界跟着一起走（index.html 的 _orbSyncMargin）。
       这里是 footer 高度的**唯一写入口**，挂在这里最可靠 —— 试过在 index.html 侧
       用 MutationObserver 盯 --composer-h、用 ResizeObserver 盯 footer，实测都漏过：
       变量明明从 110 写成 98，边界却一直停在 104（多收缩一整颗图形）。 */
    try { if (window._orbSyncMargin) window._orbSyncMargin(); } catch (e) {}
    /* 只有真的贴底才把视口重新钉到底 —— 此时底部留白刚长高，不钉会凭空多出一段空隙。
       其余情况**一律不动 scrollTop**：padding-bottom 与本函数写进去的 --composer-space
       同源，内容末端到输入框上沿的间距恒为 16px + footer 内边距，跟 h 无关；
       在这里「补偿」= 替用户滚动 = 抢走他正在看的位置。 */
    /* 🔴 变高时必须插值，不能一步到位：footer 是悬浮层，textarea 行数一变，
       padding-bottom 与 scrollHeight 同一帧长完，直接赋 scrollTop 就是 61px 的单帧跳变
       （实测 114 帧里只有 1 帧在动，用户看到的正是「卡一下」）。
       变矮则贴着就行 —— scrollTop 会被浏览器自己 clamp，不需要动画。 */
    if (atBottom && box) {
      if (prevH && h > prevH) startFollow('ease', 240);
      else box.scrollTop = box.scrollHeight;
    }
  }
  var composer = document.querySelector('#mainPanel > footer');
  var chat = document.getElementById('chatBox');
  var composerInput = composer ? composer.querySelector('textarea') : null;
  var lastInputMax = 0;
  /* 文字自然撑高，整个输入面板约到半屏为止。96px 是按钮、间距与 footer 内边距的预留；
     上限只在输入/视口变化时计算，不再有拖拽期的逐帧布局写入。 */
  function syncInputMax() {
    var viewportH = window.visualViewport ? window.visualViewport.height : window.innerHeight;
    var max = Math.max(34, Math.floor(viewportH * 0.5) - 96);
    if (max !== lastInputMax) {
      lastInputMax = max;
      document.documentElement.style.setProperty('--composer-input-max', max + 'px');
    }
    return max;
  }
  function resizeComposerInput() {
    if (!composerInput) return;
    composerInput.style.height = 'auto';
    composerInput.style.height = Math.min(composerInput.scrollHeight, syncInputMax()) + 'px';
  }
  window.__composerInputMax = syncInputMax;
  window.__resizeComposerInput = resizeComposerInput;
  resizeComposerInput();
  var compact = false, upAccum = 0;
  /* 上一次**真正判定**时的 scrollTop。判据只在它变化时才跑：
     padding/内容变化会让 scrollHeight 变、README 里的离底距离跟着变，但 scrollTop 不动。
     那种调用是"系统自己在动"，拿它判状态 = 自己把自己按成贴底（20px 台阶的原始来源）。 */
  var lastJudgeTop = -1;
  var frame = 0, composerAnimation = null, jumpAnimation = null;
  // 不再写死 110px：由 syncComposerHeight() 按真实面板高度设置（初始值先给一个
  // 保守值，避免首帧 padding 从 0 长起来时抖一下）。
  document.documentElement.style.setProperty('--composer-space', '110px');
  function setComposerCompact(value) {
    if (!composer || compact === value) return;
    var row = composer.querySelector('.input-row');
    /* 必须用 offsetHeight（布局高度）：上一次 FLIP 的 transform 还在跑时，
       getBoundingClientRect().height 是 scaleY 后的视觉高度 ⇒ 连续切换会拿到一半的值，
       起始比例错掉，看起来就是"抽一下"。after 本来就用 offsetHeight，两者同源。 */
    var before = row ? row.offsetHeight : 0;
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
  /* ===== 两个死区（hysteresis），缺一个都还会抖 =====
     · 位移死区：向上必须**累计** 24px 才算「用户在看历史」。旧写法 `top < lastTop - 1`
       是零死区 —— 触控板 1~3px 的抖动、手机惯性回弹、图片解码后的回钳全算"上滑"，
       状态机被手指噪声驱动（实测离底 12~32 每档都抖，60ms 一轮、23 次翻转）。
     · 回程死区：贴底阈值取 4px，比「死区 24 − 一次切换压扁的 20」还小。
       推导：向上触发收缩时离底至少 24，收缩把 padding 减 20 ⇒ 收缩后离底 ≥ 4。
       贴底阈值若 ≥ 4（比如原来的 12），收缩完立刻被自己判成"贴底"又展开 —— 自激。
       4 是这套参数的**推导值**，不是拍出来的；改 UP_DEADZONE 或面板高差要回来重算。
     · 判据只在 scrollTop 真的变了时跑（见 lastJudgeTop 注释），padding 变化不进状态机。 */
  var UP_DEADZONE = 24, BOTTOM_EPS = 4;
  function updateComposerState() {
    frame = 0;
    if (!chat || !composer) return;
    var top = chat.scrollTop;
    if (top === lastJudgeTop) return;          // 位置没动 = 这次触发不是滚动引起的
    var dy = top - lastJudgeTop;               // 同上：lastJudgeTop 就是上一次判定的位置
    lastJudgeTop = top;
    if (dy > 0) upAccum = 0;                   // 往下滚 = 想看更新的内容，意图清零
    else upAccum -= dy;                        // dy<0：累计向上位移
    var d = Math.max(0, chat.scrollHeight - chat.clientHeight - top);
    if (d < BOTTOM_EPS) { setComposerCompact(false); upAccum = 0; }
    else if (upAccum >= UP_DEADZONE) { setComposerCompact(true); upAccum = 0; }
  }
  /* 思考框展开：.proc-box 的 max-height 有 .35s 过渡，但 chatBox 的 scrollTop 不会自己跟上。
     实测展开 302px 摊在 20 帧上、中段却有一帧蹦 40px —— 那是浏览器 scroll anchoring 在
     「猜」锚点，节奏不受控。这里在 click 的 **capture 阶段**先记下是否贴底（必须早于
     .proc-box 自己的 onclick，否则高度已经开始长了），下一帧接管节奏。
     🔴 pin 的时长必须 >= .proc-box 的 max-height 过渡（现 .42s），否则过渡还没走完就跟丢了。 */
  if (chat) {
    chat.addEventListener('click', function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t.closest('.proc-body') || t.closest('.proc-ask')) return; // 正文/提问卡不折叠，与 toggleProc 同规则
      var pb = t.closest('.proc-box');
      if (!pb || pb.classList.contains('open')) return;              // 只处理「即将展开」
      if (!atBottomNow()) return;
      requestAnimationFrame(function () { startFollow('pin', 470); });
    }, true);
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
  window.addEventListener('resize', function () { resizeComposerInput(); syncComposerHeight(); updateComposerState(); });
  if (window.visualViewport) window.visualViewport.addEventListener('resize', resizeComposerInput, { passive: true });
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
