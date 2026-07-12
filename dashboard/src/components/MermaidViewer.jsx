import React, { useEffect, useRef } from "react";
import mermaid from "mermaid";
import svgPanZoom from "svg-pan-zoom";

const MermaidViewer = ({ chart }) => {
  const containerRef = useRef(null);

  useEffect(() => {
    mermaid.initialize({ 
      startOnLoad: false, 
      theme: "dark",
      parseError: (err) => {
        // Suppress Mermaid's default behavior of appending error divs to document.body
        console.warn("Suppressed Mermaid parse error:", err);
      }
    });
  }, []);

  useEffect(() => {
    const renderChart = async () => {
      if (containerRef.current) {
        containerRef.current.innerHTML = "";
        try {
          // Validate syntax first to avoid rendering glitches
          await mermaid.parse(chart);

          const uniqueId = "mermaid-svg-" + Math.random().toString(36).substring(2, 9);
          const { svg } = await mermaid.render(uniqueId, chart);
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
          containerRef.current.innerHTML = `<div class="error-container" style="padding: 2rem; text-align: center; color: #ef4444;">Failed to render Mermaid chart. Check syntax or repo structure.</div>`;
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