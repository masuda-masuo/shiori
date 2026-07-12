import React, { useState, useEffect } from "react";
import { 
  Search, Sliders, GitPullRequest, MessageSquare, 
  FileCode, FileText, ChevronDown, ChevronUp, 
  ExternalLink, Clock, User, Sparkles, Filter
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const SearchView = ({ repo: defaultRepo, repos }) => {
  const [query, setQuery] = useState("");
  const [searchType, setSearchType] = useState("semantic");
  const [repo, setRepo] = useState(defaultRepo || "");
  const [pathPrefix, setPathPrefix] = useState("");
  const [progLang, setProgLang] = useState("");
  
  // Tabs: all, issues_prs, code, docs
  const [activeTab, setActiveTab] = useState("all");
  // Sub-filter for Issues/PRs: all, issue, pr
  const [subType, setSubType] = useState("all");
  
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [searchTime, setSearchTime] = useState(null);
  
  // Expanded previews state: { [resultIndex]: { type: 'code'|'timeline', data: ... } }
  const [previews, setPreviews] = useState({});
  const [previewLoading, setPreviewLoading] = useState({});

  const [showFilters, setShowFilters] = useState(false);

  useEffect(() => {
    if (defaultRepo && !repo) {
      setRepo(defaultRepo);
    }
  }, [defaultRepo]);

  const handleSearch = async (e) => {
    if (e) e.preventDefault();
    if (!query.trim()) return;

    setLoading(true);
    setError(null);
    setPreviews({});
    const startTime = performance.now();

    // Determine parameters based on activeTab and subType
    let source_type = null;
    let kind = null;

    if (activeTab === "issues_prs") {
      if (subType === "issue") {
        kind = "issue";
      } else if (subType === "pr") {
        kind = "pr";
      }
    } else if (activeTab === "code") {
      source_type = "code";
    } else if (activeTab === "docs") {
      source_type = "doc";
    }

    const params = new URLSearchParams({
      query: query,
      type: searchType,
      limit: "40"
    });

    if (repo && repo !== "*all*") params.set("repo", repo);
    if (source_type) params.set("source_type", source_type);
    if (kind) params.set("kind", kind);
    if (pathPrefix) params.set("path_prefix", pathPrefix);
    if (progLang && activeTab === "code") params.set("prog_lang", progLang);

    try {
      const res = await fetch(`/api/search?${params}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || "Search failed");
      }
      const data = await res.json();
      
      let filteredResults = data.results || [];
      
      // Frontend filter for Issues/PRs 'all' subtype (to exclude code/docs)
      if (activeTab === "issues_prs" && subType === "all") {
        filteredResults = filteredResults.filter(
          r => r.source_type === "issue" || r.source_type === "pr_review"
        );
      }

      setResults(filteredResults);
      setSearchTime(Math.round(performance.now() - startTime));
    } catch (err) {
      console.error(err);
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  // Re-run search when tab or subtype changes
  useEffect(() => {
    if (query.trim()) {
      handleSearch();
    }
  }, [activeTab, subType]);

  const togglePreview = async (index, type, item) => {
    if (previews[index]) {
      setPreviews(prev => {
        const copy = { ...prev };
        delete copy[index];
        return copy;
      });
      return;
    }

    setPreviewLoading(prev => ({ ...prev, [index]: true }));

    try {
      if (type === "code") {
        const start = Math.max(1, (item.line || 1) - 15);
        const end = (item.line || 1) + 15;
        const params = new URLSearchParams({
          path: item.path,
          repo: item.repo,
          start_line: start.toString(),
          end_line: end.toString()
        });
        const res = await fetch(`/api/read_file?${params}`);
        if (!res.ok) throw new Error("Failed to load preview");
        const data = await res.json();
        setPreviews(prev => ({
          ...prev,
          [index]: { type: "code", data, startLine: start }
        }));
      } else if (type === "timeline") {
        const params = new URLSearchParams({
          number: item.issue_no.toString(),
          repo: item.repo
        });
        const res = await fetch(`/api/issue?${params}`);
        if (!res.ok) throw new Error("Failed to load timeline");
        const data = await res.json();
        setPreviews(prev => ({
          ...prev,
          [index]: { type: "timeline", data }
        }));
      }
    } catch (err) {
      console.error(err);
      alert(err.message);
    } finally {
      setPreviewLoading(prev => ({ ...prev, [index]: false }));
    }
  };

  const highlightText = (text, queryStr) => {
    if (!queryStr || !text) return text;
    const tokens = queryStr.split(/\s+/).filter(Boolean);
    if (tokens.length === 0) return text;
    
    const escapedTokens = tokens.map(t => t.replace(/[-\/\\^$*+?.()|[\]{}]/g, '\\$&'));
    const regex = new RegExp(`(${escapedTokens.join('|')})`, 'gi');
    
    const parts = text.split(regex);
    return parts.map((part, idx) => 
      regex.test(part) ? <mark key={idx} className="search-highlight">{part}</mark> : part
    );
  };

  const getSourceIcon = (source_type, kind) => {
    if (source_type === "code") return <FileCode size={18} className="icon-code" />;
    if (source_type === "doc") return <FileText size={18} className="icon-doc" />;
    if (kind === "pr" || source_type === "pr_review") return <GitPullRequest size={18} className="icon-pr" />;
    return <MessageSquare size={18} className="icon-issue" />;
  };

  const getSourceLabel = (source_type, kind) => {
    if (source_type === "code") return "Code";
    if (source_type === "doc") return "Doc";
    if (source_type === "pr_review") return "PR Review";
    if (kind === "pr") return "Pull Request";
    return "Issue";
  };

  return (
    <div className="search-view-container">
      <style>{`
        .search-view-container {
          display: flex;
          flex-direction: column;
          gap: 1.5rem;
          max-width: 1000px;
          margin: 0 auto;
        }
        .search-bar-form {
          display: flex;
          flex-direction: column;
          gap: 1rem;
          background: var(--panel-bg);
          border: 1px solid var(--panel-border);
          border-radius: 16px;
          padding: 1.5rem;
          backdrop-filter: blur(12px);
          box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
        }
        .search-input-group {
          display: flex;
          gap: 0.75rem;
          position: relative;
        }
        .search-icon-wrapper {
          position: absolute;
          left: 1rem;
          top: 50%;
          transform: translateY(-50%);
          color: var(--text-secondary);
          display: flex;
          align-items: center;
          pointer-events: none;
        }
        .search-bar-input {
          flex: 1;
          padding: 1rem 1rem 1rem 3rem;
          background: rgba(255, 255, 255, 0.03);
          border: 1px solid rgba(255, 255, 255, 0.08);
          border-radius: 12px;
          color: var(--text-primary);
          font-size: 1.1rem;
          font-family: inherit;
          transition: all 0.3s ease;
        }
        .search-bar-input:focus {
          outline: none;
          border-color: var(--accent-color);
          background: rgba(255, 255, 255, 0.06);
          box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.2);
        }
        .btn-search {
          padding: 0 2rem;
          background: var(--accent-gradient);
          color: white;
          border: none;
          border-radius: 12px;
          font-weight: 600;
          font-size: 1rem;
          cursor: pointer;
          transition: all 0.2s ease;
          display: flex;
          align-items: center;
          gap: 0.5rem;
          box-shadow: 0 4px 15px rgba(139, 92, 246, 0.3);
        }
        .btn-search:hover {
          opacity: 0.9;
          transform: translateY(-1px);
        }
        .search-controls {
          display: flex;
          flex-wrap: wrap;
          justify-content: space-between;
          align-items: center;
          gap: 1rem;
          padding-top: 0.5rem;
          border-top: 1px solid rgba(255, 255, 255, 0.05);
        }
        .search-type-toggle {
          display: flex;
          background: rgba(255, 255, 255, 0.04);
          border: 1px solid rgba(255, 255, 255, 0.06);
          border-radius: 8px;
          padding: 0.25rem;
        }
        .toggle-btn {
          padding: 0.4rem 1rem;
          border: none;
          background: transparent;
          color: var(--text-secondary);
          font-size: 0.85rem;
          font-weight: 500;
          border-radius: 6px;
          cursor: pointer;
          transition: all 0.2s ease;
          display: flex;
          align-items: center;
          gap: 0.35rem;
        }
        .toggle-btn.active {
          background: rgba(255, 255, 255, 0.08);
          color: var(--text-primary);
          box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        }
        .btn-filter-toggle {
          display: flex;
          align-items: center;
          gap: 0.5rem;
          background: transparent;
          border: 1px solid rgba(255, 255, 255, 0.1);
          color: var(--text-secondary);
          padding: 0.5rem 1rem;
          border-radius: 8px;
          cursor: pointer;
          font-size: 0.85rem;
          transition: all 0.2s;
        }
        .btn-filter-toggle:hover {
          border-color: var(--accent-color);
          color: var(--text-primary);
        }
        .advanced-filters {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
          gap: 1rem;
          padding: 1rem 0 0.5rem;
          border-top: 1px dashed rgba(255, 255, 255, 0.05);
          animation: fadeIn 0.2s ease;
        }
        .filter-field {
          display: flex;
          flex-direction: column;
          gap: 0.4rem;
        }
        .filter-field label {
          font-size: 0.75rem;
          color: var(--text-secondary);
          font-weight: 500;
        }
        .filter-field input, .filter-field select {
          padding: 0.5rem 0.75rem;
          background: rgba(255, 255, 255, 0.03);
          border: 1px solid rgba(255, 255, 255, 0.08);
          border-radius: 6px;
          color: var(--text-primary);
          font-size: 0.85rem;
          font-family: inherit;
          outline: none;
        }
        .filter-field input:focus, .filter-field select:focus {
          border-color: var(--accent-color);
        }
        
        /* Tabs */
        .search-tabs {
          display: flex;
          border-bottom: 1px solid var(--panel-border);
          gap: 1.5rem;
          margin-bottom: 0.5rem;
        }
        .search-tab {
          padding: 0.75rem 0.5rem;
          background: transparent;
          border: none;
          color: var(--text-secondary);
          font-size: 0.95rem;
          font-weight: 500;
          cursor: pointer;
          position: relative;
          transition: color 0.2s;
        }
        .search-tab:hover {
          color: var(--text-primary);
        }
        .search-tab.active {
          color: var(--text-primary);
          font-weight: 600;
        }
        .search-tab.active::after {
          content: '';
          position: absolute;
          bottom: -1px;
          left: 0;
          right: 0;
          height: 2px;
          background: var(--accent-gradient);
        }

        /* Sub Type Toggles */
        .sub-type-toggles {
          display: flex;
          gap: 0.5rem;
          margin-bottom: 0.5rem;
        }
        .sub-type-btn {
          padding: 0.3rem 0.75rem;
          background: rgba(255, 255, 255, 0.03);
          border: 1px solid rgba(255, 255, 255, 0.06);
          border-radius: 20px;
          color: var(--text-secondary);
          font-size: 0.8rem;
          cursor: pointer;
          transition: all 0.2s;
        }
        .sub-type-btn:hover {
          background: rgba(255, 255, 255, 0.06);
          color: var(--text-primary);
        }
        .sub-type-btn.active {
          background: rgba(139, 92, 246, 0.15);
          border-color: rgba(139, 92, 246, 0.3);
          color: #c084fc;
        }

        .search-meta {
          font-size: 0.85rem;
          color: var(--text-secondary);
          margin-bottom: 0.5rem;
        }

        /* Results List */
        .search-results-list {
          display: flex;
          flex-direction: column;
          gap: 1rem;
        }
        
        .result-card {
          background: var(--panel-bg);
          border: 1px solid var(--panel-border);
          border-radius: 12px;
          padding: 1.25rem;
          backdrop-filter: blur(12px);
          transition: all 0.2s ease;
          display: flex;
          flex-direction: column;
          gap: 0.75rem;
        }
        .result-card:hover {
          border-color: rgba(139, 92, 246, 0.3);
          box-shadow: 0 4px 20px rgba(0, 0, 0, 0.15);
        }
        .result-header {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          gap: 1rem;
        }
        .result-title-area {
          display: flex;
          align-items: center;
          gap: 0.75rem;
        }
        .result-icon-box {
          width: 32px;
          height: 32px;
          border-radius: 8px;
          background: rgba(255, 255, 255, 0.03);
          display: flex;
          align-items: center;
          justify-content: center;
          border: 1px solid rgba(255, 255, 255, 0.05);
        }
        .icon-code { color: #60a5fa; }
        .icon-doc { color: #34d399; }
        .icon-pr { color: #c084fc; }
        .icon-issue { color: #f59e0b; }

        .result-title-info {
          display: flex;
          flex-direction: column;
          gap: 0.2rem;
        }
        .result-title-link {
          font-weight: 600;
          color: var(--text-primary);
          text-decoration: none;
          display: inline-flex;
          align-items: center;
          gap: 0.35rem;
          font-size: 0.95rem;
          transition: color 0.2s;
        }
        .result-title-link:hover {
          color: var(--accent-color);
        }
        .result-path {
          font-size: 0.8rem;
          color: var(--text-secondary);
          font-family: monospace;
          word-break: break-all;
        }
        
        .result-score-badge {
          font-size: 0.75rem;
          font-weight: 600;
          padding: 0.2rem 0.5rem;
          background: rgba(255, 255, 255, 0.05);
          border: 1px solid rgba(255, 255, 255, 0.08);
          border-radius: 6px;
          color: var(--text-secondary);
        }

        .result-snippet {
          font-size: 0.9rem;
          color: #d1d5db;
          line-height: 1.5;
          background: rgba(0, 0, 0, 0.2);
          padding: 0.75rem 1rem;
          border-radius: 8px;
          white-space: pre-wrap;
          font-family: monospace;
          border-left: 3px solid rgba(139, 92, 246, 0.5);
          overflow-x: auto;
        }

        .search-highlight {
          background-color: rgba(139, 92, 246, 0.3);
          color: #fff;
          padding: 0.05rem 0.15rem;
          border-radius: 3px;
          font-weight: 500;
        }

        .result-footer {
          display: flex;
          justify-content: space-between;
          align-items: center;
          flex-wrap: wrap;
          gap: 0.75rem;
          font-size: 0.8rem;
          color: var(--text-secondary);
        }
        .result-metadata-chips {
          display: flex;
          align-items: center;
          gap: 1rem;
          flex-wrap: wrap;
        }
        .meta-chip {
          display: flex;
          align-items: center;
          gap: 0.35rem;
        }
        .btn-preview {
          background: transparent;
          border: 1px solid rgba(255, 255, 255, 0.1);
          color: var(--text-primary);
          padding: 0.35rem 0.75rem;
          border-radius: 6px;
          cursor: pointer;
          font-size: 0.8rem;
          font-weight: 500;
          transition: all 0.2s;
          display: flex;
          align-items: center;
          gap: 0.35rem;
        }
        .btn-preview:hover {
          border-color: var(--accent-color);
          background: rgba(139, 92, 246, 0.08);
        }

        /* Preview Panes */
        .preview-pane {
          background: rgba(0, 0, 0, 0.25);
          border: 1px solid rgba(255, 255, 255, 0.05);
          border-radius: 8px;
          padding: 1rem;
          margin-top: 0.5rem;
          animation: slideDown 0.2s ease-out;
        }
        .preview-code-block {
          font-family: 'Fira Code', 'Courier New', Courier, monospace;
          font-size: 0.85rem;
          line-height: 1.5;
          overflow-x: auto;
          display: grid;
          grid-template-columns: auto 1fr;
          gap: 1rem;
        }
        .preview-line-numbers {
          color: rgba(255, 255, 255, 0.2);
          text-align: right;
          user-select: none;
          border-right: 1px solid rgba(255, 255, 255, 0.05);
          padding-right: 0.75rem;
        }
        .preview-code-lines {
          white-space: pre;
          color: #e5e7eb;
        }
        .highlighted-line {
          background: rgba(139, 92, 246, 0.15);
          display: block;
          margin: 0 -1rem;
          padding: 0 1rem;
          border-left: 2px solid var(--accent-color);
        }

        /* Timeline Preview */
        .preview-timeline {
          display: flex;
          flex-direction: column;
          gap: 1rem;
          max-height: 400px;
          overflow-y: auto;
          padding-right: 0.5rem;
        }
        .timeline-item {
          display: flex;
          flex-direction: column;
          gap: 0.4rem;
          border-bottom: 1px solid rgba(255, 255, 255, 0.03);
          padding-bottom: 0.75rem;
        }
        .timeline-item:last-child {
          border-bottom: none;
          padding-bottom: 0;
        }
        .timeline-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          font-size: 0.75rem;
          color: var(--text-secondary);
        }
        .timeline-author {
          font-weight: 600;
          color: var(--text-primary);
          display: flex;
          align-items: center;
          gap: 0.25rem;
        }
        .timeline-body {
          font-size: 0.85rem;
          color: #d1d5db;
          word-break: break-word;
        }
        .timeline-body p {
          margin-bottom: 0.5rem;
        }
        .timeline-body pre {
          background: rgba(0, 0, 0, 0.4);
          padding: 0.5rem;
          border-radius: 6px;
          overflow-x: auto;
          margin: 0.5rem 0;
        }

        @keyframes fadeIn {
          from { opacity: 0; }
          to { opacity: 1; }
        }
        @keyframes slideDown {
          from { opacity: 0; transform: translateY(-5px); }
          to { opacity: 1; transform: translateY(0); }
        }
      `}</style>

      <form className="search-bar-form" onSubmit={handleSearch}>
        <div className="search-input-group">
          <div className="search-icon-wrapper">
            <Search size={20} />
          </div>
          <input
            type="text"
            className="search-bar-input"
            placeholder="Search code, issues, PRs, or docs..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button type="submit" className="btn-search">
            Search
          </button>
        </div>

        <div className="search-controls">
          <div className="search-type-toggle">
            <button
              type="button"
              className={`toggle-btn ${searchType === "semantic" ? "active" : ""}`}
              onClick={() => setSearchType("semantic")}
              title="Semantic/embedding-based search. Fuses vector and keyword ranks via RRF."
            >
              <Sparkles size={14} /> Semantic
            </button>
            <button
              type="button"
              className={`toggle-btn ${searchType === "keyword" ? "active" : ""}`}
              onClick={() => setSearchType("keyword")}
              title="Keyword-based search. Strong for exact matches: symbols, errors, config keys."
            >
              Keyword
            </button>
          </div>

          <button
            type="button"
            className="btn-filter-toggle"
            onClick={() => setShowFilters(!showFilters)}
          >
            <Filter size={14} />
            {showFilters ? "Hide Filters" : "Show Filters"}
            <ChevronDown size={14} style={{ transform: showFilters ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }} />
          </button>
        </div>

        {showFilters && (
          <div className="advanced-filters">
            <div className="filter-field">
              <label htmlFor="filter-repo">Repository</label>
              <select
                id="filter-repo"
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
              >
                <option value="*all*">All Repositories</option>
                {repos.map(r => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
            
            <div className="filter-field">
              <label htmlFor="filter-path">Path Prefix</label>
              <input
                id="filter-path"
                type="text"
                placeholder="e.g. src/shiori"
                value={pathPrefix}
                onChange={(e) => setPathPrefix(e.target.value)}
              />
            </div>

            {activeTab === "code" && (
              <div className="filter-field">
                <label htmlFor="filter-lang">Language</label>
                <input
                  id="filter-lang"
                  type="text"
                  placeholder="e.g. python, typescript"
                  value={progLang}
                  onChange={(e) => setProgLang(e.target.value)}
                />
              </div>
            )}
          </div>
        )}
      </form>

      <div className="search-tabs">
        <button
          className={`search-tab ${activeTab === "all" ? "active" : ""}`}
          onClick={() => setActiveTab("all")}
        >
          All
        </button>
        <button
          className={`search-tab ${activeTab === "issues_prs" ? "active" : ""}`}
          onClick={() => setActiveTab("issues_prs")}
        >
          Issues / PRs
        </button>
        <button
          className={`search-tab ${activeTab === "code" ? "active" : ""}`}
          onClick={() => setActiveTab("code")}
        >
          Code
        </button>
        <button
          className={`search-tab ${activeTab === "docs" ? "active" : ""}`}
          onClick={() => setActiveTab("docs")}
        >
          Docs
        </button>
      </div>

      {activeTab === "issues_prs" && (
        <div className="sub-type-toggles">
          <button
            className={`sub-type-btn ${subType === "all" ? "active" : ""}`}
            onClick={() => setSubType("all")}
          >
            All Threads
          </button>
          <button
            className={`sub-type-btn ${subType === "issue" ? "active" : ""}`}
            onClick={() => setSubType("issue")}
          >
            Issues Only
          </button>
          <button
            className={`sub-type-btn ${subType === "pr" ? "active" : ""}`}
            onClick={() => setSubType("pr")}
          >
            PRs Only
          </button>
        </div>
      )}

      {loading ? (
        <div className="loading-container">
          <p>Searching codebase...</p>
        </div>
      ) : error ? (
        <div className="error-container">
          <p>{error}</p>
        </div>
      ) : (
        <>
          {searchTime !== null && (
            <div className="search-meta">
              Found {results.length} {results.length === 1 ? "result" : "results"} in {searchTime}ms
            </div>
          )}

          <div className="search-results-list">
            {results.length === 0 ? (
              query.trim() && (
                <div className="card" style={{ textAlign: "center", padding: "3rem", color: "var(--text-secondary)" }}>
                  No results found. Try adjusting your search term or filters.
                </div>
              )
            ) : (
              results.map((item, index) => {
                const isCode = item.source_type === "code";
                const isTimeline = item.source_type === "issue" || item.source_type === "pr_review";
                const hasTimeline = isTimeline && item.issue_no;
                const hasCodePreview = isCode && item.path && item.line;

                return (
                  <div key={index} className="result-card">
                    <div className="result-header">
                      <div className="result-title-area">
                        <div className="result-icon-box">
                          {getSourceIcon(item.source_type, item.kind)}
                        </div>
                        <div className="result-title-info">
                          <a
                            href={item.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="result-title-link"
                          >
                            {item.source_type === "doc" && item.heading_path 
                              ? item.heading_path 
                              : item.source_type === "code" 
                              ? `${item.path.split('/').pop()} : Line ${item.line}`
                              : item.heading_path || `#${item.issue_no} ${item.kind === 'pr' ? 'PR' : 'Issue'}`
                            }
                            <ExternalLink size={12} />
                          </a>
                          <span className="result-path">
                            {item.repo} &raquo; {item.path || "discussion"}
                          </span>
                        </div>
                      </div>
                      <div className="result-score-badge">
                        Score: {item.score}
                      </div>
                    </div>

                    <div className="result-snippet">
                      {highlightText(item.snippet, query)}
                    </div>

                    <div className="result-footer">
                      <div className="result-metadata-chips">
                        <span className="badge">
                          {getSourceLabel(item.source_type, item.kind)}
                        </span>
                        {item.author && (
                          <span className="meta-chip">
                            <User size={12} /> {item.author}
                          </span>
                        )}
                        {item.updated_at && (
                          <span className="meta-chip">
                            <Clock size={12} /> {new Date(item.updated_at).toLocaleDateString()}
                          </span>
                        )}
                      </div>

                      {((hasCodePreview && item.line) || (hasTimeline && item.issue_no)) && (
                        <button
                          type="button"
                          className="btn-preview"
                          onClick={() => togglePreview(index, isCode ? "code" : "timeline", item)}
                          disabled={previewLoading[index]}
                        >
                          {previewLoading[index] ? (
                            "Loading..."
                          ) : previews[index] ? (
                            <>Hide Preview</>
                          ) : isCode ? (
                            <>Show Preview</>
                          ) : (
                            <>Show Conversation</>
                          )}
                        </button>
                      )}
                    </div>

                    {previews[index] && previews[index].type === "code" && (
                      <div className="preview-pane">
                        <div className="preview-code-block">
                          <div className="preview-line-numbers">
                            {previews[index].data.content.split('\n').map((_, i) => (
                              <div 
                                key={i} 
                                className={previews[index].startLine + i === item.line ? "highlighted-line" : ""}
                              >
                                {previews[index].startLine + i}
                              </div>
                            ))}
                          </div>
                          <div className="preview-code-lines">
                            {previews[index].data.content.split('\n').map((line, i) => (
                              <div 
                                key={i} 
                                className={previews[index].startLine + i === item.line ? "highlighted-line" : ""}
                              >
                                {highlightText(line, query)}
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}

                    {previews[index] && previews[index].type === "timeline" && (
                      <div className="preview-pane">
                        <div className="preview-timeline">
                          {previews[index].data.items && previews[index].data.items.map((comment, cIdx) => (
                            <div key={cIdx} className="timeline-item">
                              <div className="timeline-header">
                                <span className="timeline-author">
                                  <User size={10} /> {comment.author} {comment.is_bot && <span className="badge" style={{ fontSize: '0.65rem', padding: '0.05rem 0.25rem' }}>bot</span>}
                                </span>
                                <span>{comment.created_at ? new Date(comment.created_at).toLocaleString() : ""}</span>
                              </div>
                              <div className="timeline-body markdown-body">
                                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                  {comment.body}
                                </ReactMarkdown>
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </>
      )}
    </div>
  );
};

export default SearchView;