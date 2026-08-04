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
  }

  search.addEventListener('input', applySearch);
  document.addEventListener('keydown', (event) => {
    if (event.key === '/' && document.activeElement !== search) {
      event.preventDefault();
      search.focus();
    }
    if (event.key === 'Escape' && document.activeElement === search) {
      search.value = '';
      applySearch();
      search.blur();
    }
  });

  // Lightweight liquid-glass highlight: local pointer position controls the
  // refracted highlight without adding a framework or animation dependency.
  if (window.matchMedia('(pointer: fine) and (prefers-reduced-motion: no-preference)').matches) {
    let frame = 0;
    document.addEventListener('pointermove', (event) => {
      const glass = event.target.closest?.('.glass');
      if (!glass) return;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const rect = glass.getBoundingClientRect();
        glass.style.setProperty('--glass-x', `${((event.clientX - rect.left) / rect.width) * 100}%`);
        glass.style.setProperty('--glass-y', `${((event.clientY - rect.top) / rect.height) * 100}%`);
      });
    }, { passive: true });
  }

  const observer = new IntersectionObserver((entries) => {
    const visible = entries
      .filter((entry) => entry.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible || !visible.target.id) return;
    navLinks.forEach((link) => {
      link.classList.toggle('active', link.hash === `#${visible.target.id}`);
    });
  }, { rootMargin: '-18% 0px -68% 0px', threshold: [0, 0.15, 0.4] });

  navLinks.forEach((link) => {
    const target = document.querySelector(link.hash);
    if (target) observer.observe(target);
  });
})();
