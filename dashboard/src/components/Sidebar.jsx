import React from "react";
import { BarChart2, Hash, FileCode, GitFork, Search } from "lucide-react";

const Sidebar = ({ currentView, repos, currentRepo, onRepoChange }) => {
  const links = [
    { id: "search", label: "Search", icon: <Search size={18} /> },
    { id: "stats", label: "Stats", icon: <BarChart2 size={18} /> },
    { id: "symbol_index", label: "Symbols", icon: <Hash size={18} /> },
    { id: "api_reference", label: "API Reference", icon: <FileCode size={18} /> },
    { id: "module_tree", label: "Module Tree", icon: <GitFork size={18} /> }
  ];

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-icon">S</div>
        <h1>Shiori</h1>
      </div>
      
      {repos.length > 0 && (
        <div className="repo-selector">
          <label htmlFor="repo-select">Repository</label>
          <select 
            id="repo-select" 
            value={currentRepo} 
            onChange={(e) => onRepoChange(e.target.value)}
          >
            {repos.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
        </div>
      )}

      <ul className="nav-menu">
        {links.map(link => (
          <a 
            key={link.id}
            href={`#${link.id}`} 
            className={`nav-item ${currentView === link.id ? "active" : ""}`}
          >
            {link.icon} {link.label}
          </a>
        ))}
      </ul>
    </aside>
  );
};

export default Sidebar;