// Theme (dark/light) shared by the drill and the reading page.
//
// Loaded synchronously in <head> so the attribute is on <html> before the first
// paint: deferring it would show a dark page flashing white for every reader who
// chose the light theme.  Kept as its own file because both pages ban inline script
// (audit rule security/script-unsafe-inline).
//
// Precedence: an explicit choice in localStorage, else the system preference.
window.NgheTheme = (() => {
  const KEY = 'vn.theme';           // 'dark' | 'light' | '' (follow the system)
  const root = document.documentElement;

  function stored(){
    try { return localStorage.getItem(KEY) || ''; } catch { return ''; }
  }
  function system(){
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
      ? 'light' : 'dark';
  }
  function current(){ return stored() || system(); }

  function apply(){
    const theme = current();
    root.setAttribute('data-theme', theme);
    // Form controls, scrollbars and the address bar follow this, not the attribute.
    root.style.colorScheme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'dark' ? '#101726' : '#f7f5f0');
    document.dispatchEvent(new CustomEvent('nghe:theme', {detail: {theme}}));
  }

  function set(theme){
    try { theme ? localStorage.setItem(KEY, theme) : localStorage.removeItem(KEY); } catch {}
    apply();
  }

  function toggle(){ set(current() === 'dark' ? 'light' : 'dark'); return current(); }

  apply();
  // Only follow the system while the reader has not made an explicit choice.
  if (window.matchMedia){
    const media = window.matchMedia('(prefers-color-scheme: light)');
    const onChange = () => { if (!stored()) apply(); };
    if (media.addEventListener) media.addEventListener('change', onChange);
    else if (media.addListener) media.addListener(onChange);
  }

  return {current, set, toggle, apply,
          // A widget calls this to label itself: what the click would switch *to*.
          label: () => (current() === 'dark' ? 'Nền sáng' : 'Nền tối')};
})();
