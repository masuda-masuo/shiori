import React from "react";

const SearchFilters = ({
  showFilters,
  repo,
  setRepo,
  pathPrefix,
  setPathPrefix,
  progLang,
  setProgLang,
  activeTab,
  repos = [],
}) => {
  if (!showFilters) return null;

  return (
    <div className="advanced-filters">
      <div className="filter-field">
        <label htmlFor="filter-repo">Repository</label>
        <select
          id="filter-repo"
          value={repo}
          onChange={(e) => setRepo(e.target.value)}
        >
          <option value="*all*">All Repositories</option>
          {repos.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
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
  );
};

export default SearchFilters;
