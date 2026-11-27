/* Cover fallbacks. The CSP (script-src 'self') blocks inline onerror
   handlers, which is what this replaces: a dead cover swaps to its
   data-fallback-src, and its blurred backdrop copy is dropped.

   Loaded (blocking, it's tiny) in <head> rather than in app.js on purpose:
   app.js is deferred, and a cover that fails fast would fire its one 'error'
   event before any listener existed. 'error' doesn't bubble, so this listens
   on the document in the capture phase — which also covers the cards that
   AJAX searches inject later. */
document.addEventListener('error', function (e) {
  'use strict';
  var img = e.target;
  if (!img || img.tagName !== 'IMG') return;
  if (img.classList.contains('book-cover-bg')) {
    img.remove();
  } else if (img.dataset.fallbackSrc && !img.dataset.fellBack) {
    img.dataset.fellBack = '1';  // if the fallback fails too, don't loop
    img.src = img.dataset.fallbackSrc;
  }
}, true);
