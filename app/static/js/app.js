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
      if (!res.ok) throw new Error('Search request failed (' + res.status + ')');

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

    const cards = new Map();
    results.querySelectorAll('.book-card').forEach((c) => cards.set(c.dataset.resultId, c));

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

    // Filtered-results toggle.
    const n = hidden.size;
    if (n === 0) {
      anchor.hidden = true;
    } else {
      anchor.hidden = false;
      anchor.textContent = (expanded ? 'Hide ' : 'Show ') + n + ' filtered result' + (n === 1 ? '' : 's');
    }
  }

  function applyRanking(query, ranking) {
    smartSortState = { query, ranking, expanded: false, activeInterp: null };
    renderAmbiguity(ranking);
    renderSmartSort();
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

    const interpChip = e.target.closest('#smart-sort-ambiguity .chip');
    if (interpChip) { handleInterpChip(interpChip); return; }
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
