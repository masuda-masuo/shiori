import React from "react";
import { ExternalLink, User, Clock } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { highlightText, getSourceIcon, getSourceLabel } from "./searchUtils";

const SearchResultList = ({
  results = [],
  query,
  previews = {},
  previewLoading = {},
  togglePreview,
}) => {
  return (
    <div className="search-results-list">
      {results.length === 0 ? (
        query.trim() && (
          <div
            className="card"
            style={{
              textAlign: "center",
              padding: "3rem",
              color: "var(--text-secondary)",
            }}
          >
            No results found. Try adjusting your search term or filters.
          </div>
        )
      ) : (
        results.map((item, index) => {
          const isCode = item.source_type === "code";
          const isTimeline =
            item.source_type === "issue" || item.source_type === "pr_review";
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
                        ? `${item.path.split("/").pop()} : Line ${item.line}`
                        : item.heading_path ||
                          `#${item.issue_no} ${item.kind === "pr" ? "PR" : "Issue"}`}
                      <ExternalLink size={12} />
                    </a>
                    <span className="result-path">
                      {item.repo} &raquo; {item.path || "discussion"}
                    </span>
                  </div>
                </div>
                <div className="result-score-badge">Score: {item.score}</div>
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
                      <Clock size={12} />{" "}
                      {new Date(item.updated_at).toLocaleDateString()}
                    </span>
                  )}
                </div>

                {((hasCodePreview && item.line) || (hasTimeline && item.issue_no)) && (
                  <button
                    type="button"
                    className="btn-preview"
                    onClick={() =>
                      togglePreview(index, isCode ? "code" : "timeline", item)
                    }
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
                      {previews[index].data.content.split("\n").map((_, i) => (
                        <div
                          key={i}
                          className={
                            previews[index].startLine + i === item.line
                              ? "highlighted-line"
                              : ""
                          }
                        >
                          {previews[index].startLine + i}
                        </div>
                      ))}
                    </div>
                    <div className="preview-code-lines">
                      {previews[index].data.content.split("\n").map((line, i) => (
                        <div
                          key={i}
                          className={
                            previews[index].startLine + i === item.line
                              ? "highlighted-line"
                              : ""
                          }
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
                    {previews[index].data.items &&
                      previews[index].data.items.map((comment, cIdx) => (
                        <div key={cIdx} className="timeline-item">
                          <div className="timeline-header">
                            <span className="timeline-author">
                              <User size={10} /> {comment.author}{" "}
                              {comment.is_bot && (
                                <span
                                  className="badge"
                                  style={{
                                    fontSize: "0.65rem",
                                    padding: "0.05rem 0.25rem",
                                  }}
                                >
                                  bot
                                </span>
                              )}
                            </span>
                            <span>
                              {comment.created_at
                                ? new Date(comment.created_at).toLocaleString()
                                : ""}
                            </span>
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
  );
};

export default SearchResultList;
