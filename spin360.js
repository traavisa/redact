/* 360° spinner — plays our own re-hosted frames, no libraries.
 *
 * Frames live at https://quote.alldiamondeverything.com/media/<32-hex id>/000.jpg, 001.jpg, …
 * (captured and cleaned by the app). Nothing here loads from anywhere else.
 *
 *   Spin360.mount(el, { id, n })      one spinner in `el`
 *   Spin360.mountAll(root)            every [data-spin-id][data-spin-n] inside `root`
 *
 * Drag / swipe to rotate (vertical swipes still scroll the page), arrow keys when focused,
 * gentle auto-rotate until touched, frames preloaded coarse-to-fine with a progress ring
 * (you can rotate before everything has loaded), paused while off-screen.
 */
(function () {
  'use strict';
  var MEDIA_BASE = 'https://quote.alldiamondeverything.com/media/';
  var ID_RE = /^[a-f0-9]{32}$/;
  var TURN_SECONDS = 10;       // auto-rotate: one full turn
  var DRAG_TURN = 1.25;        // a drag across 1.25× the spinner's width = one full turn
  var PARALLEL = 6;

  var CSS = '' +
    '.s360{position:relative;width:100%;height:100%;background:#000;overflow:hidden;user-select:none;-webkit-user-select:none;' +
    'touch-action:pan-y;cursor:grab;outline:none;-webkit-tap-highlight-color:transparent}' +
    '.s360.dragging{cursor:grabbing}' +
    '.s360:focus-visible{box-shadow:inset 0 0 0 1px #c9a84c}' +
    '.s360 canvas{position:absolute;inset:0;width:100%;height:100%;display:block}' +
    '.s360-load{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;' +
    'background:rgba(0,0,0,.35);transition:opacity .45s;pointer-events:none}' +
    '.s360-load.done{opacity:0}' +
    '.s360-ring{width:46px;height:46px;transform:rotate(-90deg)}' +
    '.s360-ring circle{fill:none;stroke-width:2}' +
    '.s360-ring .bg{stroke:rgba(255,255,255,.12)}' +
    '.s360-ring .fg{stroke:#c9a84c;stroke-linecap:round;transition:stroke-dashoffset .2s}' +
    '.s360-pct{font:500 10px/1 Inter,-apple-system,sans-serif;letter-spacing:.14em;color:rgba(255,255,255,.7);text-transform:uppercase}' +
    '.s360-hint{position:absolute;left:50%;bottom:12px;transform:translateX(-50%);display:flex;align-items:center;gap:7px;' +
    'padding:6px 11px;border-radius:99px;background:rgba(10,10,10,.62);border:1px solid rgba(201,168,76,.28);' +
    'font:500 10px/1 Inter,-apple-system,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:#d8c48a;' +
    'transition:opacity .5s;pointer-events:none;white-space:nowrap}' +
    '.s360-hint svg{width:14px;height:14px}' +
    '.s360-hint.gone{opacity:0}' +
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

  // 0, n/2, n/4, 3n/4, n/8 … : a rough full turn is available early, then it fills in
  function loadOrder(n) {
    var out = [], seen = new Uint8Array(n), step = 1;
    while (step < n) step *= 2;
    for (; step >= 1; step = step / 2) {
      for (var i = 0; i < n; i += step) { if (!seen[i]) { seen[i] = 1; out.push(i); } }
      if (step === 1) break;
    }
    return out;
  }

  function Spinner(el, id, n) {
    this.el = el; this.id = id; this.n = n;
    this.imgs = new Array(n); this.loaded = 0; this.failed = 0;
    this.pos = 0; this.vel = 0; this.auto = !matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.touched = false; this.visible = true; this.started = false; this.drawn = -1;
    this.build();
  }

  Spinner.prototype.build = function () {
    var el = this.el, self = this;
    el.innerHTML = '';
    el.classList.add('s360');
    el.tabIndex = 0;
    el.setAttribute('role', 'img');
    el.setAttribute('aria-label', '360° view of the diamond. Drag, swipe or use the arrow keys to rotate.');
    this.canvas = document.createElement('canvas');
    this.ctx = this.canvas.getContext('2d');
    el.appendChild(this.canvas);
    var badge = document.createElement('div'); badge.className = 's360-badge'; badge.textContent = '360°';
    el.appendChild(badge);
    this.loadEl = document.createElement('div'); this.loadEl.className = 's360-load';
    this.loadEl.innerHTML = '<svg class="s360-ring" viewBox="0 0 46 46"><circle class="bg" cx="23" cy="23" r="20"/>' +
      '<circle class="fg" cx="23" cy="23" r="20" stroke-dasharray="125.66" stroke-dashoffset="125.66"/></svg>' +
      '<div class="s360-pct">Loading 360°</div>';
    el.appendChild(this.loadEl);
    this.ring = this.loadEl.querySelector('.fg');
    this.pct = this.loadEl.querySelector('.s360-pct');
    this.hint = document.createElement('div'); this.hint.className = 's360-hint';
    this.hint.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
      'stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/>' +
      '<path d="M3 21v-5h5"/></svg><span>Drag to rotate</span>';
    el.appendChild(this.hint);

    this.resize();
    if (window.ResizeObserver) new ResizeObserver(function () { self.resize(); }).observe(el);
    else window.addEventListener('resize', function () { self.resize(); });

    if (window.IntersectionObserver) {
      new IntersectionObserver(function (es) {
        es.forEach(function (e) {
          self.visible = e.isIntersecting;
          if (e.isIntersecting) { self.start(); self.kick(); }
        });
      }, { rootMargin: '300px 0px' }).observe(el);
    } else {
      this.start();
    }
    this.bindInput();
  };

  Spinner.prototype.src = function (i) { return MEDIA_BASE + this.id + '/' + pad3(i) + '.jpg'; };

  Spinner.prototype.start = function () {
    if (this.started) return;
    this.started = true;
    var self = this, queue = loadOrder(this.n), tried = new Uint8Array(this.n), active = 0;
    function next() {
      while (active < PARALLEL && queue.length) load(queue.shift());
    }
    function load(i) {
      active++;
      var im = new Image();
      im.decoding = 'async';
      im.onload = function () {
        active--; self.imgs[i] = im; self.loaded++; self.progress();
        if (self.drawn < 0) self.draw(true);           // first frame on screen as soon as it arrives
        next();
      };
      im.onerror = function () {
        active--;
        if (!tried[i]) { tried[i] = 1; queue.push(i); }  // one more try, at the end of the queue
        else { self.failed++; self.progress(); }
        next();
      };
      im.src = self.src(i);
    }
    next();
    this.loop();
  };

  Spinner.prototype.progress = function () {
    var done = this.loaded + this.failed, f = done / this.n;
    this.ring.setAttribute('stroke-dashoffset', String(125.66 * (1 - f)));
    this.pct.textContent = 'Loading ' + Math.round(f * 100) + '%';
    if (done >= this.n) {
      this.loadEl.classList.add('done');
      if (!this.loaded) this.fail();
    }
  };

  Spinner.prototype.fail = function () {
    var e = document.createElement('div'); e.className = 's360-err';
    e.textContent = '360° view is unavailable right now.';
    this.el.appendChild(e);
    this.hint.classList.add('gone');
  };

  Spinner.prototype.resize = function () {
    var r = this.el.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
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
    var im = this.imgs[k], c = this.canvas, ctx = this.ctx;
    var s = Math.min(c.width / im.naturalWidth, c.height / im.naturalHeight);
    var w = im.naturalWidth * s, h = im.naturalHeight * s;
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, c.width, c.height);
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(im, (c.width - w) / 2, (c.height - h) / 2, w, h);
    this.drawn = k;
  };

  Spinner.prototype.kick = function () { if (!this.raf && this.started) this.loop(); };

  Spinner.prototype.loop = function () {
    var self = this, last = performance.now();
    function tick(t) {
      var dt = Math.min(0.05, (t - last) / 1000); last = t;
      self.raf = 0;
      if (!self.visible) return;                          // resumes when back on screen
      var ready = self.loaded + self.failed >= self.n;
      if (self.auto && !self.touched && ready && self.loaded) self.pos += dt * self.n / TURN_SECONDS;
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
    if (this.touched) return;
    this.touched = true;
    this.hint.classList.add('gone');
  };

  Spinner.prototype.bindInput = function () {
    var self = this, el = this.el, startX = 0, startY = 0, lastX = 0, lastT = 0, decided = false, id = null;
    function framesPerPx() { return self.n / (el.clientWidth * DRAG_TURN || 1); }
    el.addEventListener('pointerdown', function (e) {
      if (e.button && e.button !== 0) return;
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
      lastX = e.clientX; lastT = now;
      self.draw(false);
      self.kick();
    });
    function end(e) {
      if (e.pointerId !== id) return;
      id = null;
      if (performance.now() - lastT > 90) self.vel = 0;   // held still before letting go: no flick
      self.dragging = false; el.classList.remove('dragging');
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
      return new Spinner(el, String(spin.id), Number(spin.n));
    },
    mountAll: function (root) {
      var els = (root || document).querySelectorAll('[data-spin-id][data-spin-n]'), out = [];
      for (var i = 0; i < els.length; i++) {
        if (els[i].__s360) continue;
        var s = Spin360.mount(els[i], { id: els[i].getAttribute('data-spin-id'), n: Number(els[i].getAttribute('data-spin-n')) });
        if (s) { els[i].__s360 = s; out.push(s); }
      }
      return out;
    },
    // HTML placeholder for a page's own markup; mountAll() fills it in
    html: function (spin) {
      if (!Spin360.valid(spin)) return '';
      return '<div class="s360-host" data-spin-id="' + spin.id + '" data-spin-n="' + Number(spin.n) + '"></div>';
    },
  };
  window.Spin360 = Spin360;
})();
