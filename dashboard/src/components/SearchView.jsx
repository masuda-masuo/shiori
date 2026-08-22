import React, { useState, useEffect } from "react";
import { Search, ChevronDown, Sparkles, Filter } from "lucide-react";
import SearchFilters from "./search/SearchFilters";
import SearchTabs from "./search/SearchTabs";
import SearchResultList from "./search/SearchResultList";

const SearchView = ({ repo: defaultRepo, repos }) => {
  const [query, setQuery] = useState("");
  const [searchType, setSearchType] = useState("semantic");
  const [repo, setRepo] = useState(defaultRepo || "");
  const [pathPrefix, setPathPrefix] = useState("");
  const [progLang, setProgLang] = useState("");
  const [activeTab, setActiveTab] = useState("all");
  const [subType, setSubType] = useState("all");
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [searchTime, setSearchTime] = useState(null);
  const [previews, setPreviews] = useState({});
  const [previewLoading, setPreviewLoading] = useState({});
  const [showFilters, setShowFilters] = useState(false);

  useEffect(() => {
    if (defaultRepo && !repo) setRepo(defaultRepo);
  }, [defaultRepo]);

  const handleSearch = async (e) => {
    if (e) e.preventDefault();
    if (!query.trim()) return;

    setLoading(true);
    setError(null);
    setPreviews({});
    const startTime = performance.now();

    let source_type = null;
    let kind = null;
    if (activeTab === "issues_prs") {
      if (subType === "issue") kind = "issue";
      else if (subType === "pr") kind = "pr";
    } else if (activeTab === "code") {
      source_type = "code";
    } else if (activeTab === "docs") {
      source_type = "doc";
    }

    const params = new URLSearchParams({ query, type: searchType, limit: "40" });
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
      if (activeTab === "issues_prs" && subType === "all") {
        filteredResults = filteredResults.filter(
          (r) => r.source_type === "issue" || r.source_type === "pr_review"
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

  useEffect(() => {
    if (query.trim()) handleSearch();
  }, [activeTab, subType]);

  const togglePreview = async (index, type, item) => {
    if (previews[index]) {
      setPreviews((prev) => {
        const copy = { ...prev };
        delete copy[index];
        return copy;
      });
      return;
    }
    setPreviewLoading((prev) => ({ ...prev, [index]: true }));
    try {
      if (type === "code") {
        const start = Math.max(1, (item.line || 1) - 15);
        const end = (item.line || 1) + 15;
        const params = new URLSearchParams({
          path: item.path,
          repo: item.repo,
          start_line: start.toString(),
          end_line: end.toString(),
        });
        const res = await fetch(`/api/read_file?${params}`);
        if (!res.ok) throw new Error("Failed to load preview");
        const data = await res.json();
        setPreviews((prev) => ({ ...prev, [index]: { type: "code", data, startLine: start } }));
      } else if (type === "timeline") {
        const params = new URLSearchParams({
          number: item.issue_no.toString(),
          repo: item.repo,
        });
        const res = await fetch(`/api/issue?${params}`);
        if (!res.ok) throw new Error("Failed to load timeline");
        const data = await res.json();
        setPreviews((prev) => ({ ...prev, [index]: { type: "timeline", data } }));
      }
    } catch (err) {
      console.error(err);
      alert(err.message);
    } finally {
      setPreviewLoading((prev) => ({ ...prev, [index]: false }));
    }
  };

  return (
    <div className="search-view-container">
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
            <ChevronDown
              size={14}
              style={{
                transform: showFilters ? "rotate(180deg)" : "none",
                transition: "transform 0.2s",
              }}
            />
          </button>
        </div>

        <SearchFilters
          showFilters={showFilters}
          repo={repo}
          setRepo={setRepo}
          pathPrefix={pathPrefix}
          setPathPrefix={setPathPrefix}
          progLang={progLang}
          setProgLang={setProgLang}
          activeTab={activeTab}
          repos={repos}
        />
      </form>

      <SearchTabs
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        subType={subType}
        setSubType={setSubType}
      />

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

          <SearchResultList
            results={results}
            query={query}
            previews={previews}
            previewLoading={previewLoading}
            togglePreview={togglePreview}
          />
        </>
      )}
    </div>
  );
};

export default SearchView;
