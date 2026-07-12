import './style.css';
import mermaid from 'mermaid';

mermaid.initialize({ startOnLoad: false, theme: 'dark' });

// --- SVG Icons ---
const icons = {
  stats: `<svg viewBox="0 0 24 24"><path d="M3 3v18h18V3H3zm16 16H5V5h14v14zM7 10h2v7H7v-7zm4-4h2v11h-2V6zm4 5h2v6h-2v-6z"/></svg>`,
  symbols: `<svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>`,
  api: `<svg viewBox="0 0 24 24"><path d="M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-1 9H9V9h10v2zm-4 4H9v-2h6v2zm4-8H9V5h10v2z"/></svg>`,
  tree: `<svg viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM9 17H7v-7h2v7zm4 0h-2V7h2v10zm4 0h-2v-4h2v4z"/></svg>`
};

// --- App Structure & Router ---
const app = document.querySelector('#app');

const renderLayout = () => {
  app.innerHTML = `
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-icon">S</div>
        <h1>Shiori</h1>
      </div>
      <ul class="nav-menu">
        <a href="#stats" class="nav-item" data-view="stats">
          ${icons.stats} Stats
        </a>
        <a href="#symbols" class="nav-item" data-view="symbols">
          ${icons.symbols} Symbols
        </a>
        <a href="#api" class="nav-item" data-view="api">
          ${icons.api} API Reference
        </a>
        <a href="#tree" class="nav-item" data-view="tree">
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
};

// --- Data Fetching ---
const fetchReport = async (template) => {
  try {
    const res = await fetch(`/api/report?template=${template}`);
    if (!res.ok) throw new Error(\`Failed to fetch \${template} report.\`);
    return await res.json();
  } catch (err) {
    console.error(err);
    // Return mock data fallback for development
    return getMockData(template);
  }
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

// --- Views ---
const renderView = async (view) => {
  const contentArea = document.getElementById('content-area');
  const title = document.getElementById('view-title');
  const desc = document.getElementById('view-desc');

  // Update Navigation Active State
  document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active'));
  const activeNav = document.querySelector(`.nav-item[data-view="\${view}"]`);
  if (activeNav) activeNav.classList.add('active');

  renderLoading();

  try {
    const data = await fetchReport(view);

    switch (view) {
      case 'stats':
        title.textContent = 'Repository Stats';
        desc.textContent = 'Codebase metrics and language distribution.';
        
        const langRows = data.languages.map(l => `
          <tr>
            <td>${l.name}</td>
            <td>${l.lines.toLocaleString()}</td>
          </tr>
        `).join('');

        contentArea.innerHTML = `
          <div class="grid">
            <div class="card">
              <h3>Total Lines</h3>
              <div class="stat-value">${data.totalLines.toLocaleString()}</div>
            </div>
            <div class="card">
              <h3>Total Files</h3>
              <div class="stat-value">${data.totalFiles}</div>
            </div>
          </div>
          <div class="card">
            <h3>Language Distribution</h3>
            <div class="table-container">
              <table>
                <thead>
                  <tr>
                    <th>Language</th>
                    <th>Lines</th>
                  </tr>
                </thead>
                <tbody>
                  ${langRows}
                </tbody>
              </table>
            </div>
          </div>
        `;
        break;

      case 'symbols':
        title.textContent = 'Symbol Index';
        desc.textContent = 'Browse functions, classes, and other symbols.';
        
        const symRows = data.symbols.map(s => `
          <tr>
            <td><strong>${s.name}</strong></td>
            <td><span class="badge ${s.kind}">${s.kind}</span></td>
            <td><span class="badge ${s.access}">${s.access}</span></td>
            <td><a href="#" class="location-link">${s.path}:${s.line}</a></td>
          </tr>
        `).join('');

        contentArea.innerHTML = `
          <div class="card">
            <div class="table-container">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Kind</th>
                    <th>Visibility</th>
                    <th>Location</th>
                  </tr>
                </thead>
                <tbody>
                  ${symRows}
                </tbody>
              </table>
            </div>
          </div>
        `;
        break;
        
      case 'api':
        title.textContent = 'API Reference';
        desc.textContent = 'Extracted docstrings and signatures.';
        contentArea.innerHTML = `
          <div class="card">
            <p>API Reference content will be rendered here.</p>
            <pre style="background:rgba(0,0,0,0.2);padding:1rem;border-radius:8px;margin-top:1rem;color:var(--text-secondary);">${data.apiText || 'def example(): pass'}</pre>
          </div>
        `;
        break;
        
      case 'tree':
        title.textContent = 'Module Tree';
        desc.textContent = 'Directory structure and module dependencies.';
        contentArea.innerHTML = `
          <div class="card">
            <div id="mermaid-container" style="text-align:center; padding: 1rem;">
              <pre class="mermaid">${data.mermaidGraph}</pre>
            </div>
          </div>
        `;
        await mermaid.run({ querySelector: '.mermaid' });
        break;
    }
  } catch (err) {
    renderError(err.message);
  }
};

const handleHashChange = () => {
  const hash = window.location.hash.slice(1) || 'stats';
  renderView(hash);
};

// --- Mock Data Fallback ---
function getMockData(template) {
  if (template === 'stats') {
    return {
      totalLines: 15678, totalFiles: 142,
      languages: [
        { name: 'Python', lines: 12040 },
        { name: 'Markdown', lines: 2500 },
        { name: 'JavaScript', lines: 1138 }
      ]
    };
  } else if (template === 'symbols') {
    return {
      symbols: [
        { name: 'shiori_report', kind: 'function', access: 'public', path: 'src/shiori/mcp_server.py', line: 120 },
        { name: 'Embedder', kind: 'class', access: 'public', path: 'src/shiori/embedding.py', line: 45 },
        { name: '_resolve_repo', kind: 'function', access: 'private', path: 'src/shiori/mcp_server.py', line: 60 }
      ]
    };
  } else if (template === 'api') {
    return { apiText: 'def shiori_report(template: str) -> dict:\n    """Generate reports..."""' };
  } else if (template === 'tree') {
    return { mermaidGraph: 'graph TD;\\n  src-->shiori;\\n  shiori-->mcp_server.py;' };
  }
}

// Initialize App
renderLayout();
window.addEventListener('hashchange', handleHashChange);
handleHashChange();