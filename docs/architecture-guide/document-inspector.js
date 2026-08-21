(() => {
  const INDEX_PATH = 'docs/architecture-guide/index.html';
  const SOURCE_SUFFIXES = ['.css', '.json', '.md', '.py', '.toml', '.ts', '.tsx', '.yaml', '.yml'];

  const requiredElement = (root, selector) => {
    const element = root.querySelector(selector);
    if (!element) throw new Error(`DocumentInspector requires ${selector}`);
    return element;
  };

  const normalizePath = (basePath, target) => {
    if (!target || /^(?:[a-z]+:)?\/\//i.test(target) || target.startsWith('mailto:')) return null;
    try {
      const resolved = new URL(target.split('#', 1)[0], `https://atlas.local/${basePath}`);
      if (resolved.origin !== 'https://atlas.local') return null;
      return decodeURIComponent(resolved.pathname.replace(/^\//, ''));
    } catch {
      return null;
    }
  };

  const formatBytes = (bytes) => {
    if (bytes < 1024) return `${bytes} B`;
    return `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`;
  };

  const pathCandidates = (manifest, reference) => {
    const cleaned = reference
      .trim()
      .replace(/^['"`]|['"`]$/g, '')
      .replace(/:(\d+)(?:-\d+)?$/, '');
    if (!SOURCE_SUFFIXES.some((suffix) => cleaned.endsWith(suffix))) return [];
    const exact = cleaned.replace(/^\.\//, '');
    if (manifest.entries[exact]) return [exact];
    return Object.keys(manifest.entries).filter((path) => path === exact || path.endsWith(`/${exact}`));
  };

  function create({ root, manifest }) {
    if (!root || !manifest || manifest.schemaVersion !== 1) {
      throw new Error('DocumentInspector requires a schema v1 content manifest.');
    }

    const title = requiredElement(root, '#inspector-title');
    const pathLabel = requiredElement(root, '#inspector-path');
    const kindLabel = requiredElement(root, '#inspector-kind');
    const content = requiredElement(root, '#inspector-content');
    const status = requiredElement(root, '#inspector-status');
    const evidence = requiredElement(root, '#inspector-evidence');
    const original = requiredElement(root, '#inspector-original');
    const closeButton = requiredElement(root, '#inspector-close');
    const backButton = requiredElement(root, '#inspector-back');
    const renderedButton = requiredElement(root, '#inspector-rendered');
    const rawButton = requiredElement(root, '#inspector-raw');
    const findInput = requiredElement(root, '#inspector-find');
    const findCount = requiredElement(root, '#inspector-find-count');
    const mobileReader = window.matchMedia('(max-width: 760px)');
    const backgroundRegions = [...document.querySelectorAll('body > .topbar, body > .shell')];

    const state = {
      currentPath: null,
      currentView: 'rendered',
      history: [],
      matches: [],
      matchIndex: -1,
      returnFocus: null,
    };

    const entryFor = (path) => manifest.entries[path] || null;

    const syncModalState = () => {
      const modal = root.classList.contains('is-open') && mobileReader.matches;
      if (modal) {
        root.setAttribute('role', 'dialog');
        root.setAttribute('aria-modal', 'true');
      } else {
        root.removeAttribute('role');
        root.removeAttribute('aria-modal');
      }
      backgroundRegions.forEach((region) => {
        region.inert = modal;
      });
    };

    const setQueryPath = (path) => {
      const url = new URL(window.location.href);
      if (path) url.searchParams.set('inspect', path);
      else url.searchParams.delete('inspect');
      window.history.replaceState(window.history.state, '', url);
    };

    const resolveDocumentLink = (anchor) => {
      const explicit = anchor.dataset.inspectPath;
      if (explicit) return entryFor(explicit) ? explicit : null;
      const href = anchor.getAttribute('href');
      if (!href || href.startsWith('#')) return null;
      const insideReader = root.contains(anchor);
      const basePath = insideReader && state.currentPath ? state.currentPath : INDEX_PATH;
      const resolved = normalizePath(basePath, href);
      return resolved && entryFor(resolved) ? resolved : null;
    };

    const updateTabs = () => {
      const entry = entryFor(state.currentPath);
      const isMarkdown = entry?.kind === 'markdown';
      renderedButton.hidden = !isMarkdown;
      const renderedSelected = isMarkdown && state.currentView === 'rendered';
      const rawSelected = !isMarkdown || state.currentView === 'raw';
      renderedButton.setAttribute('aria-selected', String(renderedSelected));
      renderedButton.tabIndex = renderedSelected ? 0 : -1;
      rawButton.textContent = isMarkdown ? '原文' : '源码';
      rawButton.setAttribute('aria-selected', String(rawSelected));
      rawButton.tabIndex = rawSelected ? 0 : -1;
      content.setAttribute('aria-labelledby', renderedSelected ? renderedButton.id : rawButton.id);
    };

    const renderCode = (entry) => {
      const source = document.createElement('div');
      source.className = 'source-view';
      source.dataset.language = entry.language;
      const fragment = document.createDocumentFragment();
      entry.content.split('\n').forEach((line, index) => {
        const row = document.createElement('div');
        row.className = 'source-line';
        row.id = `source-L${index + 1}`;
        const number = document.createElement('a');
        number.className = 'source-line-number';
        number.href = `#source-L${index + 1}`;
        number.textContent = String(index + 1);
        number.setAttribute('aria-label', `第 ${index + 1} 行`);
        const code = document.createElement('code');
        code.textContent = line || ' ';
        row.append(number, code);
        fragment.append(row);
      });
      source.append(fragment);
      content.replaceChildren(source);
    };

    const decorateMarkdown = (entry) => {
      content.querySelectorAll('code:not(pre code)').forEach((code) => {
        if (code.closest('a, button')) return;
        const candidates = pathCandidates(manifest, code.textContent || '');
        if (candidates.length !== 1) return;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'inline-source-link';
        button.dataset.inspectPath = candidates[0];
        button.textContent = code.textContent;
        button.title = `在 Trace Reader 中预览 ${candidates[0]}`;
        code.replaceWith(button);
      });
      content.querySelectorAll('a').forEach((anchor) => {
        const href = anchor.getAttribute('href') || '';
        if (href.startsWith('#')) return;
        const resolved = normalizePath(entry.path, href);
        if (resolved && entryFor(resolved)) {
          anchor.dataset.inspectPath = resolved;
          anchor.classList.add('reader-link');
        } else if (/^https?:/i.test(href)) {
          anchor.target = '_blank';
          anchor.rel = 'noopener';
        }
      });
    };

    const renderCurrent = () => {
      const entry = entryFor(state.currentPath);
      if (!entry) return;
      content.className = `inspector-content view-${state.currentView}`;
      if (entry.kind === 'markdown' && state.currentView === 'rendered') {
        const article = document.createElement('article');
        article.className = 'markdown-view';
        article.innerHTML = entry.html;
        content.replaceChildren(article);
        decorateMarkdown(entry);
      } else {
        renderCode(entry);
      }
      updateTabs();
      updateFind(true);
      content.scrollTop = 0;
    };

    const setView = (view) => {
      const entry = entryFor(state.currentPath);
      if (!entry) return;
      state.currentView = entry.kind === 'markdown' && view === 'rendered' ? 'rendered' : 'raw';
      renderCurrent();
    };

    const updateFind = (resetIndex = false) => {
      content.querySelectorAll('.find-current').forEach((element) => element.classList.remove('find-current'));
      const query = findInput.value.toLocaleLowerCase().trim();
      const selector = state.currentView === 'rendered'
        ? '.markdown-view :is(h1,h2,h3,h4,p,li,td,th,blockquote)'
        : '.source-line';
      state.matches = query
        ? [...content.querySelectorAll(selector)].filter((element) =>
          element.textContent.toLocaleLowerCase().includes(query))
        : [];
      if (resetIndex) state.matchIndex = -1;
      findCount.value = state.matches.length ? `${Math.max(state.matchIndex + 1, 0)}/${state.matches.length}` : '0';
    };

    const findNext = () => {
      updateFind(false);
      if (!state.matches.length) return;
      state.matches.forEach((element) => element.classList.remove('find-current'));
      state.matchIndex = (state.matchIndex + 1) % state.matches.length;
      const match = state.matches[state.matchIndex];
      match.classList.add('find-current');
      match.scrollIntoView({ block: 'center', behavior: 'smooth' });
      findCount.value = `${state.matchIndex + 1}/${state.matches.length}`;
    };

    const open = (path, { remember = true, focus = true } = {}) => {
      const entry = entryFor(path);
      if (!entry) return false;
      if (remember && state.currentPath && state.currentPath !== path) state.history.push(state.currentPath);
      if (!root.classList.contains('is-open')) state.returnFocus = document.activeElement;
      state.currentPath = path;
      state.currentView = entry.kind === 'markdown' ? 'rendered' : 'raw';
      state.matches = [];
      state.matchIndex = -1;
      findInput.value = '';
      title.textContent = entry.title;
      pathLabel.textContent = entry.path;
      kindLabel.textContent = entry.kind === 'markdown' ? 'MARKDOWN · RENDERED' : `${entry.language.toUpperCase()} · SOURCE`;
      evidence.innerHTML = `<span>${entry.lines} lines</span><span>${formatBytes(entry.bytes)}</span><code>sha256:${entry.sha256.slice(0, 12)}</code>`;
      status.textContent = `SNAPSHOT VERIFIED · ${manifest.entryCount} files · atlas ${manifest.digest.slice(0, 12)}`;
      original.href = entry.sourceHref;
      original.dataset.inspectorBypass = 'true';
      backButton.disabled = state.history.length === 0;
      root.classList.add('is-open');
      root.setAttribute('aria-hidden', 'false');
      document.body.classList.add('inspector-open');
      syncModalState();
      setQueryPath(path);
      renderCurrent();
      if (focus) content.focus({ preventScroll: true });
      return true;
    };

    const close = ({ restoreFocus = true } = {}) => {
      root.classList.remove('is-open');
      root.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('inspector-open');
      syncModalState();
      setQueryPath(null);
      if (restoreFocus && state.returnFocus instanceof HTMLElement) state.returnFocus.focus();
    };

    const back = () => {
      const previous = state.history.pop();
      if (!previous) return;
      open(previous, { remember: false });
      backButton.disabled = state.history.length === 0;
    };

    document.addEventListener('click', (event) => {
      const anchor = event.target.closest('a, button[data-inspect-path]');
      if (!anchor || anchor.dataset.inspectorBypass === 'true') return;
      if (anchor instanceof HTMLAnchorElement && (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)) return;
      if (root.contains(anchor) && anchor instanceof HTMLAnchorElement) {
        const href = anchor.getAttribute('href') || '';
        if (href.startsWith('#') && !href.startsWith('#source-L')) {
          const target = content.querySelector(href);
          if (target) {
            event.preventDefault();
            target.scrollIntoView({ block: 'start' });
          }
          return;
        }
      }
      const path = anchor.dataset.inspectPath || resolveDocumentLink(anchor);
      if (!path || !entryFor(path)) return;
      event.preventDefault();
      open(path);
    });

    closeButton.addEventListener('click', () => close());
    backButton.addEventListener('click', back);
    renderedButton.addEventListener('click', () => setView('rendered'));
    rawButton.addEventListener('click', () => setView('raw'));
    [renderedButton, rawButton].forEach((button) => {
      button.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        const tabs = [renderedButton, rawButton].filter((tab) => !tab.hidden);
        if (tabs.length < 2) return;
        event.preventDefault();
        const current = tabs.indexOf(button);
        const next = event.key === 'Home'
          ? tabs[0]
          : event.key === 'End'
            ? tabs.at(-1)
            : tabs[(current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
        next.focus();
        setView(next.dataset.view);
      });
    });
    findInput.addEventListener('input', () => updateFind(true));
    findInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        findNext();
      }
    });
    document.addEventListener('keydown', (event) => {
      if (!root.classList.contains('is-open')) return;
      if (event.key === 'Tab' && mobileReader.matches) {
        const focusable = [...root.querySelectorAll('a[href],button:not([disabled]),input,[tabindex]:not([tabindex="-1"])')]
          .filter((element) => !element.hidden && element.getClientRects().length > 0);
        const first = focusable[0];
        const last = focusable.at(-1);
        if (focusable.length && event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (focusable.length && !event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      } else if (event.key === 'Escape') {
        event.preventDefault();
        close();
      } else if (event.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)) {
        event.preventDefault();
        findInput.focus();
      }
    });
    mobileReader.addEventListener('change', syncModalState);

    const initialPath = new URLSearchParams(window.location.search).get('inspect');
    if (initialPath && entryFor(initialPath)) open(initialPath, { remember: false, focus: false });

    document.querySelectorAll('a[href]').forEach((anchor) => {
      const path = resolveDocumentLink(anchor);
      if (!path) return;
      const entry = entryFor(path);
      anchor.classList.add('atlas-preview-link');
      anchor.dataset.previewKind = entry.kind === 'markdown' ? 'MD' : entry.language.toUpperCase();
      anchor.title = `在 Trace Reader 中预览 ${entry.path}`;
    });

    return Object.freeze({ open, close });
  }

  window.LumenDocumentInspector = Object.freeze({ create });
})();
