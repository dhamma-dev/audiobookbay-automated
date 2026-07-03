(function () {
  'use strict';

  const TOAST_ICONS = {
    success: 'check-circle',
    error: 'alert-circle',
    info: 'info',
  };

  /* ----------------------------------------------------------
     Utilities
     ---------------------------------------------------------- */
  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }

  function refreshIcons() {
    if (window.lucide) window.lucide.createIcons();
  }

  /* ----------------------------------------------------------
     Toasts
     ---------------------------------------------------------- */
  function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.setAttribute('role', 'status');

    const iconName = TOAST_ICONS[type] || 'info';
    toast.innerHTML =
      `<i data-lucide="${iconName}" class="toast-icon" aria-hidden="true"></i>` +
      `<span>${escapeHtml(message)}</span>`;

    container.appendChild(toast);
    refreshIcons();

    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(20px)';
      toast.style.transition = 'opacity 220ms ease, transform 220ms ease';
      setTimeout(() => toast.remove(), 220);
    }, 5000);
  }

  /* ----------------------------------------------------------
     Button state helpers (Send to <client>)
     ---------------------------------------------------------- */
  function setButtonLoading(btn, loading) {
    if (loading) {
      btn.disabled = true;
      btn.dataset.originalHtml = btn.innerHTML;
      btn.innerHTML = '<span class="spinner-icon" aria-hidden="true"></span> Sending…';
    } else if (btn.dataset.originalHtml) {
      btn.disabled = false;
      btn.innerHTML = btn.dataset.originalHtml;
      delete btn.dataset.originalHtml;
      refreshIcons();
    }
  }

  function setButtonSuccess(btn) {
    btn.disabled = true;
    btn.classList.remove('btn-primary');
    btn.classList.add('btn-success');
    btn.innerHTML = '<i data-lucide="check" aria-hidden="true"></i> Added';
    refreshIcons();
    setTimeout(() => {
      btn.classList.remove('btn-success');
      btn.classList.add('btn-primary');
      setButtonLoading(btn, false);
    }, 3000);
  }

  async function handleDownload(btn) {
    const link = btn.dataset.link;
    const title = btn.dataset.title;
    if (!link || !title) return;

    setButtonLoading(btn, true);
    try {
      const res = await fetch('/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ link, title }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.message || 'Download failed');
      showToast(data.message || 'Download added.', 'success');
      setButtonSuccess(btn);
    } catch (err) {
      showToast(err.message || 'Download failed', 'error');
      setButtonLoading(btn, false);
    }
  }

  /* ----------------------------------------------------------
     Search: AJAX submit with guaranteed loading cleanup
     ---------------------------------------------------------- */
  function setSearchLoading(loading) {
    const results = document.getElementById('search-results');
    const skeletons = document.getElementById('search-skeletons');
    const submitBtn = document.getElementById('search-submit');

    if (loading) {
      if (results) {
        results.setAttribute('aria-busy', 'true');
        results.hidden = true;
      }
      if (skeletons) {
        skeletons.hidden = false; // removing [hidden] lets .is-loading take over
        skeletons.classList.add('is-loading');
        skeletons.setAttribute('aria-hidden', 'false');
      }
      if (submitBtn && !submitBtn.dataset.originalHtml) {
        submitBtn.disabled = true;
        submitBtn.dataset.originalHtml = submitBtn.innerHTML;
        submitBtn.innerHTML = '<span class="spinner-icon" aria-hidden="true"></span> Searching…';
      }
    } else {
      if (results) {
        results.removeAttribute('aria-busy');
        results.hidden = false;
      }
      if (skeletons) {
        skeletons.classList.remove('is-loading');
        skeletons.hidden = true; // [hidden]!important guarantees they go away
        skeletons.setAttribute('aria-hidden', 'true');
      }
      if (submitBtn && submitBtn.dataset.originalHtml) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = submitBtn.dataset.originalHtml;
        delete submitBtn.dataset.originalHtml;
        refreshIcons();
      }
    }
  }

  async function handleSearch(form) {
    const results = document.getElementById('search-results');
    if (!results) {
      form.submit(); // no place to inject; fall back to a normal POST
      return;
    }
    if (typeof form.reportValidity === 'function' && !form.reportValidity()) {
      return; // let the browser surface the "required" message
    }

    setSearchLoading(true);
    try {
      const res = await fetch(form.action || window.location.href, {
        method: 'POST',
        body: new FormData(form),
        headers: { 'X-Requested-With': 'fetch' },
      });
      if (!res.ok) {
        // Tor-still-starting (503) and other errors come back as JSON.
        let msg = 'Search request failed (' + res.status + ')';
        try { const j = await res.json(); if (j && j.message) msg = j.message; } catch (e) { /* not JSON */ }
        throw new Error(msg);
      }

      const html = await res.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const fresh = doc.getElementById('search-results');
      if (fresh) {
        results.innerHTML = fresh.innerHTML;
        smartSortReset(); // new result set => any cached ranking is stale
      } else {
        results.innerHTML =
          '<div class="empty-state">' +
          '<i data-lucide="alert-circle" class="empty-state-icon" aria-hidden="true"></i>' +
          '<p class="empty-state-title">Something went wrong</p>' +
          '<p class="empty-state-subtitle">Couldn\'t read the search results. Please try again.</p>' +
          '</div>';
      }
    } catch (err) {
      results.innerHTML =
        '<div class="empty-state">' +
        '<i data-lucide="wifi-off" class="empty-state-icon" aria-hidden="true"></i>' +
        '<p class="empty-state-title">Search failed</p>' +
        '<p class="empty-state-subtitle">' + escapeHtml(err.message || 'Please try again.') + '</p>' +
        '</div>';
      showToast(err.message || 'Search failed', 'error');
    } finally {
      // Whatever happened above, the loading state is always cleared here.
      setSearchLoading(false);
      refreshIcons();
    }
  }

  /* ----------------------------------------------------------
     Smart sort: Gemini re-ranking of already-loaded results
     ---------------------------------------------------------- */
  const smartSortCache = new Map(); // query -> ranking response
  let smartSortState = null;        // { query, ranking, expanded, activeInterp }

  function smartSortReset() {
    smartSortCache.clear();
    smartSortState = null;
  }

  function setSmartSortLoading(btn, loading) {
    const results = document.getElementById('search-results');
    if (loading) {
      if (results) results.classList.add('is-ranking');
      btn.disabled = true;
      btn.dataset.originalHtml = btn.innerHTML;
      btn.innerHTML = '<span class="spinner-icon" aria-hidden="true"></span> Ranking…';
    } else {
      if (results) results.classList.remove('is-ranking');
      btn.disabled = false;
      if (btn.dataset.originalHtml) {
        btn.innerHTML = btn.dataset.originalHtml;
        delete btn.dataset.originalHtml;
      }
      refreshIcons();
    }
  }

  function renderAmbiguity(ranking) {
    const box = document.getElementById('smart-sort-ambiguity');
    if (!box) return;
    const interps = (ranking && ranking.interpretations) || [];
    if (!ranking.ambiguous || interps.length === 0) {
      box.hidden = true;
      box.innerHTML = '';
      return;
    }
    let html =
      '<span class="smart-sort-ambiguity-label">' +
      '<i data-lucide="help-circle" aria-hidden="true"></i> This could mean a few things:</span>' +
      '<button type="button" class="chip chip-active" data-interp="all">All results</button>';
    interps.forEach((it, i) => {
      const desc = it.description ? ' title="' + escapeHtml(it.description) + '"' : '';
      html += '<button type="button" class="chip" data-interp="' + i + '"' + desc + '>' +
              escapeHtml(it.label) + '</button>';
    });
    box.innerHTML = html;
    box.hidden = false;
    refreshIcons();
  }

  // Reorder + show/hide cards based on the current ranking + UI state. Pure
  // DOM shuffling — no network — so interpretation chips and the filtered
  // toggle are instant.
  function renderSmartSort() {
    if (!smartSortState) return;
    const results = document.getElementById('search-results');
    const anchor = document.getElementById('smart-sort-show-filtered');
    if (!results || !anchor) return;

    const { ranking, expanded, activeInterp } = smartSortState;
    const ordering = (ranking.ordering || []).slice();
    const bucketOf = {};
    (ranking.buckets || []).forEach((b) => { bucketOf[String(b.id)] = b.bucket; });

    // Only the loose, top-level units are reordered here — bare cards plus
    // edition-group wrappers (keyed by their best card's id). Cards moved into a
    // series shelf or an edition tray are managed by that UI, not this flat sort.
    const cards = new Map();
    results.querySelectorAll(':scope > .book-card, :scope > .edition-group')
      .forEach((el) => cards.set(el.dataset.resultId, el));

    // Decide ordering + which ids are filtered away.
    let order = ordering;
    const hidden = new Set();
    const interps = ranking.interpretations || [];
    if (activeInterp != null && interps[activeInterp]) {
      const ids = new Set((interps[activeInterp].result_ids || []).map(String));
      const inInterp = ordering.filter((id) => ids.has(String(id)));
      const rest = ordering.filter((id) => !ids.has(String(id)));
      order = inInterp.concat(rest);
      rest.forEach((id) => hidden.add(String(id)));
    } else {
      ordering.forEach((id) => { if (bucketOf[String(id)] === 'unlikely') hidden.add(String(id)); });
    }

    // Apply order by moving each card just before the filtered-toggle button.
    order.forEach((id) => {
      const card = cards.get(String(id));
      if (card) results.insertBefore(card, anchor);
    });

    // Apply visibility / dimming.
    cards.forEach((card, id) => {
      const isFiltered = hidden.has(id);
      card.classList.toggle('is-dimmed-result', isFiltered);
      card.classList.toggle('is-hidden-result', isFiltered && !expanded);
    });

    // Series shelves respond to the active interpretation too: a whole shelf
    // whose books aren't part of the chosen interpretation is filtered away,
    // just like a loose card. (Buckets never hide curated series picks.)
    let hiddenSeries = 0;
    results.querySelectorAll('.series-group').forEach((group) => {
      let belongs = true;
      if (activeInterp != null && interps[activeInterp]) {
        const ids = new Set((interps[activeInterp].result_ids || []).map(String));
        belongs = Array.prototype.some.call(
          group.querySelectorAll('.book-card'),
          (c) => ids.has(c.dataset.resultId)
        );
      }
      const isFiltered = !belongs;
      group.classList.toggle('is-dimmed-result', isFiltered);
      group.classList.toggle('is-hidden-result', isFiltered && !expanded);
      if (isFiltered) {
        hiddenSeries += group.querySelectorAll('.series-entry:not(.is-gap):not(.is-collection)').length;
      }
    });

    // Filtered-results toggle.
    const n = hidden.size + hiddenSeries;
    if (n === 0) {
      anchor.hidden = true;
    } else {
      anchor.hidden = false;
      anchor.textContent = (expanded ? 'Hide ' : 'Show ') + n + ' filtered result' + (n === 1 ? '' : 's');
    }
  }

  // Paint (or upgrade) the "In your library" flag on a card. Ownership is
  // computed server-side against the local ABS index; here we just render it,
  // creating the flag for matches the initial deterministic pass didn't catch.
  function setLibraryFlag(card, status, detail) {
    if (!card) return;
    const state = status === 'partial' ? 'partial'
      : status === 'owned_other_edition' ? 'edition' : 'owned';
    card.setAttribute('data-in-library', state);
    const details = card.querySelector('.book-details');
    if (!details) return;
    let flag = card.querySelector('[data-library-flag]');
    if (!flag) {
      flag = document.createElement('div');
      flag.className = 'library-flag';
      flag.setAttribute('data-library-flag', '');
      flag.innerHTML =
        '<i data-lucide="library-big" aria-hidden="true"></i><span class="library-flag-text"></span>';
      const title = details.querySelector('.book-title');
      if (title) title.insertAdjacentElement('afterend', flag);
      else details.prepend(flag);
    }
    flag.classList.toggle('library-flag-partial', state === 'partial');
    const txt = flag.querySelector('.library-flag-text');
    if (txt) {
      txt.textContent = state === 'partial' ? 'Own ' + (detail || 'some')
        : state === 'edition' ? 'In library · other edition'
        : 'In your library';
    }
  }

  function applyOwnership(ranking) {
    const own = ranking && ranking.ownership;
    const results = document.getElementById('search-results');
    if (!Array.isArray(own) || !results) return;
    own.forEach((o) => {
      const card = results.querySelector('.book-card[data-result-id="' + o.id + '"]');
      if (card) setLibraryFlag(card, o.status, o.detail);
    });
    // Note how many books of each series shelf you already own.
    results.querySelectorAll('.series-group').forEach((group) => {
      let owned = 0;
      group.querySelectorAll('.series-entry:not(.is-gap):not(.is-collection)')
        .forEach((entry) => { if (entry.querySelector('.book-card[data-in-library]')) owned++; });
      const count = group.querySelector('.series-group-count');
      if (owned && count && !count.dataset.ownNoted) {
        count.dataset.ownNoted = '1';
        count.insertAdjacentHTML('beforeend',
          ' <span class="series-owned-note">· own ' + owned + '</span>');
      }
    });
    refreshIcons();
  }

  function applyRanking(query, ranking) {
    smartSortState = { query, ranking, expanded: false, activeInterp: null };
    renderAmbiguity(ranking);
    renderSeries(ranking);
    renderEditions(ranking);
    renderSmartSort();
    applyOwnership(ranking);
    const header = document.querySelector('.search-results-header');
    if (header && !header.dataset.ranked) {
      header.dataset.ranked = '1';
      header.insertAdjacentHTML('beforeend', ' <span class="results-ranked-note">· sorted by relevance</span>');
    }
  }

  async function handleSmartSort(btn) {
    const query = btn.dataset.query || '';
    const dataEl = document.getElementById('search-results-data');
    if (!dataEl) return;
    let results;
    try {
      results = JSON.parse(dataEl.textContent);
    } catch (e) {
      return;
    }
    if (!Array.isArray(results) || results.length === 0) return;

    if (smartSortCache.has(query)) {
      applyRanking(query, smartSortCache.get(query));
      return;
    }

    setSmartSortLoading(btn, true);
    try {
      const res = await fetch('/api/rank', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, results }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.message || 'Smart sort failed');
      smartSortCache.set(query, data);
      applyRanking(query, data);
      showToast('Sorted by relevance.', 'success');
    } catch (err) {
      showToast(err.message || 'Smart sort failed', 'error');
    } finally {
      setSmartSortLoading(btn, false);
    }
  }

  function handleInterpChip(chip) {
    if (!smartSortState) return;
    const box = chip.parentElement;
    box.querySelectorAll('.chip').forEach((c) => c.classList.remove('chip-active'));
    chip.classList.add('chip-active');
    const val = chip.dataset.interp;
    smartSortState.activeInterp = val === 'all' ? null : Number(val);
    smartSortState.expanded = false;
    renderSmartSort();
  }

  function handleToggleFiltered() {
    if (!smartSortState) return;
    smartSortState.expanded = !smartSortState.expanded;
    renderSmartSort();
  }

  /* ----------------------------------------------------------
     Series grouping: lay a detected series out as an ordered set
     of (compacted) cards with per-book selection, an alternatives
     tray, and a "send selected" batch. Built by moving the existing
     server-rendered cards into group containers — no re-render.
     ---------------------------------------------------------- */
  function buildSeriesEntry(e, cards, consumed) {
    const bestKey = e.best_id != null ? String(e.best_id) : null;
    const bestCard = bestKey && !consumed.has(bestKey) ? cards.get(bestKey) : null;

    // No real candidate for this slot — render a subtle gap marker so a missing
    // book in the middle of a run is visible without the user hunting for it.
    if (!bestCard) {
      const gap = document.createElement('div');
      gap.className = 'series-entry is-gap';
      if (e.seq != null) gap.dataset.seq = String(e.seq);
      gap.innerHTML =
        '<div class="series-entry-check series-entry-check--empty" aria-hidden="true"></div>' +
        '<div class="series-entry-body"><div class="series-gap-row">' +
        '<i data-lucide="circle-dashed" aria-hidden="true"></i>' +
        '<span class="series-gap-label">' +
        (e.seq != null ? 'Book ' + escapeHtml(String(e.seq)) : 'This book') +
        (e.title ? ' · ' + escapeHtml(e.title) : '') + '</span>' +
        '<span class="series-gap-note">not in these results</span>' +
        '</div></div>';
      return { el: gap, available: false };
    }

    consumed.add(bestKey);
    const altCards = (e.alt_ids || [])
      .map((id) => String(id))
      .filter((id) => cards.has(id) && !consumed.has(id))
      .map((id) => { consumed.add(id); return cards.get(id); });

    const entry = document.createElement('div');
    entry.className = 'series-entry';
    if (e.seq != null) entry.dataset.seq = String(e.seq);

    const gutter = document.createElement('label');
    gutter.className = 'series-entry-check';
    gutter.innerHTML =
      '<input type="checkbox" class="entry-include" checked aria-label="Include ' +
      escapeHtml(e.title || 'this book') + ' in the batch">';

    const body = document.createElement('div');
    body.className = 'series-entry-body';

    const caption = document.createElement('div');
    caption.className = 'series-entry-caption';
    caption.innerHTML =
      (e.seq != null ? '<span class="series-seq">Book ' + escapeHtml(String(e.seq)) + '</span>' : '') +
      (e.title ? '<span class="series-entry-title">' + escapeHtml(e.title) + '</span>' : '');

    const main = document.createElement('div');
    main.className = 'series-entry-main';
    bestCard.classList.add('in-series');
    main.appendChild(bestCard);

    body.appendChild(caption);
    body.appendChild(main);

    if (altCards.length) {
      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'btn btn-ghost series-alts-toggle';
      toggle.dataset.action = 'series-alts-toggle';
      if (e.alt_note) toggle.dataset.note = e.alt_note;
      const altsWrap = document.createElement('div');
      altsWrap.className = 'series-alts';
      altsWrap.hidden = true;
      altCards.forEach((c) => { c.classList.add('in-series', 'is-alt'); altsWrap.appendChild(c); });
      body.appendChild(toggle);
      body.appendChild(altsWrap);
    }

    entry.appendChild(gutter);
    entry.appendChild(body);
    return { el: entry, available: true };
  }

  // An omnibus / box set: one file covering several books. Rendered at the top
  // of the shelf, unchecked by default (individual books are the default), and
  // when ticked it suppresses the books it covers so nothing is grabbed twice.
  function buildSeriesCollection(c, cards, consumed) {
    const key = c.id != null ? String(c.id) : null;
    const card = key && !consumed.has(key) ? cards.get(key) : null;
    if (!card) return null;
    consumed.add(key);

    const covers = (c.covers || []).map((n) => String(n));
    const entry = document.createElement('div');
    entry.className = 'series-entry is-collection';
    if (covers.length) entry.dataset.covers = covers.join(',');

    const gutter = document.createElement('label');
    gutter.className = 'series-entry-check';
    gutter.innerHTML =
      '<input type="checkbox" class="entry-include" aria-label="Include the ' +
      escapeHtml(c.title || 'collection') + ' in the batch">';

    const body = document.createElement('div');
    body.className = 'series-entry-body';

    const caption = document.createElement('div');
    caption.className = 'series-entry-caption';
    const range = covers.length
      ? 'Covers books ' + (covers.length > 1 ? covers[0] + '–' + covers[covers.length - 1] : covers[0])
      : 'Collection';
    caption.innerHTML =
      '<span class="series-seq series-seq--set"><i data-lucide="package" aria-hidden="true"></i> Complete set</span>' +
      '<span class="series-entry-title">' + escapeHtml(range) + '</span>';

    const main = document.createElement('div');
    main.className = 'series-entry-main';
    card.classList.add('in-series', 'is-collection-card');
    main.appendChild(card);

    body.appendChild(caption);
    body.appendChild(main);
    entry.appendChild(gutter);
    entry.appendChild(body);
    return entry;
  }

  // Keep an entry's "chosen" card (the one in the main slot) free of the
  // edition-picker button, ensure every alternative has one, and refresh the
  // alternatives toggle label.
  function decorateEntry(entry) {
    const main = entry.querySelector('.series-entry-main');
    const altsWrap = entry.querySelector('.series-alts');
    const mainCard = main && main.querySelector('.book-card');
    if (mainCard) {
      mainCard.classList.add('is-chosen');
      const existing = mainCard.querySelector('.series-use-btn');
      if (existing) existing.remove();
    }
    let n = 0;
    if (altsWrap) {
      altsWrap.querySelectorAll('.book-card').forEach((c) => {
        c.classList.remove('is-chosen');
        if (!c.querySelector('.series-use-btn')) {
          const actions = c.querySelector('.book-actions');
          if (actions) {
            const useBtn = document.createElement('button');
            useBtn.type = 'button';
            useBtn.className = 'btn btn-secondary series-use-btn';
            useBtn.dataset.action = 'series-use-edition';
            useBtn.innerHTML = '<i data-lucide="check" aria-hidden="true"></i> Use this edition';
            actions.insertBefore(useBtn, actions.firstChild);
          }
        }
        n++;
      });
    }
    const toggle = entry.querySelector('.series-alts-toggle');
    if (toggle) {
      toggle.hidden = n === 0;
      const open = entry.classList.contains('alts-open');
      const note = toggle.dataset.note ? ' · ' + toggle.dataset.note : '';
      toggle.innerHTML =
        (open ? 'Hide ' : 'Show ') + n + ' alternative' + (n === 1 ? '' : 's') + note +
        ' <i data-lucide="chevron-' + (open ? 'up' : 'down') + '" aria-hidden="true"></i>';
    }
  }

  function updateSeriesCount(group) {
    if (!group) return;
    // The batch is every ticked, enabled box — a checked omnibus counts as one
    // send; books it covers are disabled so they don't double up.
    let n = 0;
    group.querySelectorAll('.entry-include').forEach((c) => { if (c.checked && !c.disabled) n++; });
    const btn = group.querySelector('[data-action="series-send"]');
    if (btn) {
      btn.disabled = n === 0;
      const lbl = btn.querySelector('.series-send-count');
      if (lbl) lbl.textContent = n;
    }
    // Select-all reflects the individual book entries only (not collections).
    const bookChecks = group.querySelectorAll('.series-entry:not(.is-collection):not(.is-gap) .entry-include');
    const all = group.querySelector('.series-select-all');
    if (all) {
      let checked = 0, total = 0;
      bookChecks.forEach((c) => { total++; if (c.checked) checked++; });
      all.checked = total > 0 && checked === total;
      all.indeterminate = checked > 0 && checked < total;
    }
  }

  // Reconcile book selection with any ticked collections: a book covered by a
  // selected omnibus is unticked + disabled; when no longer covered it is
  // re-enabled (and restored to ticked, since books default on).
  function syncCollections(group) {
    if (!group) return;
    const covered = new Set();
    group.querySelectorAll('.series-entry.is-collection').forEach((col) => {
      const inc = col.querySelector('.entry-include');
      if (inc && inc.checked) {
        (col.dataset.covers || '').split(',').filter(Boolean).forEach((s) => covered.add(s));
      }
    });
    group.querySelectorAll('.series-entry:not(.is-collection):not(.is-gap)').forEach((entry) => {
      const seq = entry.dataset.seq;
      const inc = entry.querySelector('.entry-include');
      const isCovered = seq != null && covered.has(seq);
      entry.classList.toggle('is-covered', isCovered);
      if (inc) {
        if (isCovered) { inc.checked = false; inc.disabled = true; }
        else {
          inc.disabled = false;
          if (entry.dataset.wasCovered === '1') inc.checked = true;
        }
      }
      entry.dataset.wasCovered = isCovered ? '1' : '';
    });
    updateSeriesCount(group);
  }

  function renderSeries(ranking) {
    const results = document.getElementById('search-results');
    if (!results) return;
    const series = (ranking && ranking.series) || [];
    if (series.length === 0) return;
    if (results.querySelector('.series-group')) return; // already built

    const cards = new Map();
    results.querySelectorAll('.book-card').forEach((c) => cards.set(c.dataset.resultId, c));
    const consumed = new Set(); // ids already claimed by an entry, across all series

    // Drop a hidden marker where groups should land: just after the results bar
    // / ambiguity strip, above the loose cards.
    const anchorAfter =
      document.getElementById('smart-sort-ambiguity') || results.querySelector('.search-results-bar');
    const sentinel = document.createElement('div');
    sentinel.style.display = 'none';
    results.insertBefore(sentinel, anchorAfter ? anchorAfter.nextSibling : results.firstChild);

    series.forEach((s) => {
      const group = document.createElement('section');
      group.className = 'series-group';
      group.dataset.label = s.label || '';

      const built = [];
      (s.entries || []).forEach((e) => {
        const r = buildSeriesEntry(e, cards, consumed);
        if (r) built.push(r);
      });
      const availableCount = built.filter((b) => b.available).length;
      if (availableCount < 2) {
        // Not enough real books to be worth a shelf — return every card these
        // entries pulled in (best *and* alternatives) to the loose flow.
        built.forEach((b) => {
          b.el.querySelectorAll('.book-card').forEach((card) => {
            card.classList.remove('in-series', 'is-alt', 'is-chosen');
            results.insertBefore(card, sentinel);
          });
        });
        return;
      }

      const collectionEls = [];
      (s.collections || []).forEach((c) => {
        const el = buildSeriesCollection(c, cards, consumed);
        if (el) collectionEls.push(el);
      });

      // "X of Y" only when we trust a larger canonical total; otherwise "N books".
      const total = (typeof s.total === 'number' && s.total > availableCount) ? s.total : null;
      const countText = total
        ? availableCount + ' of ' + total + ' available'
        : availableCount + ' book' + (availableCount === 1 ? '' : 's');

      const header = document.createElement('div');
      header.className = 'series-group-header';
      header.innerHTML =
        '<label class="series-select-all-wrap"><input type="checkbox" class="series-select-all" checked ' +
        'aria-label="Select all books in ' + escapeHtml(s.label || 'this series') + '"></label>' +
        '<div class="series-group-heading"><i data-lucide="library" aria-hidden="true"></i>' +
        '<span class="series-group-title">' + escapeHtml(s.label || 'Series') + '</span>' +
        '<span class="series-group-count">' + countText + '</span></div>' +
        '<button type="button" class="btn btn-primary series-send-btn" data-action="series-send">' +
        '<i data-lucide="download" aria-hidden="true"></i> Send <span class="series-send-count">' +
        availableCount + '</span> selected</button>';

      const list = document.createElement('div');
      list.className = 'series-entries';
      collectionEls.forEach((el) => list.appendChild(el));
      built.forEach((b) => list.appendChild(b.el));

      group.appendChild(header);
      group.appendChild(list);
      results.insertBefore(group, sentinel);

      built.forEach((b) => { if (b.available) decorateEntry(b.el); });
      updateSeriesCount(group);
    });

    sentinel.remove();
    refreshIcons();
  }

  // Standalone books that appear as several uploads: fold the duplicates into a
  // single card (the best edition) with an alternatives tray, so one specific
  // book is one entry instead of a wall of near-identical results.
  function renderEditions(ranking) {
    const results = document.getElementById('search-results');
    if (!results) return;
    const editions = (ranking && ranking.editions) || [];
    if (editions.length === 0) return;
    if (results.querySelector('.edition-group')) return; // already built

    const cards = new Map();
    results.querySelectorAll(':scope > .book-card').forEach((c) => cards.set(c.dataset.resultId, c));
    const consumed = new Set();

    editions.forEach((e) => {
      const bestKey = e.best_id != null ? String(e.best_id) : null;
      const bestCard = bestKey && !consumed.has(bestKey) ? cards.get(bestKey) : null;
      if (!bestCard) return;
      const altCards = (e.alt_ids || [])
        .map((id) => String(id))
        .filter((id) => id !== bestKey && cards.has(id) && !consumed.has(id))
        .map((id) => { consumed.add(id); return cards.get(id); });
      if (altCards.length === 0) return; // nothing to fold — leave it a plain card
      consumed.add(bestKey);

      const group = document.createElement('div');
      group.className = 'edition-group';
      group.dataset.resultId = bestKey;

      const main = document.createElement('div');
      main.className = 'series-entry-main';
      results.insertBefore(group, bestCard); // wrapper takes the card's place...
      main.appendChild(bestCard);            // ...then the card moves inside it

      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'btn btn-ghost series-alts-toggle';
      toggle.dataset.action = 'series-alts-toggle';
      if (e.alt_note) toggle.dataset.note = e.alt_note;

      const altsWrap = document.createElement('div');
      altsWrap.className = 'series-alts';
      altsWrap.hidden = true;
      altCards.forEach((c) => {
        c.classList.add('in-series', 'is-alt');
        c.classList.remove('is-hidden-result', 'is-dimmed-result');
        altsWrap.appendChild(c);
      });

      group.appendChild(main);
      group.appendChild(toggle);
      group.appendChild(altsWrap);
      decorateEntry(group);
    });

    refreshIcons();
  }

  function handleAltsToggle(toggle) {
    const entry = toggle.closest('.series-entry, .edition-group');
    if (!entry) return;
    const altsWrap = entry.querySelector('.series-alts');
    const open = entry.classList.toggle('alts-open');
    if (altsWrap) altsWrap.hidden = !open;
    decorateEntry(entry);
    refreshIcons();
  }

  function handleUseEdition(btn) {
    const altCard = btn.closest('.book-card');
    const entry = btn.closest('.series-entry, .edition-group');
    if (!altCard || !entry) return;
    const main = entry.querySelector('.series-entry-main');
    const altsWrap = entry.querySelector('.series-alts');
    const current = main.querySelector('.book-card');
    if (current && altsWrap) altsWrap.insertBefore(current, altsWrap.firstChild);
    main.appendChild(altCard);
    decorateEntry(entry);
    // Actively picking an edition implies you want this book in the batch.
    const inc = entry.querySelector('.entry-include');
    if (inc && !inc.checked) { inc.checked = true; updateSeriesCount(entry.closest('.series-group')); }
    refreshIcons();
  }

  function setSeriesSending(btn, sending) {
    if (sending) {
      btn.disabled = true;
      btn.dataset.originalHtml = btn.innerHTML;
      btn.innerHTML = '<span class="spinner-icon" aria-hidden="true"></span> Sending…';
    } else if (btn.dataset.originalHtml) {
      btn.innerHTML = btn.dataset.originalHtml;
      delete btn.dataset.originalHtml;
      refreshIcons();
    }
  }

  async function handleSeriesSend(btn) {
    const group = btn.closest('.series-group');
    if (!group) return;
    const label = group.dataset.label || '';

    const items = [];
    const cardByKey = new Map();
    group.querySelectorAll('.series-entry').forEach((entry) => {
      const inc = entry.querySelector('.entry-include');
      if (!inc || !inc.checked) return;
      const card = entry.querySelector('.series-entry-main .book-card');
      const dl = card && card.querySelector('[data-action="download"]');
      if (!dl || !dl.dataset.link || !dl.dataset.title) return;
      items.push({ link: dl.dataset.link, title: dl.dataset.title });
      cardByKey.set(dl.dataset.link + '\n' + dl.dataset.title, card);
    });
    if (items.length === 0) return;

    setSeriesSending(btn, true);
    try {
      const res = await fetch('/send/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items, batch_label: label }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.message || 'Batch send failed');

      (data.results || []).forEach((r) => {
        const card = cardByKey.get((r.link || '') + '\n' + (r.title || ''));
        if (!card) return;
        if (r.ok) {
          const dl = card.querySelector('[data-action="download"]');
          if (dl) {
            if (!dl.dataset.originalHtml) dl.dataset.originalHtml = dl.innerHTML;
            setButtonSuccess(dl);
          }
        } else {
          card.classList.add('series-send-failed');
        }
      });

      const sent = data.sent || 0;
      const total = data.total || items.length;
      if (sent === total) {
        showToast('Sent ' + sent + ' book' + (sent === 1 ? '' : 's') + ' to your server.', 'success');
      } else {
        showToast('Sent ' + sent + ' of ' + total + ' — ' + (total - sent) + ' failed.', sent ? 'info' : 'error');
      }
    } catch (err) {
      showToast(err.message || 'Batch send failed', 'error');
    } finally {
      setSeriesSending(btn, false);
    }
  }

  /* ----------------------------------------------------------
     Downloads: render + refresh
     ---------------------------------------------------------- */
  function stateBadgeClass(state) {
    const s = (state || '').toLowerCase();
    if (['downloading', 'active', 'downloading metadata', 'checking'].some((k) => s.includes(k))) return 'badge-info';
    if (['seeding', 'uploading', 'complete', 'completed', 'finished', 'seed_pending'].some((k) => s.includes(k))) return 'badge-success';
    if (['error', 'missing', 'failed'].some((k) => s.includes(k))) return 'badge-error';
    return 'badge-neutral';
  }

  function formatState(state) {
    if (!state) return 'Unknown';
    return String(state).replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function renderDownloadCard(torrent) {
    const progress = Math.round(Math.min(100, Math.max(0, Number(torrent.progress) || 0)));
    const badgeClass = stateBadgeClass(torrent.state);
    const name = escapeHtml(torrent.name || 'Unknown');
    const size = escapeHtml(torrent.size || '');
    const state = escapeHtml(formatState(torrent.state));

    return (
      '<div class="download-card" data-name="' + name + '">' +
        '<div class="download-card-header">' +
          '<span class="download-name" title="' + name + '">' + name + '</span>' +
          '<span class="download-size">' + size + '</span>' +
        '</div>' +
        '<div class="progress-row">' +
          '<div class="progress-bar" role="progressbar" aria-valuenow="' + progress + '" aria-valuemin="0" aria-valuemax="100">' +
            '<div class="progress-bar-fill" style="width: ' + progress + '%"></div>' +
          '</div>' +
          '<span class="progress-percent">' + progress + '%</span>' +
        '</div>' +
        '<div class="download-card-footer">' +
          '<span class="badge ' + badgeClass + '">' + state + '</span>' +
        '</div>' +
      '</div>'
    );
  }

  let refreshInFlight = false;

  async function refreshDownloads(opts) {
    const list = document.getElementById('downloads-list');
    const countEl = document.getElementById('downloads-count');
    const updatedEl = document.getElementById('downloads-updated');
    if (!list || refreshInFlight) return;

    const btn = document.querySelector('[data-action="refresh-downloads"]');
    const manual = opts && opts.manual;
    refreshInFlight = true;
    if (manual && btn) btn.disabled = true;

    try {
      const res = await fetch('/api/status');
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.message || 'Failed to load downloads');

      const torrents = data.torrents || [];
      if (torrents.length === 0) {
        list.innerHTML =
          '<div class="empty-state">' +
          '<i data-lucide="inbox" class="empty-state-icon" aria-hidden="true"></i>' +
          '<p class="empty-state-title">No active downloads</p>' +
          '<p class="empty-state-subtitle">Downloads you send will appear here.</p>' +
          '</div>';
      } else {
        list.innerHTML = torrents.map(renderDownloadCard).join('');
      }

      if (countEl) {
        countEl.textContent = torrents.length === 1 ? '1 active download' : torrents.length + ' active downloads';
      }
      if (updatedEl) {
        updatedEl.textContent = 'Updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      }
      refreshIcons();
    } catch (err) {
      if (manual) showToast(err.message || "Couldn't refresh downloads", 'error');
    } finally {
      refreshInFlight = false;
      if (manual && btn) btn.disabled = false;
    }
  }

  /* ----------------------------------------------------------
     Connection controls: Tor routing toggle + circuit renew
     ---------------------------------------------------------- */
  function closeConnPopover() {
    const pop = document.getElementById('conn-popover');
    const btn = document.querySelector('[data-action="conn-toggle"]');
    if (pop) pop.hidden = true;
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }

  function toggleConnPopover(btn) {
    const pop = document.getElementById('conn-popover');
    if (!pop) return;
    const opening = pop.hidden;
    pop.hidden = !opening;
    btn.setAttribute('aria-expanded', opening ? 'true' : 'false');
    if (opening) refreshIcons();
  }

  async function handleRouteToggle(checkbox) {
    const mode = checkbox.checked ? 'tor' : 'direct';
    try {
      const res = await fetch('/settings/route', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.message || 'Could not change routing');

      const label = document.querySelector('.conn-mode-label');
      if (label) label.textContent = mode === 'tor' ? 'Tor' : 'Direct';
      const warning = document.querySelector('.conn-direct-warning');
      if (warning) warning.hidden = mode === 'tor';
      const renew = document.querySelector('.conn-renew-btn');
      if (renew) renew.disabled = mode !== 'tor';
      // Switching to Direct unblocks a search that was waiting on Tor.
      if (mode === 'direct') clearTorBooting();
      showToast(mode === 'tor' ? 'Routing AudioBook Bay via Tor.' : 'Routing AudioBook Bay directly.', 'success');
    } catch (err) {
      checkbox.checked = !checkbox.checked; // revert the visual toggle
      showToast(err.message || 'Could not change routing', 'error');
    }
  }

  let renewCooldown = false;
  async function handleRenew(btn) {
    if (renewCooldown) return;
    renewCooldown = true;
    btn.disabled = true;
    const original = btn.innerHTML;
    btn.innerHTML = '<span class="spinner-icon" aria-hidden="true"></span> Renewing…';
    try {
      const res = await fetch('/tor/renew', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.message || 'Could not renew circuit');
      showToast(data.message || 'Requested a new Tor circuit.', 'success');
    } catch (err) {
      showToast(err.message || 'Could not renew circuit', 'error');
    } finally {
      btn.innerHTML = original;
      refreshIcons();
      // Tor rate-limits NEWNYM (~10s) -- keep the button disabled until then.
      setTimeout(() => {
        renewCooldown = false;
        const checkbox = document.getElementById('conn-route-tor');
        btn.disabled = !(checkbox && checkbox.checked);
      }, 10000);
    }
  }

  /* ----------------------------------------------------------
     Tor boot gating: when this browser defaults to Tor and Tor is
     still bootstrapping, the search page waits (polling) and offers
     a one-click switch to Direct, then enables itself when ready.
     ---------------------------------------------------------- */
  function clearTorBooting() {
    if (torPollTimer) { clearTimeout(torPollTimer); torPollTimer = null; }
    const notice = document.getElementById('tor-booting');
    if (notice) notice.remove();
    const submit = document.getElementById('search-submit');
    if (submit) submit.disabled = false;
    const label = document.querySelector('.conn-mode-label');
    if (label && /starting/i.test(label.textContent)) label.textContent = 'Tor';
  }

  let torPollTimer = null;
  function pollConnection() {
    fetch('/api/connection')
      .then((r) => r.json())
      .then((d) => {
        if (d.tor_status === 'ready' || d.route_mode === 'direct') {
          clearTorBooting();
          if (d.tor_status === 'ready') showToast('Tor is ready — search away.', 'success');
        } else {
          torPollTimer = setTimeout(pollConnection, 2500);
        }
      })
      .catch(() => { torPollTimer = setTimeout(pollConnection, 4000); });
  }

  async function handleSearchDirect(btn) {
    btn.disabled = true;
    try {
      const res = await fetch('/settings/route', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'direct' }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.message || 'Could not switch to Direct');
      const sw = document.getElementById('conn-route-tor');
      if (sw) sw.checked = false;
      const warning = document.querySelector('.conn-direct-warning');
      if (warning) warning.hidden = false;
      const renew = document.querySelector('.conn-renew-btn');
      if (renew) renew.disabled = true;
      clearTorBooting();
      showToast('Searching directly. Your server IP is visible to AudioBook Bay.', 'info');
    } catch (err) {
      btn.disabled = false;
      showToast(err.message || 'Could not switch to Direct', 'error');
    }
  }

  /* ----------------------------------------------------------
     Wiring
     ---------------------------------------------------------- */
  document.addEventListener('click', (e) => {
    const dl = e.target.closest('[data-action="download"]');
    if (dl) { handleDownload(dl); return; }

    const refresh = e.target.closest('[data-action="refresh-downloads"]');
    if (refresh) { refreshDownloads({ manual: true }); return; }

    const smartSort = e.target.closest('[data-action="smart-sort"]');
    if (smartSort) { handleSmartSort(smartSort); return; }

    const toggleFiltered = e.target.closest('[data-action="toggle-filtered"]');
    if (toggleFiltered) { handleToggleFiltered(); return; }

    const seriesSend = e.target.closest('[data-action="series-send"]');
    if (seriesSend) { handleSeriesSend(seriesSend); return; }

    const useEdition = e.target.closest('[data-action="series-use-edition"]');
    if (useEdition) { handleUseEdition(useEdition); return; }

    const altsToggle = e.target.closest('[data-action="series-alts-toggle"]');
    if (altsToggle) { handleAltsToggle(altsToggle); return; }

    const interpChip = e.target.closest('#smart-sort-ambiguity .chip');
    if (interpChip) { handleInterpChip(interpChip); return; }

    const connToggle = e.target.closest('[data-action="conn-toggle"]');
    if (connToggle) { toggleConnPopover(connToggle); return; }

    const connRenew = e.target.closest('[data-action="conn-renew"]');
    if (connRenew) { handleRenew(connRenew); return; }

    const searchDirect = e.target.closest('[data-action="search-direct"]');
    if (searchDirect) { handleSearchDirect(searchDirect); return; }

    // A click anywhere outside the connection control closes its popover.
    if (!e.target.closest('.conn-control')) closeConnPopover();
  });

  document.addEventListener('change', (e) => {
    const route = e.target.closest('[data-action="conn-route"]');
    if (route) { handleRouteToggle(route); return; }

    const ownedToggle = e.target.closest('[data-action="toggle-owned"]');
    if (ownedToggle) {
      const results = document.getElementById('search-results');
      if (results) results.classList.toggle('hide-owned', ownedToggle.checked);
      return;
    }

    const selectAll = e.target.closest('.series-select-all');
    if (selectAll) {
      const group = selectAll.closest('.series-group');
      if (group) {
        group.querySelectorAll('.series-entry:not(.is-collection):not(.is-gap) .entry-include')
          .forEach((c) => { if (!c.disabled) c.checked = selectAll.checked; });
        updateSeriesCount(group);
      }
      return;
    }

    const include = e.target.closest('.entry-include');
    if (include) {
      const group = include.closest('.series-group');
      if (include.closest('.series-entry.is-collection')) syncCollections(group);
      else updateSeriesCount(group);
      return;
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeConnPopover();
  });

  document.addEventListener('submit', (e) => {
    const form = e.target.closest('#search-form');
    if (!form) return;
    e.preventDefault();
    handleSearch(form);
  });

  function init() {
    refreshIcons();
    setSearchLoading(false);

    const downloadsList = document.getElementById('downloads-list');
    if (downloadsList) {
      refreshDownloads();
      setInterval(refreshDownloads, 10000);
    }

    // If the page rendered with Tor still bootstrapping, keep search gated and
    // poll until routing is usable (or the user switches to Direct).
    if (document.getElementById('tor-booting')) {
      const submit = document.getElementById('search-submit');
      if (submit) submit.disabled = true;
      pollConnection();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // bfcache safety: if the page is restored from history (back/forward),
  // DOMContentLoaded won't fire again — make sure no loading state lingers.
  window.addEventListener('pageshow', (e) => {
    if (e.persisted) setSearchLoading(false);
  });

  window.showToast = showToast;
})();
