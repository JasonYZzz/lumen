(() => {
  const search = document.querySelector('#search');
  const sections = [...document.querySelectorAll('.searchable')];
  const noResults = document.querySelector('#no-results');
  const navLinks = [...document.querySelectorAll('#toc a')];

  const normalize = (value) => value.toLocaleLowerCase().replace(/\s+/g, ' ').trim();

  function applySearch() {
    const query = normalize(search.value);
    let visible = 0;
    sections.forEach((section) => {
      const haystack = normalize(`${section.dataset.title || ''} ${section.textContent}`);
      const match = !query || haystack.includes(query);
      section.classList.toggle('search-hidden', !match);
      if (match) visible += 1;
    });
    noResults.hidden = visible !== 0;
    search.setAttribute('aria-invalid', visible === 0 ? 'true' : 'false');
  }

  search.addEventListener('input', applySearch);
  document.addEventListener('keydown', (event) => {
    if (event.key === '/' && document.activeElement !== search && !document.body.classList.contains('inspector-open')) {
      event.preventDefault();
      search.focus();
    }
    if (event.key === 'Escape' && document.activeElement === search) {
      search.value = '';
      applySearch();
      search.blur();
    }
  });

  const observer = new IntersectionObserver((entries) => {
    const visible = entries
      .filter((entry) => entry.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible || !visible.target.id) return;
    navLinks.forEach((link) => {
      link.classList.toggle('active', link.hash === `#${visible.target.id}`);
      if (link.hash === `#${visible.target.id}`) link.classList.add('visited');
    });
  }, { rootMargin: '-18% 0px -68% 0px', threshold: [0, 0.15, 0.4] });

  navLinks.forEach((link) => {
    const target = document.querySelector(link.hash);
    if (target) observer.observe(target);
    link.addEventListener('click', () => link.classList.add('visited'));
  });

  const inspectorRoot = document.querySelector('#document-inspector');
  const manifest = window.__LUMEN_ATLAS_CONTENT__;
  if (inspectorRoot && manifest && window.LumenDocumentInspector) {
    window.LumenDocumentInspector.create({ root: inspectorRoot, manifest });
  }
})();
