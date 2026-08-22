import React from "react";

const SearchTabs = ({ activeTab, setActiveTab, subType, setSubType }) => {
  return (
    <>
      <div className="search-tabs">
        <button
          type="button"
          className={`search-tab ${activeTab === "all" ? "active" : ""}`}
          onClick={() => setActiveTab("all")}
        >
          All
        </button>
        <button
          type="button"
          className={`search-tab ${activeTab === "issues_prs" ? "active" : ""}`}
          onClick={() => setActiveTab("issues_prs")}
        >
          Issues / PRs
        </button>
        <button
          type="button"
          className={`search-tab ${activeTab === "code" ? "active" : ""}`}
          onClick={() => setActiveTab("code")}
        >
          Code
        </button>
        <button
          type="button"
          className={`search-tab ${activeTab === "docs" ? "active" : ""}`}
          onClick={() => setActiveTab("docs")}
        >
          Docs
        </button>
      </div>

      {activeTab === "issues_prs" && (
        <div className="sub-type-toggles">
          <button
            type="button"
            className={`sub-type-btn ${subType === "all" ? "active" : ""}`}
            onClick={() => setSubType("all")}
          >
            All Threads
          </button>
          <button
            type="button"
            className={`sub-type-btn ${subType === "issue" ? "active" : ""}`}
            onClick={() => setSubType("issue")}
          >
            Issues Only
          </button>
          <button
            type="button"
            className={`sub-type-btn ${subType === "pr" ? "active" : ""}`}
            onClick={() => setSubType("pr")}
          >
            PRs Only
          </button>
        </div>
      )}
    </>
  );
};

export default SearchTabs;
