import './style.css';

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
        <li class="nav-item active" data-view="stats">
          <span>📊</span> Stats
        </li>
        <li class="nav-item" data-view="symbols">
          <span>🔍</span> Symbols
        </li>
        <li class="nav-item" data-view="api">
          <span>📖</span> API Reference
        </li>
        <li class="nav-item" data-view="tree">
          <span>🌳</span> Module Tree
        </li>
      </ul>
    </aside>
    <main class="main-content">
      <div class="header">
        <h2 id="view-title">Repository Stats</h2>
        <p id="view-desc">Overview of the codebase metrics.</p>
      </div>
      <div id="content-area"></div>
    </main>
  `;

  // Attach navigation events
  document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
      document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active'));
      e.currentTarget.classList.add('active');
      const view = e.currentTarget.getAttribute('data-view');
      renderView(view);
    });
  });
};

// --- Mock Data ---
const mockStats = {
  totalLines: 15678,
  totalFiles: 142,
  languages: [
    { name: 'Python', lines: 12040 },
    { name: 'Markdown', lines: 2500 },
    { name: 'JavaScript', lines: 1138 }
  ]
};

const mockSymbols = [
  { name: 'shiori_report', kind: 'function', access: 'public', path: 'src/shiori/mcp_server.py', line: 120 },
  { name: 'Embedder', kind: 'class', access: 'public', path: 'src/shiori/embedding.py', line: 45 },
  { name: '_resolve_repo', kind: 'function', access: 'private', path: 'src/shiori/mcp_server.py', line: 60 }
];

// --- Views ---
const renderView = (view) => {
  const contentArea = document.getElementById('content-area');
  const title = document.getElementById('view-title');
  const desc = document.getElementById('view-desc');

  switch (view) {
    case 'stats':
      title.textContent = 'Repository Stats';
      desc.textContent = 'Codebase metrics and language distribution.';
      
      const langRows = mockStats.languages.map(l => `
        <tr>
          <td>${l.name}</td>
          <td>${l.lines.toLocaleString()}</td>
        </tr>
      `).join('');

      contentArea.innerHTML = `
        <div class="grid">
          <div class="card">
            <h3>Total Lines</h3>
            <div class="stat-value">${mockStats.totalLines.toLocaleString()}</div>
          </div>
          <div class="card">
            <h3>Total Files</h3>
            <div class="stat-value">${mockStats.totalFiles}</div>
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
      
      const symRows = mockSymbols.map(s => `
        <tr>
          <td><strong>${s.name}</strong></td>
          <td><span class="badge ${s.kind}">${s.kind}</span></td>
          <td><span class="badge ${s.access}">${s.access}</span></td>
          <td><a href="#" style="color:var(--accent-color);text-decoration:none;">${s.path}:${s.line}</a></td>
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
          <p>API Reference content will be rendered here from Markdown/HTML.</p>
          <pre style="background:rgba(0,0,0,0.2);padding:1rem;border-radius:8px;margin-top:1rem;color:var(--text-secondary);">def shiori_report(template: str) -> dict:\n    """Generate reports..."""</pre>
        </div>
      `;
      break;
      
    case 'tree':
      title.textContent = 'Module Tree';
      desc.textContent = 'Directory structure and module dependencies.';
      contentArea.innerHTML = `
        <div class="card">
          <p>Mermaid.js diagram will be rendered here.</p>
          <div style="padding:2rem;text-align:center;color:var(--text-secondary);border:1px dashed var(--panel-border);border-radius:8px;margin-top:1rem;">
            [ Graph Placeholder ]
          </div>
        </div>
      `;
      break;
  }
};

// Initialize App
renderLayout();
renderView('stats');