import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const sourceUrl = (name) => new URL(`../src/app/components/pages/${name}`, import.meta.url);
const readSource = (url) => readFileSync(url, "utf8");

test("行情页与研究页使用互斥条件渲染", () => {
  const appSource = readSource(new URL("../src/app/App.tsx", import.meta.url));
  const usesConditionalBranches =
    /\{\s*page\s*===\s*["']market["']\s*&&\s*\([\s\S]*?<MarketView\b/.test(appSource) &&
    /\{\s*page\s*===\s*["']research["']\s*&&\s*\([\s\S]*?<ResearchTasks\b/.test(appSource);
  const usesSwitchBranches =
    /switch\s*\(\s*page\s*\)/.test(appSource) &&
    /case\s*["']market["']\s*:[\s\S]*?return\s*\([\s\S]*?<MarketView\b/.test(appSource) &&
    /case\s*["']research["']\s*:[\s\S]*?return\s*\([\s\S]*?<ResearchTasks\b/.test(appSource);

  assert.doesNotMatch(appSource, /className=\{vis\(["'](?:market|research)["']\)\}/);
  assert.equal(
    usesConditionalBranches || usesSwitchBranches,
    true,
    "MarketView 与 ResearchTasks 必须按当前 page 互斥挂载",
  );
});

test("研究页面及兼容组件不得依赖 MarketView", () => {
  const researchComponents = ["ResearchTasks.tsx", "ResearchStation.tsx"];

  for (const name of researchComponents) {
    const url = sourceUrl(name);
    if (!existsSync(url)) continue;

    assert.doesNotMatch(
      readSource(url),
      /(?:import|export)[^;]*from\s*["'][^"']*MarketView["']/,
      `${name} 不得导入或转出 MarketView`,
    );
  }
});

test("行情页不再提供生成研究报告入口", () => {
  const marketSource = readSource(sourceUrl("MarketView.tsx"));

  assert.doesNotMatch(marketSource, /生成研究报告/);
});
