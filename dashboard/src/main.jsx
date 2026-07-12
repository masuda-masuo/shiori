import './style.css';
import mermaid from 'mermaid';

const icons = {
  stats: `<svg viewBox="0 0 24 24"><path d="M3 3v18h18V3H3zm16 16H5V5h14v14zM7 10h2v7H7v-7zm4-4h2v11h-2V6zm4 5h2v6h-2v-6z"/></svg>`,
  symbols: `<svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>`,
  api: `<svg viewBox="0 0 24 24"><path d="M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-1 9H9V9h10v2zm-4 4H9v-2h6v2zm4-8H9V5h10v2z"/></svg>`,
  tree: `<svg viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM9 17H7v-7h2v7zm4 0h-2V7h2v10zm4 0h-2v-4h2v4z"/></svg>`
};

let currentRepo = null;

const app = document.querySelector('#app');

const renderLayout = (repos) => {
  app.innerHTML = `
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-icon">S</div>
        <h1>Shiori</h1>
      </div>
      <div class="repo-selector">
        <label for="repo-select">Repository</label>
        <select id="repo-select">
          ${repos.map(r => `<option value="${r}">${r}</option>`).join('')}
        </select>
      </div>
      <ul class="nav-menu">
        <a href="#stats" class="nav-item" data-view="stats">
          ${icons.stats} Stats
        </a>
        <a href="#symbol_index" class="nav-item" data-view="symbol_index">
          ${icons.symbols} Symbols
        </a>
        <a href="#api_reference" class="nav-item" data-view="api_reference">
          ${icons.api} API Reference
        </a>
        <a href="#module_tree" class="nav-item" data-view="module_tree">
          ${icons.tree} Module Tree
        </a>
      </ul>
    </aside>
    <main class="main-content">
      <div class="header">
        <h2 id="view-title">Loading...</h2>
        <p id="view-desc"></p>
      </div>
      <div id="content-area"></div>
    </main>
  `;

  document.getElementById('repo-select').addEventListener('change', (e) => {
    currentRepo = e.target.value;
    handleHashChange();
  });
};

const fetchReport = async (template) => {
  const params = new URLSearchParams({ template });
  if (currentRepo) params.set('repo', currentRepo);
  const res = await fetch(`/api/report?${params}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `Failed to fetch ${template} report.`);
  }
  return await res.json();
};

const renderLoading = () => {
  document.getElementById('content-area').innerHTML = `
    <div class="loading-container">
      <p>Loading data...</p>
    </div>
  `;
};

const renderError = (msg) => {
  document.getElementById('content-area').innerHTML = `
    <div class="error-container">
      <p>Error: ${msg}</p>
    </div>
  `;
};

const isSepLine = (cells) => cells.every(c => /^[-:\s]+$/.test(c));

const mdToHtml = (md) => {
  const header = [];
  const body = [];
  let inSep = false;
  let inCode = false;

  for (const line of md.split('\n')) {
    if (line.startsWith('```')) {
      inCode = !inCode;
      continue;
    }
    if (inCode) continue;

    if (line.startsWith('| ')) {
      const cells = line.split('|').slice(1, -1).map(c => c.trim());
      if (isSepLine(cells)) { inSep = true; continue; }
      const row = '<tr>' + cells.map(c => `<td>${c}</td>`).join('') + '</tr>';
      if (!inSep) header.push(row);
      else body.push(row);
    }
  }

  let html = '';
  if (header.length) html += '<thead>' + header.join('') + '</thead>';
  if (body.length) html += '<tbody>' + body.join('') + '</tbody>';
  return html;
};

const renderMermaid = (markdown) => {
  const match = markdown.match(/```mermaid\n([\s\S]*?)```/);
  const graph = match ? match[1].trim() : markdown;
  return `
    <div class="card">
      <div id="mermaid-container" style="text-align:center; padding: 1rem;">
        <pre class="mermaid">${graph}</pre>
      </div>
    </div>
  `;
};

const renderView = async (view) => {
  const contentArea = document.getElementById('content-area');
  const title = document.getElementById('view-title');
  const desc = document.getElementById('view-desc');

  document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active'));
  const activeNav = document.querySelector(`.nav-item[data-view="${view}"]`);
  if (activeNav) activeNav.classList.add('active');

  renderLoading();

  try {
    const data = await fetchReport(view);

    const templates = {
      stats: { title: 'Repository Stats', desc: 'Codebase metrics and language distribution.' },
      symbol_index: { title: 'Symbol Index', desc: 'Browse functions, classes, and other symbols.' },
      api_reference: { title: 'API Reference', desc: 'Extracted docstrings and signatures.' },
      module_tree: { title: 'Module Tree', desc: 'Directory structure and module dependencies.' },
    };

    const tpl = templates[view] || { title: view, desc: '' };
    title.textContent = tpl.title;
    desc.textContent = tpl.desc;

    switch (view) {
      case 'stats':
      case 'symbol_index':
        contentArea.innerHTML = `
          <div class="table-container">
            <table>${mdToHtml(data.markdown)}</table>
          </div>`;
        break;

      case 'api_reference':
        contentArea.innerHTML = `<div class="card"><pre style="white-space:pre-wrap">${data.markdown || ''}</pre></div>`;
        break;

      case 'module_tree':
        contentArea.innerHTML = renderMermaid(data.markdown);
        try {
          mermaid.initialize({ startOnLoad: false, theme: 'dark' });
          const nodes = document.querySelectorAll('.mermaid');
          if (nodes.length) await mermaid.run({ nodes });
        } catch (e) {
          console.warn('mermaid render failed:', e);
        }
        break;
    }
  } catch (err) {
    renderError(err.message);
  }
};

const handleHashChange = () => {
  let hash = window.location.hash.slice(1) || 'stats';
  const valid = ['stats', 'symbol_index', 'api_reference', 'module_tree'];
  if (!valid.includes(hash)) hash = 'stats';
  renderView(hash);
};

window.addEventListener('hashchange', handleHashChange);

const init = async () => {
  try {
    const res = await fetch('/api/repos');
    const data = await res.json();
    currentRepo = data.repos[0] || null;
    renderLayout(data.repos);
    handleHashChange();
  } catch (err) {
    renderLayout([]);
    handleHashChange();
  }
};

init();
