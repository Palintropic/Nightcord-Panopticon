// Applies the viewer's theme choice (light / dark / system) before the first paint, so it is
// loaded synchronously from <head> on every page. The choice is per browser (localStorage);
// "system" leaves data-theme unset and uwp.css follows prefers-color-scheme.
(function(){
  var KEY = 'panopticon.theme';
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

  // Kept separately from storage so a choice still holds for this page when storage is blocked.
  var current = stored();
  apply(current);

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
  };

  // Another tab changed the choice.
  window.addEventListener('storage', function(e){
    if(e.key === KEY || e.key === null){ current = stored(); apply(current); }
  });
})();
