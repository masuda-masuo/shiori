import React, { useState, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import MermaidViewer from "./MermaidViewer";
import SearchView from "./SearchView";

const templates = {
  search: { title: "Search Knowledge", desc: "Search across code, documents, issues, and PR comments." },
  stats: { title: "Repository Stats", desc: "Codebase metrics and language distribution." },
  symbol_index: { title: "Symbol Index", desc: "Browse functions, classes, and other symbols." },
  api_reference: { title: "API Reference", desc: "Extracted docstrings and signatures." },
  module_tree: { title: "Module Tree", desc: "Directory structure and module dependencies." },
};

const isTestPath = (p = "") =>
  /(^|\/)tests?\//i.test(p) ||
  /(^|\/)test_[^/]*$/i.test(p) ||
  /[._-](test|spec)\.[a-z]+$/i.test(p);

const STATS_SCOPES = [
  ["all", "All"],
  ["src", "Source"],
  ["test", "Tests"],
];

const STATS_COLUMNS = [
  ["language", "Language"],
  ["files", "Files"],
  ["code", "Code"],
  ["comments", "Comments"],
  ["blanks", "Blanks"],
];

const StatsView = ({ data }) => {
  const [expandedLang, setExpandedLang] = useState(null);
  const [scope, setScope] = useState("all");
  const [sort, setSort] = useState({ key: "code", dir: "desc" });

  if (!data || !data.rows) return null;

  const inScope = (file) =>
    scope === "all" || (scope === "test" ? isTestPath(file.name) : !isTestPath(file.name));

  // Re-aggregate per language from the file list so the table, the expanded
  // list and the Total row all agree with the selected scope.
  const rows = data.rows
    .map((row) => {
      // A row without a file list cannot be scoped; show it as the server sent it.
      if (!row.reports) return { ...row, reports: [] };
      const files = row.reports.filter(inScope);
      return {
        language: row.language,
        files: files.length,
        code: files.reduce((a, f) => a + (f.code || 0), 0),
        comments: files.reduce((a, f) => a + (f.comments || 0), 0),
        blanks: files.reduce((a, f) => a + (f.blanks || 0), 0),
        reports: files,
      };
    })
    .filter((row) => row.files > 0);

  const dir = sort.dir === "asc" ? 1 : -1;
  rows.sort((a, b) =>
    sort.key === "language"
      ? dir * String(a.language).localeCompare(String(b.language))
      : dir * ((a[sort.key] || 0) - (b[sort.key] || 0))
  );

  const total = rows.reduce(
    (acc, row) => ({
      files: acc.files + (row.files || 0),
      code: acc.code + (row.code || 0),
      comments: acc.comments + (row.comments || 0),
      blanks: acc.blanks + (row.blanks || 0),
    }),
    { files: 0, code: 0, comments: 0, blanks: 0 }
  );

  const onSort = (key) =>
    setSort((s) => ({
      key,
      dir:
        s.key === key
          ? s.dir === "desc"
            ? "asc"
            : "desc"
          : key === "language"
          ? "asc"
          : "desc",
    }));

  const arrow = (key) => (sort.key === key ? (sort.dir === "asc" ? " ↑" : " ↓") : "");
  const num = (n) => (n || 0).toLocaleString("en-US");

  return (
    <>
      <div className="stats-scope">
        <div className="search-type-toggle">
          {STATS_SCOPES.map(([id, label]) => (
            <button
              key={id}
              type="button"
              className={`toggle-btn ${scope === id ? "active" : ""}`}
              onClick={() => setScope(id)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="card">
        <div className="table-container">
          <table className="stats-table">
            <thead>
              <tr>
                {STATS_COLUMNS.map(([key, label]) => (
                  <th
                    key={key}
                    onClick={() => onSort(key)}
                    title={`Sort by ${label}`}
                    style={{ cursor: "pointer", userSelect: "none" }}
                  >
                    {label}
                    {arrow(key)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <React.Fragment key={row.language}>
                  <tr
                    className="stats-row clickable"
                    onClick={() => setExpandedLang(expandedLang === row.language ? null : row.language)}
                    style={{ cursor: "pointer" }}
                  >
                    <td style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontWeight: "500", color: "var(--accent-color)" }}>
                      <span style={{ fontSize: "0.75rem", display: "inline-block", width: "12px" }}>
                        {expandedLang === row.language ? "▼" : "▶"}
                      </span>
                      {row.language}
                    </td>
                    <td>{num(row.files)}</td>
                    <td>{num(row.code)}</td>
                    <td>{num(row.comments)}</td>
                    <td>{num(row.blanks)}</td>
                  </tr>
                  {expandedLang === row.language && row.reports.length > 0 && (
                    <tr>
                      <td colSpan={5} style={{ padding: "0.75rem 1.5rem", background: "rgba(0,0,0,0.15)" }}>
                        <div className="stats-file-list" style={{ maxHeight: "320px", overflowY: "auto" }}>
                          <table style={{ width: "100%", fontSize: "0.875rem" }}>
                            <thead>
                              <tr style={{ background: "transparent", borderBottom: "1px solid rgba(255,255,255,0.05)" }}>
                                <th style={{ textAlign: "left", padding: "0.5rem" }}>File Name</th>
                                <th style={{ textAlign: "right", padding: "0.5rem" }}>Code</th>
                                <th style={{ textAlign: "right", padding: "0.5rem" }}>Comments</th>
                                <th style={{ textAlign: "right", padding: "0.5rem" }}>Blanks</th>
                              </tr>
                            </thead>
                            <tbody>
                              {row.reports.map((file) => (
                                <tr key={file.name} style={{ borderBottom: "1px solid rgba(255,255,255,0.02)" }}>
                                  <td style={{ textAlign: "left", padding: "0.5rem", fontFamily: "monospace", wordBreak: "break-all" }}>
                                    {file.name}{" "}
                                    <span
                                      className={`badge ${isTestPath(file.name) ? "function" : "class"}`}
                                      style={{ fontSize: "0.7rem", padding: "0.05rem 0.35rem" }}
                                    >
                                      {isTestPath(file.name) ? "test" : "src"}
                                    </span>
                                  </td>
                                  <td style={{ textAlign: "right", padding: "0.5rem" }}>{num(file.code)}</td>
                                  <td style={{ textAlign: "right", padding: "0.5rem" }}>{num(file.comments)}</td>
                                  <td style={{ textAlign: "right", padding: "0.5rem" }}>{num(file.blanks)}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              ))}
              <tr style={{ fontWeight: "bold", borderTop: "2px solid var(--panel-border)" }}>
                <td>Total</td>
                <td>{num(total.files)}</td>
                <td>{num(total.code)}</td>
                <td>{num(total.comments)}</td>
                <td>{num(total.blanks)}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
};

const SymbolIndexView = ({ data, searchQuery }) => {
  const [expandedPaths, setExpandedPaths] = useState({});

  if (!data || !data.rows) return null;

  // Filter rows based on search query
  const filteredRows = searchQuery
    ? data.rows.filter(
        (row) =>
          row.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
          row.path.toLowerCase().includes(searchQuery.toLowerCase()) ||
          row.kind.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : data.rows;

  // Group by path
  const groups = {};
  filteredRows.forEach((row) => {
    if (!groups[row.path]) {
      groups[row.path] = [];
    }
    groups[row.path].push(row);
  });

  const paths = Object.keys(groups).sort();

  const isExpanded = (path) => {
    if (searchQuery) return true; // Auto-expand all when searching
    return !!expandedPaths[path];
  };

  const togglePath = (path) => {
    setExpandedPaths((prev) => ({
      ...prev,
      [path]: !prev[path],
    }));
  };

  return (
    <div className="symbol-index-container">
      {paths.length === 0 ? (
        <p style={{ textAlign: "center", padding: "2rem", color: "var(--text-secondary)" }}>
          No symbols found matching the query.
        </p>
      ) : (
        paths.map((path) => {
          const rows = groups[path];
          const expanded = isExpanded(path);
          return (
            <div 
              key={path} 
              className="card" 
              style={{ marginBottom: "0.75rem", padding: "0.75rem 1.25rem" }}
            >
              <div 
                className="symbol-file-header" 
                onClick={() => togglePath(path)}
                style={{ 
                  display: "flex", 
                  justifyContent: "space-between", 
                  alignItems: "center", 
                  cursor: "pointer" 
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
                  <span style={{ fontSize: "0.75rem", width: "12px", display: "inline-block", color: "var(--accent-color)" }}>
                    {expanded ? "▼" : "▶"}
                  </span>
                  <span style={{ fontFamily: "monospace", fontWeight: "600", fontSize: "0.95rem" }}>{path}</span>
                </div>
                <span className="badge" style={{ 
                  background: "rgba(139, 92, 246, 0.15)", 
                  color: "var(--accent-color)", 
                  padding: "0.2rem 0.5rem", 
                  borderRadius: "12px", 
                  fontSize: "0.8rem",
                  fontWeight: "500"
                }}>
                  {rows.length} {rows.length === 1 ? "symbol" : "symbols"}
                </span>
              </div>
              
              {expanded && (
                <div className="table-container" style={{ marginTop: "0.75rem", borderTop: "1px solid rgba(255,255,255,0.05)", paddingTop: "0.75rem" }}>
                  <table style={{ width: "100%", fontSize: "0.9rem" }}>
                    <thead>
                      <tr style={{ background: "transparent" }}>
                        <th style={{ textAlign: "left", padding: "0.4rem 0.5rem" }}>Name</th>
                        <th style={{ textAlign: "left", padding: "0.4rem 0.5rem" }}>Kind</th>
                        <th style={{ textAlign: "left", padding: "0.4rem 0.5rem" }}>Access</th>
                        <th style={{ textAlign: "right", padding: "0.4rem 0.5rem" }}>Line</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row, idx) => (
                        <tr key={idx} style={{ borderBottom: "1px solid rgba(255,255,255,0.02)" }}>
                          <td style={{ textAlign: "left", padding: "0.4rem 0.5rem", fontWeight: "500", fontFamily: "monospace" }}>{row.name}</td>
                          <td style={{ textAlign: "left", padding: "0.4rem 0.5rem" }}>
                            <span style={{ 
                              background: "rgba(255,255,255,0.05)", 
                              padding: "0.15rem 0.4rem", 
                              borderRadius: "4px", 
                              fontSize: "0.75rem",
                              textTransform: "capitalize"
                            }}>
                              {row.kind}
                            </span>
                          </td>
                          <td style={{ textAlign: "left", padding: "0.4rem 0.5rem", fontStyle: "italic", color: "var(--text-secondary)" }}>
                            {row.access || "-"}
                          </td>
                          <td style={{ textAlign: "right", padding: "0.4rem 0.5rem", fontFamily: "monospace", color: "var(--text-secondary)" }}>
                            {row.line}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          );
        })
      )}
    </div>
  );
};

const ReportView = ({ view, repo, repos }) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [searchQuery, setSearchQuery] = useState("");

  const tpl = templates[view] || { title: view, desc: "" };

  useEffect(() => {
    if (view === "search") {
      setLoading(false);
      setError(null);
      setData(null);
      setSearchQuery("");
      return;
    }
    setLoading(true);
    setError(null);
    setData(null);
    setSearchQuery("");

    const params = new URLSearchParams({ template: view });
    if (repo) params.set("repo", repo);
    if (view === "symbol_index") {
      params.set("max_results", "50000");
    } else if (view === "module_tree") {
      params.set("max_results", "10000");
    }

    fetch(`/api/report?${params}`)
      .then(async (res) => {
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(err.detail || `Failed to fetch ${view} report.`);
        }
        return res.json();
      })
      .then((json) => {
        setData(json);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        setError(err.message);
        setLoading(false);
      });
  }, [view, repo]);

  const renderContent = () => {
    if (view === "search") {
      return <SearchView repo={repo} repos={repos} />;
    }
    if (loading) {
      return (
        <div className="loading-container">
          <p>Loading data...</p>
        </div>
      );
    }
    if (error) {
      return (
        <div className="error-container">
          <p>Error: {error}</p>
        </div>
      );
    }
    if (!data || !data.markdown) {
      return <p>No data available.</p>;
    }

    // Special rendering for Module Tree (Mermaid)
    if (view === "module_tree") {
      const match = data.markdown.match(/```mermaid\n([\s\S]*?)```/);
      const graph = match ? match[1].trim() : data.markdown;
      return <MermaidViewer chart={graph} />;
    }

    if (view === "stats" && data.data) {
      return <StatsView data={data.data} />;
    }

    if (view === "symbol_index" && data.data) {
      return (
        <div>
          <input 
            type="text" 
            placeholder="Search symbols..." 
            className="search-input"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
          <SymbolIndexView data={data.data} searchQuery={searchQuery} />
        </div>
      );
    }

    // Default Markdown Rendering for everything else (Stats, Symbols, API)
    return (
      <div className="card">
        {view === "symbol_index" && (
           <input 
             type="text" 
             placeholder="Search symbols..." 
             className="search-input"
             value={searchQuery}
             onChange={(e) => setSearchQuery(e.target.value)}
           />
        )}
        <div className="table-container markdown-body">
          <ReactMarkdown 
            remarkPlugins={[remarkGfm]}
            components={{
              table: ({node, ...props}) => <table {...props} />,
              tr: ({node, children, ...props}) => {
                // simple frontend filter for tables if searchQuery exists
                if (searchQuery && view === "symbol_index") {
                  const text = extractText(children).toLowerCase();
                  if (!text.includes(searchQuery.toLowerCase())) {
                    return <tr {...props} style={{ display: 'none' }}>{children}</tr>;
                  }
                }
                return <tr {...props}>{children}</tr>;
              }
            }}
          >
            {data.markdown}
          </ReactMarkdown>
        </div>
      </div>
    );
  };

  // Helper to extract plain text from React nodes for search filtering
  const extractText = (children) => {
    if (typeof children === "string") return children;
    if (Array.isArray(children)) return children.map(extractText).join("");
    if (children && children.props && children.props.children) {
      return extractText(children.props.children);
    }
    return "";
  };

  return (
    <>
      <div className="header">
        <h2>{tpl.title}</h2>
        <p>{tpl.desc}</p>
      </div>
      <div id="content-area">
        {renderContent()}
      </div>
    </>
  );
};

export default ReportView;