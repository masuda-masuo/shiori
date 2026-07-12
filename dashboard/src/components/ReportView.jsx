import React, { useState, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import MermaidViewer from "./MermaidViewer";

const templates = {
  stats: { title: "Repository Stats", desc: "Codebase metrics and language distribution." },
  symbol_index: { title: "Symbol Index", desc: "Browse functions, classes, and other symbols." },
  api_reference: { title: "API Reference", desc: "Extracted docstrings and signatures." },
  module_tree: { title: "Module Tree", desc: "Directory structure and module dependencies." },
};

const ReportView = ({ view, repo }) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [searchQuery, setSearchQuery] = useState("");

  const tpl = templates[view] || { title: view, desc: "" };

  useEffect(() => {
    setLoading(true);
    setError(null);
    setData(null);
    setSearchQuery("");

    const params = new URLSearchParams({ template: view });
    if (repo) params.set("repo", repo);
    if (view === "symbol_index" || view === "module_tree") {
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