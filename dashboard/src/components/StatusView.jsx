import React, { useState, useEffect } from "react";
import { Activity, RefreshCw, AlertTriangle, CheckCircle } from "lucide-react";

const formatAge = (seconds) => {
  if (seconds === null || seconds === undefined) return "N/A";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
};

const StatusView = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchStatus = async () => {
    try {
      setError(null);
      const res = await fetch("/api/status");
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || err.error || `HTTP ${res.status}`);
      }
      const json = await res.json();
      setData(json);
    } catch (err) {
      console.error("Failed to fetch status:", err);
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 60000);
    return () => clearInterval(interval);
  }, []);

  if (loading && !data) {
    return (
      <div className="loading-container">
        <p>Loading status...</p>
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="error-container">
        <p>Failed to load status: {error}</p>
        <button
          type="button"
          className="btn-refresh-status"
          style={{ marginTop: "1rem" }}
          onClick={fetchStatus}
        >
          <RefreshCw size={14} /> Retry
        </button>
      </div>
    );
  }

  const summary = data?.summary || {};
  const repos = data?.repos || {};
  const unhealthyCount = summary.unhealthy_repos ?? Object.keys(repos).length;
  const totalRepos = summary.total_repos ?? 0;
  const healthyRepos = summary.healthy_repos ?? (totalRepos - unhealthyCount);
  const pendingTotal = summary.pending_total ?? 0;
  const oldestRepo = summary.oldest_repo;
  const tokenProvider = data?.token_provider || "N/A";
  const chunkSource = summary.chunk_counts_source || "N/A";
  const omittedCount = summary.omitted_repos || 0;
  const omittedNames = summary.omitted_repo_names || [];

  return (
    <div className="status-view-container">
      {/* 1. Header Line */}
      <div className="status-header-card">
        <div className="status-header-top">
          <div className="status-header-title">
            <Activity size={20} /> System Status Overview
          </div>
          <button
            type="button"
            className="btn-refresh-status"
            onClick={fetchStatus}
          >
            <RefreshCw size={14} /> Refresh
          </button>
        </div>

        {error && (
          <div className="error-container" style={{ padding: "0.5rem 1rem" }}>
            <p>Update warning: {error}</p>
          </div>
        )}

        <div className="status-metrics-grid">
          <div className="status-metric-item">
            <span className="status-metric-label">Healthy / Total</span>
            <span className="status-metric-value">
              {healthyRepos} / {totalRepos}
            </span>
          </div>

          <div className="status-metric-item">
            <span className="status-metric-label">Pending Items</span>
            <span className="status-metric-value">{pendingTotal}</span>
          </div>

          <div className="status-metric-item">
            <span className="status-metric-label">Oldest Sync</span>
            <span className="status-metric-value">
              {oldestRepo
                ? `${oldestRepo.repo} (${formatAge(oldestRepo.age_seconds)} ago)`
                : "N/A"}
            </span>
          </div>

          <div className="status-metric-item">
            <span className="status-metric-label">Token Provider</span>
            <span className="status-metric-value">{tokenProvider}</span>
          </div>

          <div className="status-metric-item">
            <span className="status-metric-label">Chunk Source</span>
            <span className="status-metric-value">{chunkSource}</span>
          </div>
        </div>
      </div>

      {/* 2. Degraded block or All healthy */}
      {unhealthyCount > 0 ? (
        <div className="status-degraded-block">
          <div className="status-degraded-title">
            <AlertTriangle size={20} /> Degraded Repositories ({unhealthyCount})
          </div>

          <div className="degraded-repo-list">
            {Object.entries(repos).map(([repoName, repoData]) => {
              const {
                index_stale,
                never_indexed,
                consecutive_failures,
                last_error,
                pending_count,
                warnings = [],
              } = repoData;

              return (
                <div key={repoName} className="degraded-repo-card">
                  <div className="degraded-repo-name">{repoName}</div>

                  <div className="degraded-conditions">
                    {index_stale && (
                      <span className="condition-badge stale">Index Stale</span>
                    )}
                    {never_indexed && (
                      <span className="condition-badge never-indexed">
                        Never Indexed
                      </span>
                    )}
                    {consecutive_failures > 0 && (
                      <span className="condition-badge failing">
                        Failing ({consecutive_failures} failure
                        {consecutive_failures > 1 ? "s" : ""})
                      </span>
                    )}
                    {pending_count > 0 && (
                      <span className="condition-badge pending">
                        {pending_count} Pending
                      </span>
                    )}
                  </div>

                  {last_error && (
                    <div className="degraded-error-text">
                      <strong>Last Error:</strong> {last_error}
                    </div>
                  )}

                  {warnings.length > 0 && (
                    <div className="degraded-warnings-list">
                      {warnings.map((w, idx) => (
                        <div key={idx}>&bull; {w}</div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {omittedCount > 0 && (
            <div className="omitted-degraded-repos">
              {omittedCount} more degraded {omittedCount === 1 ? "repo" : "repos"} not shown:{" "}
              {omittedNames.join(", ")}
            </div>
          )}
        </div>
      ) : (
        <div className="status-healthy-block">
          <CheckCircle size={20} /> All repos healthy
        </div>
      )}

      {/* 3. Raw JSON details for debugging */}
      <details className="status-details-json">
        <summary>Raw Status Data (JSON)</summary>
        <pre>{JSON.stringify(data, null, 2)}</pre>
      </details>
    </div>
  );
};

export default StatusView;
