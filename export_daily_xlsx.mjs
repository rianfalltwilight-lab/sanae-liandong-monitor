#!/usr/bin/env node
/**
 * Export the collector's daily JSON to an editable Excel workbook.
 * Requires @oai/artifact-tool. If it is not on Node's module path, set
 * ARTIFACT_TOOL_MODULE to its absolute dist/artifact_tool.mjs path.
 * Usage: node export_daily_xlsx.mjs --input report.json --output daily.xlsx
 *        [--preview-dir previews] [--chart-png daily-chart.png]
 * No network calls, credentials, QQ operations, or data collection occur here.
 */
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const COLORS = ["#2563EB", "#D97706", "#059669", "#7C3AED", "#DB2777", "#0891B2", "#475569"];
const MONEY = '"¥"#,##0.00;[Red]-"¥"#,##0.00';
const PERCENT = '0.0%;[Red]-0.0%';
const DATE_TIME = 'yyyy-mm-dd hh:mm:ss';
const EXCEL_MAX_ROWS = 1048576;
const HEADER_ROW = 5;

export function literal(value) {
  if (value === null || value === undefined) return "";
  const text = String(value).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "");
  // Inputs are merchant-controlled. Keep formula-like titles literal in Excel.
  return /^[\s]*[=+\-@]/u.test(text) ? `'${text}` : text;
}

function numeric(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function col(index) {
  let result = "";
  for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) result = String.fromCharCode(65 + (n - 1) % 26) + result;
  return result;
}

function wallDate(value) {
  if (!value) return null;
  // Excel has no timezone type. Preserve the report's local wall time instead
  // of silently converting ISO +08:00 timestamps to UTC clock readings.
  const text = String(value);
  const match = /^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)/.exec(text);
  if (!match) throw new Error(`Invalid observation timestamp: ${text}`);
  const date = new Date(`${match[1]}T${match[2]}Z`);
  if (!Number.isFinite(date.getTime())) throw new Error(`Invalid observation timestamp: ${text}`);
  return date;
}

function clockLabel(value) {
  return String(value).replace("T", " ").slice(11, 19);
}

function wrapNote(text, width = 80) {
  const chars = [...String(text)];
  const lines = [];
  for (let i = 0; i < chars.length; i += width) lines.push(chars.slice(i, i + width).join(""));
  return lines.length ? lines : [""];
}

async function artifactModule() {
  const specifier = process.env.ARTIFACT_TOOL_MODULE;
  try {
    return await import(specifier ? (specifier.startsWith("file:") ? specifier : pathToFileURL(path.resolve(specifier)).href) : "@oai/artifact-tool");
  } catch (error) {
    throw new Error("Excel export needs @oai/artifact-tool; set ARTIFACT_TOOL_MODULE to its absolute dist/artifact_tool.mjs path. " + error.message);
  }
}

async function fontFamily() {
  if (process.env.XLSX_FONT_FAMILY) return process.env.XLSX_FONT_FAMILY;
  try {
    await fs.access(path.join(process.env.WINDIR || "C:\\Windows", "Fonts", "msyh.ttc"));
    return "Microsoft YaHei";
  } catch {
    return "Arial";
  }
}

function writeRows(sheet, startRow, rows) {
  if (!rows.length) return;
  const width = rows[0].length;
  if (startRow + rows.length - 1 > EXCEL_MAX_ROWS) throw new Error(`${sheet.name}: exceeds Excel's ${EXCEL_MAX_ROWS} row limit; split the source report before exporting.`);
  for (let i = 0; i < rows.length; i += 2000) {
    const block = rows.slice(i, i + 2000);
    sheet.getRangeByIndexes(startRow + i - 1, 0, block.length, width).values = block;
  }
}

function setupSheet(sheet, title, context, headers, rowCount, widths, font) {
  const last = Math.max(HEADER_ROW + rowCount, 8);
  const edge = col(headers.length - 1);
  const used = sheet.getRange(`A1:${edge}${last}`);
  used.format.font = { name: font, size: 10, color: "#172033" };
  used.format.rowHeight = 21;
  used.format.verticalAlignment = "center";
  sheet.showGridLines = false;
  sheet.getRange("A1").values = [[literal(title)]];
  sheet.getRange("A1").format.font = { name: font, size: 16, bold: true, color: "#172033" };
  sheet.getRange("A1").format.rowHeight = 30;
  sheet.getRange(`A2:${edge}2`).format.borders = { bottom: { style: "thin", color: "#94A3B8" } };
  sheet.getRange("A3").values = [[literal(context)]];
  sheet.getRange("A3").format.font = { name: font, size: 10, italic: true, color: "#64748B" };
  sheet.getRange(`A${HEADER_ROW}:${edge}${HEADER_ROW}`).values = [headers];
  sheet.getRange(`A${HEADER_ROW}:${edge}${HEADER_ROW}`).format = {
    fill: "#26364B", font: { name: font, size: 10, bold: true, color: "#FFFFFF" },
    rowHeight: 31, wrapText: true, horizontalAlignment: "center", verticalAlignment: "center",
    borders: { insideVertical: { style: "thin", color: "#FFFFFF" } },
  };
  widths.forEach((width, i) => { sheet.getRange(`${col(i)}1:${col(i)}${last}`).format.columnWidth = width; });
  if (rowCount) {
    const body = sheet.getRange(`A${HEADER_ROW + 1}:${edge}${HEADER_ROW + rowCount}`);
    body.format.borders = { insideHorizontal: { style: "thin", color: "#E9EDF2" } };
  }
  return { last, edge };
}

function formatColumn(sheet, index, rows, format) {
  if (rows) sheet.getRange(`${col(index)}6:${col(index)}${5 + rows}`).setNumberFormat(format);
}

function addNotes(sheet, firstRow, notes, font) {
  let row = firstRow;
  for (const note of notes) {
    for (const text of wrapNote(note)) {
      sheet.getRange(`A${row}`).values = [[literal(text)]];
      sheet.getRange(`A${row}`).format.font = { name: font, size: 10, color: "#475569" };
      sheet.getRange(`A${row}`).format.rowHeight = 23;
      row += 1;
    }
    row += 1;
  }
  return row;
}

function wideSeries(report, shops) {
  const map = new Map();
  const indexes = new Map(shops.map((shop, i) => [String(shop.shop_id), i + 2]));
  for (const point of report.series) {
    if (!indexes.has(String(point.shop_id))) throw new Error(`Series has an unknown shop id: ${point.shop_id}`);
    let row = map.get(point.at);
    if (!row) {
      row = [wallDate(point.at), literal(clockLabel(point.at)), ...shops.map(() => null)];
      map.set(point.at, row);
    }
    row[indexes.get(String(point.shop_id))] = point.success ? numeric(point.min_price) : null;
  }
  const result = [];
  let previousAt = null;
  let gapRows = 0;
  for (const [at, row] of [...map].sort(([a], [b]) => a.localeCompare(b))) {
    if (previousAt !== null && Date.parse(at) - Date.parse(previousAt) > 60000) {
      result.push([new Date(wallDate(previousAt).getTime() + 60000), "缺测间隔", ...shops.map(() => null)]);
      gapRows += 1;
    }
    result.push(row);
    previousAt = at;
  }
  return { rows: result, gapRows };
}

function buildChart(chartsSheet, priceSheet, rows, shops, shopIndex, chartIndex, font, report) {
  const end = 5 + rows;
  const ranges = [priceSheet.getRange(`B5:B${end}`)];
  if (shopIndex === null) {
    shops.forEach((_, i) => ranges.push(priceSheet.getRange(`${col(i + 2)}5:${col(i + 2)}${end}`)));
  } else ranges.push(priceSheet.getRange(`${col(shopIndex + 2)}5:${col(shopIndex + 2)}${end}`));
  // Isolated observations cannot form a continuous trend. Columns keep those
  // measured values visible without bridging failed/missing observations.
  const hasContinuousData = ranges.slice(1).some(range => {
    const values = range.values.slice(1).map(row => row[0]);
    return values.some((value, i) => i > 0 && typeof value === "number" && typeof values[i - 1] === "number");
  });
  const chart = chartsSheet.charts.add(hasContinuousData ? "line" : "bar", ranges);
  const startRow = 6 + Math.floor(chartIndex / 2) * 19;
  const startCol = chartIndex % 2 === 0 ? "A" : "K";
  const endCol = chartIndex % 2 === 0 ? "J" : "T";
  chart.setPosition(`${startCol}${startRow}`, `${endCol}${startRow + 17}`);
  const dateLabel = `${report.date}${report.partial ? "（部分时段）" : ""}`;
  chart.title = `${dateLabel} ${shopIndex === null ? "全部店铺" : shops[shopIndex].shop_name}在售最低价（元）`;
  chart.titleTextStyle.fontSize = 14;
  chart.titleTextStyle.typeface = font;
  chart.legend = { position: "bottom", textStyle: { typeface: font, fontSize: 10 } };
  chart.hasLegend = shopIndex === null;
  chart.xAxis = { axisType: "textAxis", textStyle: { typeface: font, fontSize: 10 } };
  chart.xAxis.title.text = "采样顺序（非等距时间；缺测留空）";
  chart.yAxis = { min: 0, numberFormatCode: '"¥"0.00', numberFormatSourceLinked: false, textStyle: { typeface: font, fontSize: 10 } };
  for (let i = 0; i < chart.series.items.length; i++) chart.series.items[i].fill = COLORS[(shopIndex === null ? i : shopIndex) % COLORS.length];
  return chart;
}

export async function buildWorkbook(report, { Workbook }, options = {}) {
  for (const key of ["shops", "products", "series", "observations", "analysis", "notes"]) {
    if (!Array.isArray(report[key])) throw new Error(`Daily report.${key} must be an array.`);
  }
  const font = options.font || await fontFamily();
  const workbook = Workbook.create();
  const summary = workbook.worksheets.add("日报");
  const products = workbook.worksheets.add("商品明细");
  const prices = workbook.worksheets.add("价格采样");
  const raw = workbook.worksheets.add("原始观测");
  const charts = workbook.worksheets.add("图表");
  const shops = report.shops;
  const timezone = report.timezone || "Asia/Shanghai";
  const scope = `${report.date} · ${timezone} · ${report.partial ? "部分时段数据，非完整全天" : "当日已记录时段"}`;

  const summaryRows = shops.map(s => [literal(s.shop_name), numeric(s.min_price), numeric(s.max_price), numeric(s.first_price), numeric(s.last_price), null,
    numeric(s.latest_stock_targets), numeric(s.attempts), numeric(s.successes), null, numeric(s.available_samples), wallDate(s.first_observed), wallDate(s.last_observed)]);
  setupSheet(summary, "链动小铺每日价格观察", scope,
    ["店铺", "最低报价", "最高报价", "首次最低价", "末次最低价", "首末变化", "最新有货商品数", "采集次数", "成功次数", "采集成功率", "有货样本", "首次观测", "末次观测"], summaryRows.length,
    [22, 13, 13, 14, 14, 13, 15, 11, 11, 13, 12, 28, 28], font);
  writeRows(summary, 6, summaryRows);
  const formulaRanges = [];
  if (summaryRows.length) {
    const count = summaryRows.length;
    const changes = summary.getRange(`F6:F${count + 5}`);
    changes.formulas = summaryRows.map((_, i) => [`=IF(OR(D${i + 6}="",E${i + 6}="",D${i + 6}=0),"",(E${i + 6}-D${i + 6})/D${i + 6})`]);
    const successRates = summary.getRange(`J6:J${count + 5}`);
    successRates.formulas = summaryRows.map((_, i) => [`=IF(OR(H${i + 6}="",I${i + 6}="",H${i + 6}=0),"",I${i + 6}/H${i + 6})`]);
    formulaRanges.push({ sheet: summary, range: `F6:F${count + 5}` }, { sheet: summary, range: `J6:J${count + 5}` });
    summary.getRange(`J6:J${count + 5}`).conditionalFormats.add("cellIs", { operator: "lessThan", formula: 0.95, format: { fill: "#FFF0CC", font: { color: "#854D0E" } } });
  }
  for (const c of [1, 2, 3, 4]) formatColumn(summary, c, summaryRows.length, MONEY);
  for (const c of [5, 9]) formatColumn(summary, c, summaryRows.length, PERCENT);
  for (const c of [11, 12]) formatColumn(summary, c, summaryRows.length, DATE_TIME);
  const notes = [
    `观察窗口：${report.first_observed || "暂无"} 至 ${report.last_observed || "暂无"}。`,
    `生成时间：${report.generated_at || "未提供"}。`,
    "统计口径：店铺走势为每轮已确认速刷 team5x、在售有库存商品的最低报价；不同商品、时长和配置混合，不能当作同规格涨跌。",
    "空白表示失败、无货或没有有效报价，绝不按零元处理。报价与库存仅为采样时点数据，库存下降不等于成交量。",
    ...report.analysis.map(text => `分析：${text}`),
    ...report.notes.map(text => `说明：${text}`),
    `表内时间采用 ${timezone} 本地时钟。价格采样与原始观测均保留全部输入行，未做时间降采样。`,
  ];
  const summaryEnd = addNotes(summary, summaryRows.length + 8, notes, font);

  const productRows = report.products.map(p => [literal(p.shop_name), literal(p.item_id), literal(p.edition), literal(p.title), numeric(p.first_price), numeric(p.last_price), null,
    numeric(p.min_price), numeric(p.max_price), numeric(p.price_changes), numeric(p.first_stock), numeric(p.last_stock), numeric(p.observations), wallDate(p.first_observed), wallDate(p.last_observed), literal(p.url)]);
  setupSheet(products, "商品明细（按标题版本区分）", "同一商品改标题后分开记录；首末变化只比较本行版本。来源链接为采集时的商品地址。",
    ["店铺", "商品 ID", "标题版本", "商品标题", "首次报价", "末次报价", "首末变化", "最低价", "最高价", "价格变化次数", "首次库存", "末次库存", "观测次数", "首次观测", "末次观测", "商品来源链接"],
    productRows.length, [20, 22, 18, 62, 13, 13, 13, 13, 13, 15, 12, 12, 12, 28, 28, 58], font);
  writeRows(products, 6, productRows);
  if (productRows.length) {
    const changes = products.getRange(`G6:G${productRows.length + 5}`);
    changes.formulas = productRows.map((_, i) => [`=IF(OR(E${i + 6}="",F${i + 6}="",E${i + 6}=0),"",(F${i + 6}-E${i + 6})/E${i + 6})`]);
    formulaRanges.push({ sheet: products, range: `G6:G${productRows.length + 5}` });
    products.getRange(`D6:D${productRows.length + 5}`).format.wrapText = true;
    products.getRange(`A6:P${productRows.length + 5}`).format.rowHeight = 42;
  }
  for (const c of [4, 5, 7, 8]) formatColumn(products, c, productRows.length, MONEY);
  formatColumn(products, 6, productRows.length, PERCENT);
  for (const c of [13, 14]) formatColumn(products, c, productRows.length, DATE_TIME);
  products.freezePanes.freezeRows(5);

  const { rows: priceRows, gapRows } = wideSeries(report, shops);
  setupSheet(prices, "各店在售目标最低价采样", `逐采样时刻展开，原始输入全保留；失败留空；全店缺测超过60秒插入空白断点（${gapRows}行），不补价格。`,
    ["观测时刻", "图表时间标签", ...shops.map(s => literal(s.shop_name))], priceRows.length, [28, 18, ...shops.map(() => 18)], font);
  writeRows(prices, 6, priceRows);
  formatColumn(prices, 0, priceRows.length, DATE_TIME);
  for (let i = 0; i < shops.length; i++) formatColumn(prices, i + 2, priceRows.length, MONEY);
  prices.freezePanes.freezeRows(5);

  const rawRows = report.observations.map(o => [wallDate(o.at), literal(o.shop_name), literal(o.shop_id), literal(o.item_id), literal(o.edition), literal(o.title), numeric(o.price), numeric(o.stock),
    o.active == null ? null : Boolean(o.active), o.target == null ? null : Boolean(o.target), o.classification_pending == null ? null : Boolean(o.classification_pending), literal(o.url)]);
  setupSheet(raw, "原始观测与来源", "保留全部输入观测。目标=true 表示已确认速刷 team5x；待分类=true 的记录不用于已确认目标报价。",
    ["观测时刻", "店铺", "店铺 ID", "商品 ID", "标题版本", "采样商品标题", "报价", "库存", "在售", "目标", "待分类", "商品来源链接"], rawRows.length,
    [28, 20, 20, 22, 18, 62, 13, 12, 10, 10, 10, 58], font);
  writeRows(raw, 6, rawRows);
  formatColumn(raw, 0, rawRows.length, DATE_TIME);
  formatColumn(raw, 6, rawRows.length, MONEY);
  if (rawRows.length) {
    raw.getRange(`F6:F${rawRows.length + 5}`).format.wrapText = true;
    raw.getRange(`A6:L${rawRows.length + 5}`).format.rowHeight = 42;
  }
  raw.freezePanes.freezeRows(5);

  charts.showGridLines = false;
  charts.getRange("A1:T82").format.font = { name: font, size: 10, color: "#172033" };
  charts.getRange("A1:T82").format.columnWidth = 10;
  charts.getRange("A1:T82").format.rowHeight = 23;
  charts.getRange("A1").values = [["店铺价格走势"]];
  charts.getRange("A1").format.font = { name: font, size: 16, bold: true };
  charts.getRange("A2").values = [[literal(scope)]];
  charts.getRange("A3").values = [["图表直接绑定「价格采样」单元格；空白不补零。最低价可能因商品、时长或配置组合变化而变化。"]];
  charts.getRange("A4").values = [["横轴按采样顺序，非等距时间；全店缺测超60秒断线。孤立点见采样表；无连续点时用柱形显示。"]];
  const chartObjects = [];
  if (priceRows.length && shops.length) {
    chartObjects.push(buildChart(charts, prices, priceRows.length, shops, null, 0, font, report));
    shops.forEach((_, i) => chartObjects.push(buildChart(charts, prices, priceRows.length, shops, i, i + 1, font, report)));
  } else charts.getRange("A6").values = [["当前报告没有价格采样，暂无可绘制数据。"]];

  // Inspect both formula ranges and workbook structure before producing a file.
  const inspection = [];
  for (const entry of formulaRanges) {
    inspection.push(await workbook.inspect({ kind: "formula", sheetId: entry.sheet.name, range: entry.range, maxChars: 1500, options: { maxResults: 5 } }));
    for (const [r, row] of entry.sheet.getRange(entry.range).values.entries()) {
      for (const value of row) {
        if (typeof value === "string" && /^#(?:REF!|DIV\/0!|VALUE!|N\/A|NAME\?|NUM!|NULL!|SPILL!|CALC!)/.test(value)) {
          throw new Error(`Formula error in ${entry.sheet.name}!${entry.range}, offset ${r}: ${value}`);
        }
      }
    }
  }
  inspection.push(await workbook.inspect({ kind: "workbook,sheet,table", maxChars: 4000, tableMaxRows: 2, tableMaxCols: 4 }));
  inspection.push(await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!", options: { useRegex: true, maxResults: 100 }, summary: "final formula error scan" }));
  const bindings = chartObjects.map(chart => ({ title: chart.title.text, series: chart.series.items.map(series => ({ values: series.formula, categories: series.categoryFormula })) }));
  if (bindings.some(chart => chart.series.some(series => !series.values || !series.categories))) throw new Error("An editable chart is missing its source-cell references.");
  return { workbook, font, inspection, bindings, summaryEnd, counts: { shops: shops.length, products: productRows.length, priceSamples: priceRows.length - gapRows, gapRows, observations: rawRows.length, charts: chartObjects.length } };
}

async function atomicWrite(file, bytes) {
  const temporary = `${file}.${process.pid}.tmp`;
  await fs.mkdir(path.dirname(file), { recursive: true });
  try {
    await fs.writeFile(temporary, bytes);
    await fs.rename(temporary, file);
  } finally {
    await fs.rm(temporary, { force: true });
  }
}

function argumentsFrom(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    if (!["--input", "--output", "--preview-dir", "--chart-png"].includes(argv[i]) || !argv[i + 1]) throw new Error("Usage: node export_daily_xlsx.mjs --input report.json --output daily.xlsx [--preview-dir previews] [--chart-png daily-chart.png]");
    args[argv[i].slice(2)] = argv[i + 1];
  }
  if (!args.input || !args.output) throw new Error("--input and --output are required.");
  return args;
}

export async function main(argv = process.argv.slice(2)) {
  const args = argumentsFrom(argv);
  const source = JSON.parse((await fs.readFile(args.input, "utf8")).replace(/^\uFEFF/, ""));
  const artifact = await artifactModule();
  const built = await buildWorkbook(source, artifact);
  const outputPath = path.resolve(args.output);
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  const output = await artifact.SpreadsheetFile.exportXlsx(built.workbook);
  const temporary = `${outputPath}.${process.pid}.tmp.xlsx`;
  try {
    await output.save(temporary);
    await fs.rename(temporary, outputPath);
  } finally {
    await fs.rm(temporary, { force: true });
    await fs.rm(`${temporary}.inspect.ndjson`, { force: true });
  }
  if (args["preview-dir"]) {
    const previewDir = path.resolve(args["preview-dir"]);
    await fs.mkdir(previewDir, { recursive: true });
    for (const [sheetName, range] of [["日报", `A1:M${Math.min(built.summaryEnd, 42)}`], ["商品明细", "A1:P13"], ["价格采样", `A1:${col(Math.max(2, built.counts.shops + 1))}17`], ["原始观测", "A1:L13"]]) {
      const preview = await built.workbook.render({ sheetName, range, scale: 1.25, format: "png" });
      await atomicWrite(path.join(previewDir, `${sheetName}.png`), new Uint8Array(await preview.arrayBuffer()));
    }
    for (let page = 0; page < Math.max(1, Math.ceil(built.counts.charts / 2)); page++) {
      const start = page === 0 ? 1 : 6 + page * 19;
      const end = 23 + page * 19;
      const preview = await built.workbook.render({ sheetName: "图表", range: `A${start}:T${end}`, scale: 1.25, format: "png" });
      await atomicWrite(path.join(previewDir, `图表-${page + 1}.png`), new Uint8Array(await preview.arrayBuffer()));
    }
    await atomicWrite(path.join(previewDir, "xlsx-inspection.json"), JSON.stringify({ font: built.font, counts: built.counts, bindings: built.bindings, inspection: built.inspection }, null, 2));
  }
  if (args["chart-png"]) {
    const chartPath = path.resolve(args["chart-png"]);
    await fs.mkdir(path.dirname(chartPath), { recursive: true });
    const preview = await built.workbook.render({ sheetName: "图表", range: built.counts.charts ? "A6:J23" : "A1:J8", scale: 1.5, format: "png" });
    await atomicWrite(chartPath, new Uint8Array(await preview.arrayBuffer()));
  }
  console.log(JSON.stringify({ output: outputPath, font: built.font, ...built.counts, formulaErrors: 0 }));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(`Daily XLSX export failed: ${error.message}`); process.exitCode = 1; });
}
