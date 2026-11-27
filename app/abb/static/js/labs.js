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
  const rows = Array.prototype.slice.call(form.querySelectorAll('tbody tr'));
  const filter = document.getElementById('sweep-filter');
  const confidence = document.getElementById('sweep-confidence');
  const picked = document.getElementById('sweep-picked');
  const apply = document.getElementById('sweep-apply');

  function box(row) { return row.querySelector('input[name="item"]'); }

  function count() {
    const n = rows.filter(function (r) { return box(r).checked; }).length;
    picked.textContent = String(n);
    apply.disabled = n === 0;
  }

  // Hidden rows are never submitted ticked: what you apply is what you see.
  function applyFilter() {
    const q = (filter.value || '').trim().toLowerCase();
    const c = confidence.value;
    rows.forEach(function (r) {
      const show = (!q || r.dataset.text.indexOf(q) !== -1) && (!c || r.dataset.confidence === c);
      r.hidden = !show;
      if (!show) box(r).checked = false;
    });
    count();
  }

  filter.addEventListener('input', applyFilter);
  confidence.addEventListener('change', applyFilter);
  form.addEventListener('change', function (e) {
    if (e.target.name === 'item') count();
  });
  // Editing a proposal means you've looked at it: tick the row.
  form.addEventListener('input', function (e) {
    if (e.target.classList.contains('labs-input')) {
      box(e.target.closest('tr')).checked = true;
      count();
    }
  });
  form.addEventListener('click', function (e) {
    const btn = e.target.closest('[data-sweep]');
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
