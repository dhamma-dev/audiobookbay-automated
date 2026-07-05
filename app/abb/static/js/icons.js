/* Local icon renderer — replaces the unpkg lucide script (v2: no third-party
   code runs in the browser). Swaps every <i data-lucide="name"> for an inline
   SVG referencing the vendored sprite (static/images/icons.svg), producing
   the same markup shape lucide.createIcons() did so all existing CSS applies.
   Exposes window.lucide.createIcons for app.js's refreshIcons(). */
(function () {
  'use strict';

  var SPRITE = document.currentScript
    ? document.currentScript.dataset.sprite
    : '/static/images/icons.svg';
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var XLINK_NS = 'http://www.w3.org/1999/xlink';

  function createIcons() {
    var nodes = document.querySelectorAll('i[data-lucide]');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var name = el.getAttribute('data-lucide');
      if (!name) continue;

      var svg = document.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('class', ('lucide lucide-' + name + ' ' + (el.getAttribute('class') || '')).trim());
      svg.setAttribute('width', '24');
      svg.setAttribute('height', '24');
      svg.setAttribute('viewBox', '0 0 24 24');
      svg.setAttribute('fill', 'none');
      svg.setAttribute('stroke', 'currentColor');
      svg.setAttribute('stroke-width', '2');
      svg.setAttribute('stroke-linecap', 'round');
      svg.setAttribute('stroke-linejoin', 'round');
      // Carry over the non-class attributes (aria-hidden, title hooks, etc.).
      for (var a = 0; a < el.attributes.length; a++) {
        var attr = el.attributes[a];
        if (attr.name !== 'class' && attr.name !== 'data-lucide') {
          svg.setAttribute(attr.name, attr.value);
        }
      }

      var use = document.createElementNS(SVG_NS, 'use');
      use.setAttribute('href', SPRITE + '#' + name);
      use.setAttributeNS(XLINK_NS, 'xlink:href', SPRITE + '#' + name); // older Safari
      svg.appendChild(use);
      el.parentNode.replaceChild(svg, el);
    }
  }

  window.lucide = { createIcons: createIcons };
})();
