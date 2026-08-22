import React from "react";
import { FileCode, FileText, GitPullRequest, MessageSquare } from "lucide-react";

export const highlightText = (text, queryStr) => {
  if (!queryStr || !text) return text;
  const tokens = queryStr.split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return text;

  const escapedTokens = tokens.map((t) => t.replace(/[-\/\\^$*+?.()|[\]{}]/g, "\\$&"));
  const regex = new RegExp(`(${escapedTokens.join("|")})`, "gi");

  const parts = text.split(regex);
  return parts.map((part, idx) =>
    regex.test(part)
      ? React.createElement("mark", { key: idx, className: "search-highlight" }, part)
      : part
  );
};

export const getSourceIcon = (source_type, kind) => {
  if (source_type === "code") {
    return React.createElement(FileCode, { size: 18, className: "icon-code" });
  }
  if (source_type === "doc") {
    return React.createElement(FileText, { size: 18, className: "icon-doc" });
  }
  if (kind === "pr" || source_type === "pr_review") {
    return React.createElement(GitPullRequest, { size: 18, className: "icon-pr" });
  }
  return React.createElement(MessageSquare, { size: 18, className: "icon-issue" });
};

export const getSourceLabel = (source_type, kind) => {
  if (source_type === "code") return "Code";
  if (source_type === "doc") return "Doc";
  if (source_type === "pr_review") return "PR Review";
  if (kind === "pr") return "Pull Request";
  return "Issue";
};
