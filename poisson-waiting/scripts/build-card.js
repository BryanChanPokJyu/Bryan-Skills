#!/usr/bin/env node
"use strict";

const fs = require("fs");

const EVIDENCE_LABELS = {
  customer_adoption: "客户在买 / 采用率上升",
  revenue_accelerating: "收入在加速",
  margin_intact: "利润率没崩",
  backlog_growing: "订单 / RPO / backlog 增长",
  industry_tailwind: "行业趋势配合",
  price_not_reflected: "价格还没完全反映",
};

const NARRATIVE_LABELS = {
  no_evidence_update: "无证据更新",
  no_falsifier: "无 falsifier",
  cost_basis_anchoring: "因成本价死扛",
  waited_long_enough_fallacy: "用“跌够久了”当理由",
  price_as_evidence: "用价格波动替代证据更新",
};

const LAMBDA_LEVELS = new Set(["低", "上升", "高", "下降"]);
const RISK_LEVELS = new Set(["低", "中", "高"]);
const ACTIONS = new Set(["准备建仓", "加仓", "持有等待", "减仓", "退出", "仅观察"]);

function fail(message) {
  console.error(`error: ${message}`);
  process.exit(1);
}

function schema() {
  return {
    ticker: "NVDA",
    company_name: "NVIDIA Corporation",
    date: "YYYY-MM-DD",
    waiting_event: "具体状态跳变事件",
    lambda_direction: "低 / 上升 / 高 / 下降",
    lambda_evidence: ["独立信号 1", "独立信号 2"],
    evidence: Object.fromEntries(Object.keys(EVIDENCE_LABELS).map((key) => [key, true])),
    evidence_notes: Object.fromEntries(Object.keys(EVIDENCE_LABELS).map((key) => [key, "数据与判断依据"])),
    upside_odds: 30,
    downside_loss: 15,
    falsifiers: {
      thesis_wrong: "什么情况说明 thesis 错了",
      state_transition_failed: "什么情况说明状态迁移失败",
      capital_stagnation: "什么情况说明等待已变成资本钝化",
    },
    risk_costs: {
      carry: "低 / 中 / 高",
      fragility: "低 / 中 / 高",
      opportunity: "低 / 中 / 高",
      false_positive: "低 / 中 / 高",
    },
    narrative_check: Object.fromEntries(Object.keys(NARRATIVE_LABELS).map((key) => [key, false])),
    action: "准备建仓 / 加仓 / 持有等待 / 减仓 / 退出 / 仅观察",
    reason: "一句话理由，必须落在 lambda 和证据上",
    review_clock: "下次复盘日期或触发条件",
    _data_snapshot: "可选：保留 fetch-data.py 输出的 Yahoo Finance 快照",
  };
}

function parseArgs(argv) {
  const args = { input: null, output: null, allowDraft: false, showSchema: false };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--schema") args.showSchema = true;
    else if (arg === "--allow-draft") args.allowDraft = true;
    else if (arg === "--in") args.input = argv[++index];
    else if (arg === "--out") args.output = argv[++index];
    else fail(`unknown argument: ${arg}`);
  }
  return args;
}

function readInput(path) {
  const text = path ? fs.readFileSync(path, "utf8") : fs.readFileSync(0, "utf8");
  try {
    return JSON.parse(text);
  } catch (error) {
    fail(`invalid JSON input: ${error.message}`);
  }
}

function isBlank(value) {
  return value === null || value === undefined || value === "" ||
    (typeof value === "string" && (value.trim() === "" || value.trim().startsWith("FILL:")));
}

function requireFilled(errors, path, value) {
  if (isBlank(value)) errors.push(`${path} is not filled`);
}

function validate(card, allowDraft) {
  const errors = [];
  requireFilled(errors, "ticker", card.ticker);
  requireFilled(errors, "company_name", card.company_name);
  requireFilled(errors, "date", card.date);
  requireFilled(errors, "waiting_event", card.waiting_event);
  requireFilled(errors, "lambda_direction", card.lambda_direction);
  if (!isBlank(card.lambda_direction) && !LAMBDA_LEVELS.has(card.lambda_direction)) {
    errors.push("lambda_direction must be one of: 低 / 上升 / 高 / 下降");
  }
  if (typeof card.lambda_direction === "number" || /\d/.test(String(card.lambda_direction || ""))) {
    errors.push("lambda_direction must not be numeric");
  }
  if (!Array.isArray(card.lambda_evidence) || card.lambda_evidence.length < 2 ||
      card.lambda_evidence.some(isBlank)) {
    errors.push("lambda_evidence must contain at least two independent filled signals");
  }
  for (const key of Object.keys(EVIDENCE_LABELS)) {
    if (typeof (card.evidence || {})[key] !== "boolean") {
      errors.push(`evidence.${key} must be true or false`);
    }
    requireFilled(errors, `evidence_notes.${key}`, (card.evidence_notes || {})[key]);
  }
  if (!(typeof card.upside_odds === "number" && card.upside_odds >= 0)) {
    errors.push("upside_odds must be a non-negative number");
  }
  if (!(typeof card.downside_loss === "number" && card.downside_loss > 0)) {
    errors.push("downside_loss must be a positive number");
  }
  for (const key of ["thesis_wrong", "state_transition_failed", "capital_stagnation"]) {
    requireFilled(errors, `falsifiers.${key}`, (card.falsifiers || {})[key]);
  }
  for (const key of ["carry", "fragility", "opportunity", "false_positive"]) {
    const value = (card.risk_costs || {})[key];
    requireFilled(errors, `risk_costs.${key}`, value);
    if (!isBlank(value) && !RISK_LEVELS.has(value)) {
      errors.push(`risk_costs.${key} must be one of: 低 / 中 / 高`);
    }
  }
  requireFilled(errors, "action", card.action);
  if (!isBlank(card.action) && !ACTIONS.has(card.action)) {
    errors.push("action must be one of: 准备建仓 / 加仓 / 持有等待 / 减仓 / 退出 / 仅观察");
  }
  requireFilled(errors, "reason", card.reason);
  requireFilled(errors, "review_clock", card.review_clock);
  if (errors.length && !allowDraft) fail(errors.join("\n- "));
  return errors;
}

function evidenceDensity(evidence) {
  const count = Object.keys(EVIDENCE_LABELS).filter((key) => evidence[key] === true).length;
  const label = count >= 5 ? "已达临界密度" : count >= 3 ? "接近临界密度" : "远离临界密度";
  return { count, label };
}

function payoff(card) {
  if (!(typeof card.upside_odds === "number") || !(typeof card.downside_loss === "number") ||
      card.downside_loss <= 0) {
    return { ratio: null, level: "待补充" };
  }
  const ratio = card.upside_odds / card.downside_loss;
  return { ratio, level: ratio >= 2 ? "高" : "低" };
}

function lambdaForQuadrant(direction) {
  if (direction === "高" || direction === "上升") return "高";
  if (direction === "低" || direction === "下降") return "低";
  return "待补充";
}

function qualitativeEv(card, density, payoffResult) {
  const riskValues = Object.values(card.risk_costs || {});
  const highCosts = riskValues.filter((value) => value === "高").length;
  const lambda = lambdaForQuadrant(card.lambda_direction);
  if (lambda === "高" && density.count >= 4 && payoffResult.level === "高" && highCosts === 0) {
    return "正：lambda、证据密度与赔率同时支持继续研究或准备行动";
  }
  if (card.lambda_direction === "下降" || density.count <= 2 || highCosts >= 2) {
    return "负：证据密度、lambda 方向或等待成本不足以支持继续占用资本";
  }
  return "中性：尚未形成足够密集的证据网络，维持纪律性观察并等待触发条件";
}

function narrativeWarnings(card) {
  const check = card.narrative_check || {};
  const warnings = Object.keys(NARRATIVE_LABELS)
    .filter((key) => check[key] === true)
    .map((key) => NARRATIVE_LABELS[key]);
  if (Object.values(card.falsifiers || {}).some(isBlank) && !warnings.includes("无 falsifier")) {
    warnings.push("无 falsifier");
  }
  return warnings;
}

function fmt(value, fallback = "待补充") {
  return isBlank(value) ? fallback : String(value);
}

function render(card, draftErrors) {
  const density = evidenceDensity(card.evidence || {});
  const payoffResult = payoff(card);
  const quadrant = `${lambdaForQuadrant(card.lambda_direction)} lambda + ${payoffResult.level} payoff`;
  const warnings = narrativeWarnings(card);
  const lambdaEvidence = Array.isArray(card.lambda_evidence) ? card.lambda_evidence : [];
  const lines = [];

  lines.push(`# Poisson Waiting Card — ${fmt(card.ticker)} ${fmt(card.company_name, "")} — ${fmt(card.date)}`);
  if (draftErrors.length) {
    lines.push("", "⚠️ 草稿：以下字段尚未填写完整：");
    for (const error of draftErrors) lines.push(`- ${error}`);
  }
  lines.push("", "## 1. 在等什么事件", fmt(card.waiting_event, "⚠️ 无明确事件，不是等待"));
  lines.push("", "## 2. λ 方向", `**${fmt(card.lambda_direction)}**`);
  for (const item of lambdaEvidence) lines.push(`- ${fmt(item)}`);
  lines.push("", "## 3. 证据网络（是否接近临界密度）");
  for (const [key, label] of Object.entries(EVIDENCE_LABELS)) {
    const marked = (card.evidence || {})[key] === true ? "x" : " ";
    lines.push(`- [${marked}] ${label}：${fmt((card.evidence_notes || {})[key])}`);
  }
  lines.push(``, `→ 判定：**${density.label}**（${density.count}/6）`);
  lines.push("", "## 4. 赔率不对称");
  lines.push(`- 上行赔率：${fmt(card.upside_odds)}%`);
  lines.push(`- 下行损失：${fmt(card.downside_loss)}%`);
  lines.push(`- 上行赔率 ÷ 下行损失：${payoffResult.ratio === null ? "待补充" : payoffResult.ratio.toFixed(2)}`);
  lines.push("", "## 5. 证伪条件（falsifier）");
  lines.push(`- 什么情况说明我错了：${fmt((card.falsifiers || {}).thesis_wrong)}`);
  lines.push(`- 什么情况说明没有进入状态跳变：${fmt((card.falsifiers || {}).state_transition_failed)}`);
  lines.push(`- 什么情况说明等待已变成资本钝化：${fmt((card.falsifiers || {}).capital_stagnation)}`);
  lines.push("", "## 6. 四象限定位");
  lines.push(`- ${quadrant}`);
  lines.push(`- EV / 单位时间：${qualitativeEv(card, density, payoffResult)}`);
  lines.push("", "## 7. 叙事麻醉警报");
  if (warnings.length) {
    for (const warning of warnings) lines.push(`- ⚠️ ${warning}`);
  } else {
    lines.push("- 未触发已配置警报");
  }
  lines.push("", "## 8. 结论 + Review Clock");
  lines.push(`- 当前动作：${fmt(card.action)}`);
  lines.push(`- 一句话理由：${fmt(card.reason)}`);
  lines.push(`- 下次复盘时间 / 触发条件：${fmt(card.review_clock)}`);
  return `${lines.join("\n")}\n`;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.showSchema) {
    process.stdout.write(`${JSON.stringify(schema(), null, 2)}\n`);
    return;
  }
  const card = readInput(args.input);
  const errors = validate(card, args.allowDraft);
  const markdown = render(card, errors);
  if (args.output) fs.writeFileSync(args.output, markdown, "utf8");
  else process.stdout.write(markdown);
}

main();
