"""JavaScript expressions used to read and exchange Web Reader tokens."""

import json


_TOKEN_SCAN_HELPERS = r"""
  function push(val, bucket) {
    if (typeof val === 'string' && val.length > 20 &&
        out[bucket].indexOf(val) < 0) {
      out[bucket].push(val);
    }
  }
  function tryParse(text) {
    try { return JSON.parse(text); } catch (e) { return null; }
  }
  function walk(value, depth, seen) {
    if (!value || depth > 6) return;
    if (typeof value === 'string') {
      if (/^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$/.test(value)) {
        push(value, 'refresh');
        return;
      }
      var topParsed = tryParse(value);
      if (topParsed && typeof topParsed === 'object') {
        walk(topParsed, depth + 1, seen);
      }
      return;
    }
    if (typeof value !== 'object' || seen.indexOf(value) >= 0) return;
    seen.push(value);
    for (var key in value) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) continue;
      var v = value[key];
      if (typeof v === 'string') {
        if (/refresh/i.test(key)) {
          var refreshValue = tryParse(v);
          if (refreshValue && typeof refreshValue === 'object') {
            walk(refreshValue, depth + 1, seen);
          } else {
            push(v, 'refresh');
          }
        }
        if (/hardware|device/i.test(key) &&
            /^[0-9a-fA-F-]{8,}$/.test(v)) push(v, 'hardware');
        if (/usertoken/i.test(key) && v.indexOf('U2FsdGVk') === 0)
            push(v, 'refresh');
        if (/userinfos/i.test(key) && v.indexOf('U2FsdGVk') === 0)
            push(v, 'hardware');
        var parsed = tryParse(v);
        if (parsed) walk(parsed, depth + 1, seen);
      } else if (v && typeof v === 'object') {
        walk(v, depth + 1, seen);
      }
    }
  }
"""


_GRAB_SNIPPET = r"""
(function () {
  var out = {refresh: [], hardware: [], idb: []};
__TOKEN_SCAN_HELPERS__
  function scanStore(store) {
    if (!store) return;
    try {
      for (var i = 0; i < store.length; i++) {
        var value = store.getItem(store.key(i));
        if (value) walk(value, 0, []);
      }
    } catch (e) {}
  }
  scanStore(localStorage);
  scanStore(sessionStorage);
  return new Promise(function (resolve) {
    var settled = false;
    function finish() {
      if (!settled) { settled = true; resolve(JSON.stringify(out)); }
    }
    try {
      if (window.indexedDB && indexedDB.databases) {
        indexedDB.databases().then(function (dbs) {
          for (var i = 0; i < dbs.length; i++) {
            if (dbs[i] && dbs[i].name) out.idb.push(String(dbs[i].name));
          }
          finish();
        }, finish);
        setTimeout(finish, 800);
        return;
      }
    } catch (e) {}
    finish();
  });
})()
""".replace("__TOKEN_SCAN_HELPERS__", _TOKEN_SCAN_HELPERS)


_IDB_SNIPPET = r"""
(function (dbname) {
  return new Promise(function (resolve) {
    var out = {refresh: [], hardware: []};
__TOKEN_SCAN_HELPERS__
    var settled = false;
    function finish() {
      if (!settled) { settled = true; resolve(JSON.stringify(out)); }
    }
    setTimeout(finish, 3000);
    try {
      var open = indexedDB.open(dbname);
      open.onsuccess = function (event) {
        var db = event.target.result;
        var names = [];
        for (var i = 0; i < (db.objectStoreNames || []).length; i++) {
          names.push(db.objectStoreNames[i]);
        }
        var remaining = names.length;
        if (!remaining) { db.close(); finish(); return; }
        names.forEach(function (storeName) {
          try {
            var tx = db.transaction(storeName, 'readonly');
            var req = tx.objectStore(storeName).getAll();
            req.onsuccess = function () {
              (req.result || []).forEach(function (entry) {
                walk(entry, 0, []);
              });
              remaining -= 1;
              if (remaining <= 0) { db.close(); finish(); }
            };
            req.onerror = function () {
              remaining -= 1;
              if (remaining <= 0) { db.close(); finish(); }
            };
          } catch (e) {
            remaining -= 1;
            if (remaining <= 0) { finish(); }
          }
        });
      };
      open.onerror = function () { finish(); };
    } catch (e) { finish(); }
  });
})(__DB_NAME__)
""".replace("__TOKEN_SCAN_HELPERS__", _TOKEN_SCAN_HELPERS)


_EXCHANGE_SNIPPET = r"""
(function (tokenUrl, body) {
  return fetch(tokenUrl, {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: body,
    credentials: 'omit',
    referrer: 'https://webreader.mytolino.com/'
  }).then(function (response) {
    return response.text().then(function (text) {
      return JSON.stringify({
        status: response.status,
        body: text.substring(0, 4096)
      });
    });
  }).catch(function (err) {
    return JSON.stringify({status: 0, body: 'fetch failed: ' + err});
  });
})(TOKEN_URL_PLACEHOLDER, BODY_PLACEHOLDER)
"""


def idb_expression(db_name):
    """Build a scan expression for one IndexedDB database."""
    return _IDB_SNIPPET.replace("__DB_NAME__", json.dumps(str(db_name)))
