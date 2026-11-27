/* Labs → Library series sweep. The page works as plain forms; this adds the
   row filter, the tick helpers and the progress poll. Loaded only there. */
(function () {
  'use strict';

  const run = document.getElementById('sweep-run');
  if (run && run.dataset.running === '1') {
    const text = document.getElementById('sweep-progress-text');
    const bar = document.getElementById('sweep-progress');
    const poll = async function () {
      try {
        const res = await fetch(run.dataset.statusUrl, { headers: { Accept: 'application/json' } });
        const job = await res.json();
        if (!job.running) { window.location.reload(); return; }
        if (bar) { bar.max = job.total || 1; bar.value = job.done; }
        if (text) text.textContent = 'Checking your library… ' + job.done + ' of ' + (job.total || '?');
      } catch (e) { /* a blip: try again on the next tick */ }
      window.setTimeout(poll, 2000);
    };
    window.setTimeout(poll, 1500);
  }

  const form = document.getElementById('sweep-form');
  if (!form) return;
  const groups = Array.prototype.slice.call(form.querySelectorAll('[data-group]'));
  const rows = Array.prototype.slice.call(form.querySelectorAll('[data-row]'));
  const filter = document.getElementById('sweep-filter');
  const confidence = document.getElementById('sweep-confidence');
  const picked = document.getElementById('sweep-picked');
  const apply = document.getElementById('sweep-apply');

  function box(row) { return row.querySelector('input[name="item"]'); }
  function rowsOf(group) {
    return Array.prototype.slice.call(group.querySelectorAll('[data-row]'))
      .filter(function (r) { return !r.hidden; });
  }

  // The count on the button, and each series' own tick box (ticked / some / none).
  function count() {
    const n = rows.filter(function (r) { return box(r).checked; }).length;
    picked.textContent = String(n);
    apply.disabled = n === 0;
    groups.forEach(function (g) {
      const mine = rowsOf(g);
      const on = mine.filter(function (r) { return box(r).checked; }).length;
      const tick = g.querySelector('[data-sweep="group-tick"]');
      tick.checked = mine.length > 0 && on === mine.length;
      tick.indeterminate = on > 0 && on < mine.length;
    });
  }

  // Hidden rows are never submitted ticked: what you apply is what you see.
  // A series with no visible proposal is hidden whole (its owned books are
  // only context).
  function applyFilter() {
    const q = (filter.value || '').trim().toLowerCase();
    const c = confidence.value;
    rows.forEach(function (r) {
      const show = (!q || r.dataset.text.indexOf(q) !== -1) && (!c || r.dataset.confidence === c);
      r.hidden = !show;
      if (!show) box(r).checked = false;
    });
    groups.forEach(function (g) { g.hidden = rowsOf(g).length === 0; });
    count();
  }

  filter.addEventListener('input', applyFilter);
  confidence.addEventListener('change', applyFilter);
  form.addEventListener('change', function (e) {
    if (e.target.name === 'item') { count(); return; }
    if (e.target.dataset.sweep === 'group-tick') {
      const on = e.target.checked;
      rowsOf(e.target.closest('[data-group]')).forEach(function (r) { box(r).checked = on; });
      count();
    }
  });
  form.addEventListener('input', function (e) {
    // One name per series: copy it to every book in the group.
    if (e.target.dataset.sweep === 'group-name') {
      const g = e.target.closest('[data-group]');
      g.querySelectorAll('[data-series]').forEach(function (i) { i.value = e.target.value; });
      return;
    }
    // Editing a number means you've looked at it: tick the row.
    if (e.target.classList.contains('labs-input-seq')) {
      box(e.target.closest('[data-row]')).checked = true;
      count();
    }
  });
  form.addEventListener('click', function (e) {
    const btn = e.target.closest('button[data-sweep]');
    if (!btn) return;
    const high = btn.dataset.sweep === 'pick-high';
    rows.forEach(function (r) {
      if (r.hidden) return;
      box(r).checked = high && r.dataset.confidence === 'high';
    });
    count();
  });
  // Enter in a text box would press the first submit button (a row's Skip).
  form.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && e.target.tagName === 'INPUT' && e.target.type !== 'checkbox') {
      e.preventDefault();
    }
  });
  count();
})();
