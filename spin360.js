/* 360° spinner — plays our own re-hosted frames, no libraries.
 *
 * Frames live at https://quote.alldiamondeverything.com/media/<32-hex id>/000.webp, 001.webp, …
 * (capture version 5+; version 4 captures are 000.jpg …), captured and cleaned by the app.
 * Nothing here loads from anywhere else.
 *
 *   Spin360.mount(el, { id, n, top, v })  one spinner in `el`; `top` is the frame it starts on
 *   Spin360.mountAll(root)               every [data-spin-id][data-spin-n] inside `root`
 *   Spin360.html(spin, { still })        markup for a page: the top frame as a plain image (so it
 *                                        shows at once), over the stone's still image if given
 *
 * Loading is progressive and lazy: nothing loads until the stone is near the screen; then the
 * top frame, then every 8th frame (dragging works from here, within a second or two), then the
 * rest in the background. Drag / swipe to rotate (vertical swipes still scroll the page), arrow
 * keys when focused, gentle auto-rotate once every frame is in, paused while off-screen.
 * Auto-rotate pauses while the user touches it and resumes smoothly from the current frame after
 * 3 s of no interaction; the small pause/play button stops it for good until play is pressed.
 * Captures from version 6 also have a full-quality set (NNN@hi.webp): once the small set is in,
 * it loads in the background (frames nearest the current one first) and each frame is swapped in
 * as it arrives. The canvas is sized for the screen's pixel density, so it is sharp on phones and
 * retina laptops. Captures with only the small set (v4, v5) work exactly as before.
 * Frame files never change (unique names), so repeat visits come from the browser cache.
 */
(function () {
  'use strict';
  var MEDIA_BASE = 'https://quote.alldiamondeverything.com/media/';
  var ID_RE = /^[a-f0-9]{32}$/;
  var TURN_SECONDS = 10;       // auto-rotate: one full turn
  var TOP_PAUSE = 1.4;         // auto-rotate rests this long on the top frame after each turn
  var DRAG_TURN = 1.25;        // a drag across 1.25× the spinner's width = one full turn
  var PARALLEL = 8;            // frames requested at once (HTTP/2 on our domain)
  var COARSE = 8;              // phase 2: every 8th frame from the top frame
  var WEBP_SINCE = 5;          // capture version from which frames are .webp
  var HI_SINCE = 6;            // capture version from which NNN@hi.webp (full-quality frames) exist
  var HI_PARALLEL = 4;         // full-quality frames requested at once (they are bigger)
  var RESUME_MS = 3000;        // auto-rotate resumes after this long with no interaction
  var RAMP_S = 0.7;            // ...easing back up to speed over this long
  var MAX_DPR = 3;

  var CSS = '' +
    '.s360{position:relative;width:100%;height:100%;background:#000;overflow:hidden;user-select:none;-webkit-user-select:none;' +
    'touch-action:pan-y;cursor:grab;outline:none;-webkit-tap-highlight-color:transparent}' +
    '.s360.dragging{cursor:grabbing}' +
    '.s360:focus-visible{box-shadow:inset 0 0 0 1px #c9a84c}' +
    '.s360 canvas{position:absolute;inset:0;width:100%;height:100%;display:block}' +
    '.s360-host{position:relative;width:100%;height:100%;background:#000 center/contain no-repeat}' +
    '.s360-poster{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:block;transition:opacity .3s}' +
    '.s360-poster.gone{opacity:0;pointer-events:none}' +
    '.s360-load{position:absolute;right:10px;top:10px;display:flex;align-items:center;gap:6px;padding:4px 8px 4px 5px;' +
    'border-radius:99px;background:rgba(10,10,10,.55);transition:opacity .45s;pointer-events:none}' +
    '.s360-load.done{opacity:0}' +
    '.s360-ring{width:16px;height:16px;transform:rotate(-90deg)}' +
    '.s360-ring circle{fill:none;stroke-width:2}' +
    '.s360-ring .bg{stroke:rgba(255,255,255,.12)}' +
    '.s360-ring .fg{stroke:#c9a84c;stroke-linecap:round;transition:stroke-dashoffset .2s}' +
    '.s360-pct{font:500 9px/1 Inter,-apple-system,sans-serif;letter-spacing:.12em;color:rgba(255,255,255,.7);text-transform:uppercase}' +
    '.s360-hint{position:absolute;left:50%;bottom:12px;transform:translateX(-50%);display:flex;align-items:center;gap:7px;' +
    'padding:6px 11px;border-radius:99px;background:rgba(10,10,10,.62);border:1px solid rgba(201,168,76,.28);' +
    'font:500 10px/1 Inter,-apple-system,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:#d8c48a;' +
    'transition:opacity .5s;pointer-events:none;white-space:nowrap}' +
    '.s360-hint svg{width:14px;height:14px}' +
    '.s360-hint.gone{opacity:0}' +
    '.s360-btn{position:absolute;right:10px;bottom:10px;width:30px;height:30px;padding:0;display:flex;align-items:center;' +
    'justify-content:center;border-radius:50%;background:rgba(10,10,10,.55);border:1px solid rgba(255,255,255,.18);' +
    'color:rgba(255,255,255,.8);cursor:pointer;transition:background .2s,color .2s}' +
    '.s360-btn:hover{background:rgba(10,10,10,.8);color:#d8c48a}' +
    '.s360-btn:focus-visible{outline:1px solid #c9a84c}' +
    '.s360-btn svg{width:14px;height:14px;fill:currentColor}' +
    '.s360-badge{position:absolute;top:10px;left:10px;font:600 9px/1 Inter,-apple-system,sans-serif;letter-spacing:.14em;' +
    'color:rgba(255,255,255,.7);padding:4px 6px;background:rgba(10,10,10,.55);border:1px solid rgba(255,255,255,.14);' +
    'border-radius:4px;pointer-events:none}' +
    '.s360-err{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:20px;' +
    'font:400 13px/1.5 Inter,-apple-system,sans-serif;color:#6e6e6e}';

  function injectCss() {
    if (document.getElementById('s360-css')) return;
    var st = document.createElement('style');
    st.id = 's360-css';
    st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);
  }

  function pad3(i) { return ('00' + i).slice(-3); }

  // Offsets from the top frame, in load order: 0; every 8th (a rough full turn, dragging works);
  // then every 4th, every 2nd, the rest (it fills in). Returns [order, number of coarse frames].
  function loadOrder(n) {
    var out = [0], seen = new Uint8Array(n), step, i;
    seen[0] = 1;
    for (i = COARSE; i < n; i += COARSE) { seen[i] = 1; out.push(i); }
    var coarse = out.length;
    for (step = COARSE / 2; step >= 1; step = step / 2) {
      for (i = 0; i < n; i += step) { if (!seen[i]) { seen[i] = 1; out.push(i); } }
    }
    return [out, coarse];
  }

  function Spinner(el, id, n, top, v) {
    this.el = el; this.id = id; this.n = n; this.top = top;
    this.ext = Number(v) >= WEBP_SINCE ? 'webp' : 'jpg';
    this.coarseLeft = 0;
    this.turnStart = top; this.restUntil = 0;
    this.imgs = new Array(n); this.loaded = 0; this.failed = 0;
    this.hasHi = Number(v) >= HI_SINCE;
    this.hi = new Array(n); this.hiLoaded = 0; this.hiStarted = false;
    this.pos = top; this.vel = 0; this.auto = !matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.userPaused = !this.auto;     // reduced motion: starts stopped; play is the user's choice
    this.lastInteract = -1e9; this.ramp = 1;
    this.touched = false; this.visible = true; this.started = false; this.drawn = -1; this.drawnHi = false;
    el.__s360 = this;
    this.build();
  }

  Spinner.prototype.build = function () {
    var el = this.el, self = this;
    this.poster = el.querySelector('img.s360-poster');     // the top frame, already in the page
    el.innerHTML = '';
    el.classList.add('s360');
    el.tabIndex = 0;
    el.setAttribute('role', 'img');
    el.setAttribute('aria-label', '360° view of the diamond. Drag, swipe or use the arrow keys to rotate.');
    this.canvas = document.createElement('canvas');
    this.ctx = this.canvas.getContext('2d');
    el.appendChild(this.canvas);
    if (this.poster) el.appendChild(this.poster);           // stays on top until the canvas has drawn
    var badge = document.createElement('div'); badge.className = 's360-badge'; badge.textContent = '360°';
    el.appendChild(badge);
    this.loadEl = document.createElement('div'); this.loadEl.className = 's360-load';
    this.loadEl.innerHTML = '<svg class="s360-ring" viewBox="0 0 46 46"><circle class="bg" cx="23" cy="23" r="18" stroke-width="6"/>' +
      '<circle class="fg" cx="23" cy="23" r="18" stroke-width="6" stroke-dasharray="113.1" stroke-dashoffset="113.1"/></svg>' +
      '<div class="s360-pct">360°</div>';
    el.appendChild(this.loadEl);
    this.ring = this.loadEl.querySelector('.fg');
    this.pct = this.loadEl.querySelector('.s360-pct');
    this.hint = document.createElement('div'); this.hint.className = 's360-hint gone';
    this.hint.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
      'stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/>' +
      '<path d="M3 21v-5h5"/></svg><span>Drag to rotate</span>';
    el.appendChild(this.hint);
    this.btn = document.createElement('button');
    this.btn.type = 'button'; this.btn.className = 's360-btn';
    this.btn.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
    this.btn.addEventListener('click', function (e) { e.stopPropagation(); self.toggleAuto(); });
    el.appendChild(this.btn);
    this.paintBtn();

    this.resize();
    if (window.ResizeObserver) new ResizeObserver(function () { self.resize(); }).observe(el);
    else window.addEventListener('resize', function () { self.resize(); });

    // Lazy: frames only start loading when the stone is (nearly) on screen
    if (window.IntersectionObserver) {
      this.visible = false;
      new IntersectionObserver(function (es) {
        es.forEach(function (e) {
          self.visible = e.isIntersecting;
          if (e.isIntersecting) { self.start(); self.kick(); }
        });
      }, { rootMargin: '200px 0px' }).observe(el);
    } else {
      this.start();
    }
    this.bindInput();
  };

  Spinner.prototype.src = function (i) { return MEDIA_BASE + this.id + '/' + pad3(i) + '.' + this.ext; };
  Spinner.prototype.srcHi = function (i) { return MEDIA_BASE + this.id + '/' + pad3(i) + '@hi.' + this.ext; };

  Spinner.prototype.paintBtn = function () {
    var paused = this.userPaused;
    this.btn.setAttribute('aria-label', paused ? 'Play automatic rotation' : 'Pause automatic rotation');
    this.btn.title = paused ? 'Play' : 'Pause';
    this.btn.setAttribute('data-state', paused ? 'paused' : 'playing');
    this.btn.innerHTML = paused ? '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>'
      : '<svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';
  };

  Spinner.prototype.toggleAuto = function () {
    this.userPaused = !this.userPaused;
    if (!this.userPaused) { this.auto = true; this.lastInteract = -1e9; this.ramp = 0; this.retarget(); }
    this.paintBtn();
    this.kick();
  };

  // When auto-rotate starts again from an arbitrary frame it first completes the turn to the top frame
  Spinner.prototype.retarget = function () {
    var n = this.n;
    var end = this.top + Math.ceil((this.pos - this.top) / n) * n;    // the next time round to the top frame
    if (end - this.pos < 0.5) end += n;
    this.turnStart = end - n;                                          // (the turn ends at turnStart + n)
  };

  Spinner.prototype.start = function () {
    if (this.started) return;
    this.started = true;
    // Load order starts at the top frame, then every 8th frame, then the rest
    var self = this, lo = loadOrder(this.n), tried = new Uint8Array(this.n), active = 0;
    var queue = lo[0].map(function (i) { return (i + self.top) % self.n; });
    var coarse = {};
    queue.slice(0, lo[1]).forEach(function (i) { coarse[i] = 1; });
    this.coarseLeft = lo[1];
    function next() {
      // The top frame goes alone first (nothing competes with it); then PARALLEL at a time
      var limit = self.loaded + self.failed === 0 ? 1 : PARALLEL;
      while (active < limit && queue.length) load(queue.shift());
    }
    function settle(i) {
      if (coarse[i]) { delete coarse[i]; if (--self.coarseLeft === 0) self.hint.classList.remove('gone'); }
    }
    function load(i) {
      active++;
      var im = new Image();
      im.decoding = 'async';
      if (i === self.top && im.fetchPriority !== undefined) im.fetchPriority = 'high';
      im.onload = function () {
        active--; self.imgs[i] = im; self.loaded++; settle(i); self.progress();
        if (self.drawn < 0) self.draw(true);           // first frame on screen as soon as it arrives
        next();
      };
      im.onerror = function () {
        active--;
        if (!tried[i]) { tried[i] = 1; queue.push(i); }  // one more try, at the end of the queue
        else { self.failed++; settle(i); self.progress(); }
        next();
      };
      im.src = self.src(i);                            // the page's top-frame image is reused from cache
    }
    next();
    this.loop();
  };

  Spinner.prototype.progress = function () {
    var done = this.loaded + this.failed, f = done / this.n;
    this.ring.setAttribute('stroke-dashoffset', String(113.1 * (1 - f)));
    this.pct.textContent = '360° ' + Math.round(f * 100) + '%';
    if (done >= this.n) {
      this.loadEl.classList.add('done');
      if (!this.loaded) this.fail();
      else this.startHi();
    }
  };

  // Full-quality set: after the small set is in, nearest-to-the-current-frame first, swapped in as it arrives
  Spinner.prototype.startHi = function () {
    if (!this.hasHi || this.hiStarted) return;
    var conn = navigator.connection || {};
    if (conn.saveData || (navigator.deviceMemory && navigator.deviceMemory <= 2)) return;   // data saver / very low memory
    this.hiStarted = true;
    var self = this, n = this.n, cur = ((Math.round(this.pos) % n) + n) % n, queue = [], active = 0, tried = new Uint8Array(n), i;
    for (i = 0; i < n; i++) if (this.imgs[i]) queue.push(i);
    queue.sort(function (a, b) {
      var da = Math.min((a - cur + n) % n, (cur - a + n) % n), db = Math.min((b - cur + n) % n, (cur - b + n) % n);
      return da - db;
    });
    function next() { while (active < HI_PARALLEL && queue.length) load(queue.shift()); }
    function load(i) {
      active++;
      var im = new Image();
      im.decoding = 'async';
      im.onload = function () {
        active--; self.hi[i] = im; self.hiLoaded++;
        if (self.drawn === i || (self.drawn >= 0 && self.nearest(((Math.round(self.pos) % n) + n) % n) === i)) self.draw(true);
        next();
      };
      im.onerror = function () { active--; if (!tried[i]) { tried[i] = 1; queue.push(i); } next(); };
      im.src = self.srcHi(i);
    }
    next();
  };

  Spinner.prototype.fail = function () {
    var e = document.createElement('div'); e.className = 's360-err';
    e.textContent = '360° view is unavailable right now.';
    this.el.appendChild(e);
    this.hint.classList.add('gone');
  };

  Spinner.prototype.resize = function () {
    var r = this.el.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
    var w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w; this.canvas.height = h; this.draw(true);
    }
  };

  // The nearest frame that has loaded (so rotating works before loading finishes)
  Spinner.prototype.nearest = function (i) {
    var n = this.n;
    if (this.imgs[i]) return i;
    for (var d = 1; d < n; d++) {
      var a = (i + d) % n, b = (i - d + n) % n;
      if (this.imgs[a]) return a;
      if (this.imgs[b]) return b;
    }
    return -1;
  };

  Spinner.prototype.draw = function (force) {
    var n = this.n, i = ((Math.round(this.pos) % n) + n) % n, k = this.nearest(i);
    if (k < 0 || (k === this.drawn && !force)) return;
    var small = this.imgs[k], im = this.hi[k] || small, c = this.canvas, ctx = this.ctx;
    // Size from the small frame (same aspect ratio as the full-quality one), whole device pixels
    var s = Math.min(c.width / small.naturalWidth, c.height / small.naturalHeight);
    var w = Math.round(small.naturalWidth * s), h = Math.round(small.naturalHeight * s);
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, c.width, c.height);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(im, Math.round((c.width - w) / 2), Math.round((c.height - h) / 2), w, h);
    this.drawn = k; this.drawnHi = !!this.hi[k];
    if (this.poster) { this.poster.classList.add('gone'); this.poster = null; }
  };

  Spinner.prototype.kick = function () { if (!this.raf && this.started) this.loop(); };

  Spinner.prototype.loop = function () {
    var self = this, last = performance.now();
    function tick(t) {
      var dt = Math.min(0.05, (t - last) / 1000); last = t;
      self.raf = 0;
      if (!self.visible) return;                          // resumes when back on screen
      var ready = self.loaded + self.failed >= self.n;
      var idle = !self.dragging && t - self.lastInteract >= RESUME_MS && Math.abs(self.vel) < 0.5;
      if (self.auto && !self.userPaused && idle && ready && self.loaded && t >= self.restUntil) {
        if (self.ramp === 0) self.retarget();
        self.ramp = Math.min(1, self.ramp + dt / RAMP_S);
        self.pos += dt * self.n / TURN_SECONDS * self.ramp * self.ramp * (3 - 2 * self.ramp);
        if (self.pos >= self.turnStart + self.n) {          // back on the top frame: rest there a moment
          self.pos = self.turnStart = self.turnStart + self.n;
          self.restUntil = t + TOP_PAUSE * 1000;
        }
      } else if (!idle) {
        self.ramp = 0;                                       // the next start eases in from rest
      }
      if (!self.dragging && Math.abs(self.vel) > 0.01) {  // a little inertia after a flick
        self.pos += self.vel * dt;
        self.vel *= Math.exp(-dt * 5);
      }
      self.draw(false);
      self.raf = requestAnimationFrame(tick);
    }
    this.raf = requestAnimationFrame(tick);
  };

  Spinner.prototype.touch = function () {
    this.lastInteract = performance.now();                  // auto-rotate resumes RESUME_MS after the last interaction
    this.ramp = 0;
    if (this.touched) return;
    this.touched = true;
    this.hint.classList.add('gone');
  };

  Spinner.prototype.bindInput = function () {
    var self = this, el = this.el, startX = 0, startY = 0, lastX = 0, lastT = 0, decided = false, id = null;
    function framesPerPx() { return self.n / (el.clientWidth * DRAG_TURN || 1); }
    el.addEventListener('pointerdown', function (e) {
      if (e.button && e.button !== 0) return;
      if (e.target && e.target.closest && e.target.closest('.s360-btn')) return;
      id = e.pointerId; startX = lastX = e.clientX; startY = e.clientY; lastT = performance.now();
      decided = e.pointerType === 'mouse';               // touch: wait to see if it's a vertical scroll
      if (decided) { self.dragging = true; el.classList.add('dragging'); try { el.setPointerCapture(id); } catch (x) {} }
      self.vel = 0; self.touch();
    });
    el.addEventListener('pointermove', function (e) {
      if (e.pointerId !== id) return;
      if (!decided) {
        var dx0 = Math.abs(e.clientX - startX), dy0 = Math.abs(e.clientY - startY);
        if (dx0 < 6 && dy0 < 6) return;
        if (dy0 > dx0) { id = null; return; }            // vertical: let the page scroll
        decided = true; self.dragging = true; el.classList.add('dragging');
        try { el.setPointerCapture(id); } catch (x) {}
      }
      var now = performance.now(), dx = e.clientX - lastX;
      self.pos -= dx * framesPerPx();
      var dt = Math.max(8, now - lastT) / 1000, cap = self.n * 1.5;   // flick speed: at most 1.5 turns/s
      self.vel = Math.max(-cap, Math.min(cap, 0.8 * (-dx * framesPerPx() / dt) + 0.2 * self.vel));
      lastX = e.clientX; lastT = now; self.lastInteract = now;
      self.draw(false);
      self.kick();
    });
    function end(e) {
      if (e.pointerId !== id) return;
      id = null;
      if (performance.now() - lastT > 90) self.vel = 0;   // held still before letting go: no flick
      self.dragging = false; el.classList.remove('dragging');
      self.lastInteract = performance.now();
      self.kick();
    }
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
    el.addEventListener('keydown', function (e) {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault(); self.touch(); self.vel = 0;
      self.pos = Math.round(self.pos) + (e.key === 'ArrowRight' ? 1 : -1) * Math.max(1, Math.round(self.n / 72));
      self.draw(false); self.kick();
    });
    el.addEventListener('dragstart', function (e) { e.preventDefault(); });
  };

  function valid(id, n) { return ID_RE.test(String(id || '')) && n >= 2 && n <= 720 && Math.floor(n) === n; }

  var Spin360 = {
    valid: function (spin) { return !!spin && valid(spin.id, Number(spin.n)); },
    mount: function (el, spin) {
      if (!el || !spin || !valid(spin.id, Number(spin.n))) return null;
      injectCss();
      var n = Number(spin.n), top = Math.floor(Number(spin.top) || 0);
      return new Spinner(el, String(spin.id), n, top >= 0 && top < n ? top : 0, Number(spin.v) || 0);
    },
    mountAll: function (root) {
      var els = (root || document).querySelectorAll('[data-spin-id][data-spin-n]'), out = [];
      for (var i = 0; i < els.length; i++) {
        if (els[i].__s360) continue;
        var s = Spin360.mount(els[i], { id: els[i].getAttribute('data-spin-id'), n: Number(els[i].getAttribute('data-spin-n')),
          top: Number(els[i].getAttribute('data-spin-top') || 0), v: Number(els[i].getAttribute('data-spin-v') || 0) });
        if (s) { els[i].__s360 = s; out.push(s); }
      }
      return out;
    },
    // Top frame URL (a plain image a page can show before the script runs)
    topSrc: function (spin) {
      var n = Number(spin.n), top = Math.floor(Number(spin.top) || 0);
      return MEDIA_BASE + spin.id + '/' + pad3(top >= 0 && top < n ? top : 0) + '.' + (Number(spin.v) >= WEBP_SINCE ? 'webp' : 'jpg');
    },
    // HTML placeholder for a page's own markup; mountAll() fills it in. The top frame is a lazy
    // <img> in the markup, so it shows as soon as the stone nears the screen; `opts.still` (our own
    // /media/ still image) sits behind it until it arrives.
    html: function (spin, opts) {
      if (!Spin360.valid(spin)) return '';
      injectCss();
      var top = Math.floor(Number(spin.top) || 0), v = Number(spin.v) || 0;
      var still = opts && typeof opts.still === 'string' && opts.still.indexOf(MEDIA_BASE) === 0 &&
        /^[a-f0-9\/.]+$/.test(opts.still.slice(MEDIA_BASE.length)) ? opts.still : '';
      return '<div class="s360-host" data-spin-id="' + spin.id + '" data-spin-n="' + Number(spin.n) +
        '" data-spin-top="' + (top >= 0 && top < spin.n ? top : 0) + '" data-spin-v="' + v + '"' +
        (still ? ' style="background-image:url(\'' + still + '\')"' : '') + '>' +
        '<img class="s360-poster" src="' + Spin360.topSrc(spin) + '" alt="" loading="lazy" decoding="async"></div>';
    },
  };
  window.Spin360 = Spin360;
})();
