import mermaid from 'mermaid';
import { JSDOM } from 'jsdom';

const dom = new JSDOM(`<!DOCTYPE html><div id="container"></div>`);
global.window = dom.window;
global.document = window.document;

mermaid.initialize({ startOnLoad: false });

const chart = `
mindmap
  root((masuda-masuo/shiori))
      src
        shiori
          __init__.py
          mcp_server.py
`;
try {
  const result = await mermaid.render('id', chart);
  console.log("Success");
} catch (e) {
  console.error(e.message);
}
