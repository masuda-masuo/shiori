import React, { useEffect, useRef } from "react";
import mermaid from "mermaid";
import svgPanZoom from "svg-pan-zoom";

mermaid.initialize({ startOnLoad: false, theme: "dark" });

const MermaidViewer = ({ chart }) => {
  const containerRef = useRef(null);

  useEffect(() => {
    const renderChart = async () => {
      if (containerRef.current) {
        containerRef.current.innerHTML = "";
        try {
          const { svg } = await mermaid.render("mermaid-svg-" + Date.now(), chart);
          containerRef.current.innerHTML = svg;
          
          const svgElement = containerRef.current.querySelector("svg");
          if (svgElement) {
            svgElement.style.width = "100%";
            svgElement.style.height = "600px";
            svgPanZoom(svgElement, {
              zoomEnabled: true,
              controlIconsEnabled: true,
              fit: true,
              center: true
            });
          }
        } catch (err) {
          console.error("Mermaid error:", err);
          containerRef.current.innerHTML = `<div class="error-container">Failed to render Mermaid chart.</div>`;
        }
      }
    };
    if (chart) {
      renderChart();
    }
  }, [chart]);

  return (
    <div className="card">
      <div 
        ref={containerRef} 
        style={{ width: "100%", height: "600px", overflow: "hidden", background: "rgba(0,0,0,0.2)", borderRadius: "8px" }} 
      />
    </div>
  );
};

export default MermaidViewer;