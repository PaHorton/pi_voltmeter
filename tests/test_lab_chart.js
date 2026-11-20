const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../web/lab.html"), "utf8");
const source = html.slice(html.indexOf("    function renderChart("), html.indexOf("    async function loadHistory("));

class Element {
  constructor(tag) {
    this.tag = tag;
    this.attributes = {};
    this.children = [];
    this.clientWidth = 390;
    this.namespaceURI = "http://www.w3.org/2000/svg";
    this.textContent = "";
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
}

function render(points) {
  const chart = new Element("div");
  const context = {
    document: {createElementNS: (_, tag) => new Element(tag)},
    el: (tag, cls, text) => Object.assign(new Element(tag), {className: cls, textContent: text}),
    state: {selected: "pressure"},
  };
  vm.runInNewContext(source, context);
  context.renderChart(chart, {points, unit: "Torr"});
  return chart;
}

test("chart labels values, engineering units and local time on narrow screens", () => {
  const chart = render([
    {time: "2026-11-20T12:00:00Z", value: 1.2e-7},
    {time: "2026-11-20T12:05:00Z", value: 2.4e-7},
  ]);
  const svg = chart.children[0];
  const labels = svg.children.filter(child => child.tag === "text").map(child => child.textContent);
  assert.equal(svg.attributes.viewBox, "0 0 374 280");
  assert.ok(labels.includes("Torr"));
  assert.ok(labels.includes("Local time"));
  assert.equal(labels.filter(label => /e-7/.test(label)).length, 5);
  assert.ok(svg.children.some(child => child.attributes.class === "axis-boundary"));
  assert.ok(svg.children.some(child => child.tag === "polyline"));
  assert.match(svg.attributes["aria-label"], /Torr versus local time/);
});

test("chart draws isolated values without bridging invalid gaps", () => {
  const chart = render([
    {time: "2026-11-20T12:00:00Z", value: 1},
    {time: "2026-11-20T12:01:00Z", value: null},
    {time: "2026-11-20T12:02:00Z", value: 2},
  ]);
  const elements = chart.children[0].children;
  assert.equal(elements.filter(child => child.tag === "circle").length, 2);
  assert.equal(elements.filter(child => child.tag === "polyline").length, 0);
});

test("chart reports when no compatible values exist", () => {
  const chart = render([{time: "2026-11-20T12:00:00Z", value: null}]);
  assert.equal(chart.children[0].textContent, "No compatible valid values in this time range.");
});
