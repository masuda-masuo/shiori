import React, { useState, useEffect } from "react";
import Sidebar from "./components/Sidebar";
import ReportView from "./components/ReportView";

const App = () => {
  const [view, setView] = useState("search");
  const [repos, setRepos] = useState([]);
  const [currentRepo, setCurrentRepo] = useState("");

  useEffect(() => {
    // Initial load: parse hash for view
    const handleHash = () => {
      const hash = window.location.hash.slice(1);
      if (["search", "stats", "symbol_index", "api_reference", "module_tree"].includes(hash)) {
        setView(hash);
      }
    };
    handleHash();
    window.addEventListener("hashchange", handleHash);

    // Fetch config to get repos
    fetch("/api/repos")
      .then(res => res.json())
      .then(data => {
        // Mocking repo list extraction from somewhere, or just use one for now
        // For now, if the API doesn"t return repos list, we just default to empty
        if (data && data.repos) {
           setRepos(data.repos);
           setCurrentRepo(data.repos[0]);
        }
      })
      .catch(console.error);

    return () => window.removeEventListener("hashchange", handleHash);
  }, []);

  return (
    <>
      <Sidebar 
        currentView={view} 
        repos={repos} 
        currentRepo={currentRepo} 
        onRepoChange={setCurrentRepo} 
      />
      <main className="main-content">
        <ReportView view={view} repo={currentRepo} repos={repos} />
      </main>
    </>
  );
};

export default App;