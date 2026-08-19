/* Form draft autosave — survives a refresh or a failed submit for a few
 * minutes so typed-in data isn't lost.
 *
 * Uses localStorage (not real cookies) so nothing is sent to the server on
 * unrelated requests, and each entry carries its own save timestamp so it
 * expires on read without needing a server round-trip. Password fields are
 * never persisted, even in memory beyond the DOM, for obvious reasons.
 *
 * Opt out of a specific form with data-no-draft="1" on the <form> tag.
 */
(function () {
  var TTL_MS = 5 * 60 * 1000; // 5 minutes
  var PREFIX = 'medtrack:draft:';
  var SKIP_TYPES = { password: 1, file: 1, submit: 1, button: 1, reset: 1 };

  function draftKey(form) {
    var id = form.getAttribute('data-draft-id') || form.getAttribute('action') || form.id || 'form';
    return PREFIX + location.pathname + ':' + id;
  }

  function isPersistable(field) {
    if (!field.name) return false;
    var type = (field.type || '').toLowerCase();
    return !SKIP_TYPES[type];
  }

  function fields(form) {
    return Array.prototype.filter.call(form.elements, isPersistable);
  }

  function saveDraft(form) {
    var data = {};
    fields(form).forEach(function (field) {
      if (field.type === 'checkbox' || field.type === 'radio') {
        if (field.checked) data[field.name] = field.value;
      } else {
        data[field.name] = field.value;
      }
    });
    try {
      localStorage.setItem(draftKey(form), JSON.stringify({ t: Date.now(), data: data }));
    } catch (e) { /* storage full/unavailable — draft just won't persist */ }
  }

  function readDraft(form) {
    var raw;
    try {
      raw = localStorage.getItem(draftKey(form));
    } catch (e) { return null; }
    if (!raw) return null;
    var parsed;
    try { parsed = JSON.parse(raw); } catch (e) { parsed = null; }
    if (!parsed || Date.now() - parsed.t > TTL_MS) {
      try { localStorage.removeItem(draftKey(form)); } catch (e) {}
      return null;
    }
    return parsed.data;
  }

  function restoreDraft(form) {
    var data = readDraft(form);
    if (!data) return;
    fields(form).forEach(function (field) {
      if (!(field.name in data)) return;
      if (field.type === 'checkbox' || field.type === 'radio') {
        field.checked = field.value === data[field.name];
      } else {
        field.value = data[field.name];
      }
      field.dispatchEvent(new Event('input', { bubbles: true }));
    });
    form.dispatchEvent(new CustomEvent('formdraft:restored', { detail: data }));
  }

  function attach(form) {
    if (form.hasAttribute('data-no-draft')) return;
    if ((form.method || 'get').toLowerCase() !== 'post') return;
    restoreDraft(form);
    var timer = null;
    form.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () { saveDraft(form); }, 300);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    Array.prototype.forEach.call(document.querySelectorAll('form'), attach);
  });
})();
