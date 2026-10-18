// Applies the viewer's theme before the first paint, so it is loaded synchronously from <head> on
// every page.
// - Mode (light / dark / system) is per browser (localStorage); "system" leaves data-theme unset
//   and uwp.css follows prefers-color-scheme.
// - Scale (缩放) is per browser too: CSS zoom on <html>, so a big screen and a phone can differ.
//   Layout breakpoints are container queries (uwp.css), which follow the zoomed width.
// - Accents (curve, healthy tiles, status bar) are saved on the server (/api/appearance) and
//   cached here, so a reload paints the saved colours at once instead of flashing the default.
(function(){
  var KEY = 'panopticon.theme';
  var ACCENT_KEY = 'panopticon.accents';
  var SCALE_KEY = 'panopticon.scale';
  var SCALES = [80, 90, 100, 110, 125, 150, 175, 200];
  // Part -> [background variable, foreground variable or null].
  var ACCENT_VARS = {curve:['--CurveAccent', null], tile:['--TileAccent', '--TileForeground'], bar:['--BarAccent', '--BarForeground']};
  var root = document.documentElement;

  function stored(){
    try{
      var v = localStorage.getItem(KEY);
      return v === 'light' || v === 'dark' ? v : 'system';
    }catch(e){ return 'system'; }
  }
  function apply(v){
    if(v === 'light' || v === 'dark') root.setAttribute('data-theme', v);
    else root.removeAttribute('data-theme');
  }

  function storedScale(){
    try{
      var v = Number(localStorage.getItem(SCALE_KEY));
      return SCALES.indexOf(v) >= 0 ? v : 100;
    }catch(e){ return 100; }
  }
  function applyScale(v){
    if(!root.style) return;
    root.style.zoom = v === 100 ? '' : String(v / 100);
  }

  function validHex(v){ return typeof v === 'string' && /^#[0-9a-fA-F]{6}$/.test(v); }
  // White or black text, whichever contrasts more with the background (WCAG relative luminance).
  function foreground(hex){
    var l = [1, 3, 5].map(function(i){
      var c = parseInt(hex.substr(i, 2), 16) / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    var lum = 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2];
    return 1.05 / (lum + 0.05) >= (lum + 0.05) / 0.05 ? '#ffffff' : '#000000';
  }
  function cleanAccents(a){
    var out = {};
    Object.keys(ACCENT_VARS).forEach(function(k){ if(a && validHex(a[k])) out[k] = a[k].toLowerCase(); });
    return out;
  }
  function storedAccents(){
    try{ return cleanAccents(JSON.parse(localStorage.getItem(ACCENT_KEY) || '{}')); }
    catch(e){ return {}; }
  }
  // Unset parts fall back to the stylesheet's default (the accent).
  function applyAccents(a){
    if(!root.style) return;
    Object.keys(ACCENT_VARS).forEach(function(k){
      var bg = ACCENT_VARS[k][0], fg = ACCENT_VARS[k][1];
      if(a[k]){
        root.style.setProperty(bg, a[k]);
        if(fg) root.style.setProperty(fg, foreground(a[k]));
      } else {
        root.style.removeProperty(bg);
        if(fg) root.style.removeProperty(fg);
      }
    });
  }

  // Kept separately from storage so a choice still holds for this page when storage is blocked.
  var current = stored();
  var accents = storedAccents();
  var scale = storedScale();
  apply(current);
  applyAccents(accents);
  applyScale(scale);

  window.panopticonTheme = {
    get: function(){ return current; },
    set: function(v){
      current = v === 'light' || v === 'dark' ? v : 'system';
      try{
        if(current === 'system') localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, current);
      }catch(e){}
      apply(current);
    },
    getAccents: function(){ return Object.assign({}, accents); },
    setAccents: function(a){
      accents = cleanAccents(a);
      try{ localStorage.setItem(ACCENT_KEY, JSON.stringify(accents)); }catch(e){}
      applyAccents(accents);
    },
    foreground: foreground,
    SCALES: SCALES.slice(),
    getScale: function(){ return scale; },
    setScale: function(v){
      scale = SCALES.indexOf(v) >= 0 ? v : 100;
      try{
        if(scale === 100) localStorage.removeItem(SCALE_KEY);
        else localStorage.setItem(SCALE_KEY, String(scale));
      }catch(e){}
      applyScale(scale);
    },
  };

  // Another tab changed the choice.
  window.addEventListener('storage', function(e){
    if(e.key === KEY || e.key === null){ current = stored(); apply(current); }
    if(e.key === ACCENT_KEY || e.key === null){ accents = storedAccents(); applyAccents(accents); }
    if(e.key === SCALE_KEY || e.key === null){ scale = storedScale(); applyScale(scale); }
  });
})();
