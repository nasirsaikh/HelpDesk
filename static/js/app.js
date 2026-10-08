(() => {
  const company = document.body.dataset.company;
  try {
    const theme = localStorage.getItem('helpdesk-theme');
    if (theme === 'dark') document.documentElement.dataset.theme = 'dark';
  } catch (_) {}
  document.querySelector('[data-theme-toggle]')?.addEventListener('click', () => {
    const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('helpdesk-theme', theme); } catch (_) {}
  });
  const sidebar = document.querySelector('#sidebar');
  const app = document.querySelector('.app-shell');
  const menuButton = document.querySelector('[data-toggle-sidebar]');
  const mobile = window.matchMedia('(max-width: 800px)');
  const syncNavigation = () => {
    const open = mobile.matches && document.body.classList.contains('sidebar-open');
    if (sidebar) sidebar.inert = mobile.matches && !open;
    if (app) app.inert = open;
    menuButton?.setAttribute('aria-expanded', String(open));
  };
  const closeNavigation = () => {
    document.body.classList.remove('sidebar-open');
    syncNavigation();
    menuButton?.focus();
  };
  menuButton?.addEventListener('click', () => {
    document.body.classList.add('sidebar-open');
    syncNavigation();
    document.querySelector('[data-close-sidebar]')?.focus();
  });
  document.querySelector('[data-close-sidebar]')?.addEventListener('click', closeNavigation);
  mobile.addEventListener('change', syncNavigation);
  syncNavigation();
  document.querySelector('[data-company-switch]')?.addEventListener('change', (event) => {
    // A normal navigation discards all previous-company fragments and client-side state.
    event.target.form.requestSubmit();
  });
  document.addEventListener('htmx:configRequest', (event) => {
    event.detail.headers['X-Company-Context'] = company;
    const csrf = document.querySelector('[name=csrfmiddlewaretoken]')?.value;
    if (csrf) event.detail.headers['X-CSRFToken'] = csrf;
  });
  document.addEventListener('htmx:beforeSwap', (event) => {
    const incoming = event.detail.xhr.getResponseHeader('X-Company-Context');
    if (incoming && incoming !== company) {
      event.detail.shouldSwap = false;
      window.location.reload();
    }
  });
  // Browser back/forward cache must not resurrect an old company's records.
  window.addEventListener('pageshow', (event) => { if (event.persisted) window.location.reload(); });
  document.querySelectorAll('[data-fullscreen-target]').forEach(button => button.addEventListener('click', () => {
    const target = document.getElementById(button.dataset.fullscreenTarget);
    target?.classList.add('fullscreen-panel');
    target?.setAttribute('role', 'dialog');
    target?.setAttribute('aria-modal', 'true');
    target?.setAttribute('aria-label', 'Policy members');
    document.body.classList.add('modal-open');
    target?.querySelector('[data-exit-fullscreen]')?.focus();
  }));
  const closeFullscreen = () => {
    if (!document.querySelector('.fullscreen-panel')) return;
    document.querySelectorAll('.fullscreen-panel').forEach(panel => {
      panel.classList.remove('fullscreen-panel'); panel.removeAttribute('role'); panel.removeAttribute('aria-modal');
    });
    document.body.classList.remove('modal-open');
    document.querySelector('[data-fullscreen-target]')?.focus();
  };
  document.querySelectorAll('[data-exit-fullscreen]').forEach(button => button.addEventListener('click', closeFullscreen));
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    closeFullscreen();
    if (document.body.classList.contains('sidebar-open')) closeNavigation();
  });
  document.addEventListener('keydown', event => {
    const modal = document.querySelector('.fullscreen-panel') || (mobile.matches && document.body.classList.contains('sidebar-open') ? sidebar : null);
    if (!modal || event.key !== 'Tab') return;
    const nodes = [...modal.querySelectorAll('a, button, input, select, textarea')].filter(node => node.offsetParent !== null);
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
})();
