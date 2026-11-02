import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Bar,
  BarChart,
  Brush,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import * as XLSX from "xlsx";

const API_URL = "/api/ask";
const DEFAULT_VISIBLE_POINTS = 100;
const themeStorageKey = "industrial-copilot-theme";
const historyStorageKey = "industrial-copilot-history";

const starterPrompts = [
  "Ποιά είναι η συνολική ενέργεια που καταναλώθηκε ανά τύπο μηχανής για την χθεσινή ημέρα;",
  "Ποιά είναι η μέση θερμοκρασία ολων των μηχανών ανά ώρα για τις προηγούμενες 3 ημέρες;",
  "Ποιές μηχανές χρειάζονται συντήρηση και έχουν υψηλό κίνδυνο;",
  "Πόσες ανωμαλίες έχουν καταγραφεί ανά τύπο μηχανής την προηγούμενη εβδομάδα;",
  "Ποιοί είναι οι πιο συχνοί τύποι βλάβης;",
  "Πως εξελίχθηκε η μέση υγρασία ανά ημέρα τις προηγούμενες 7 ημέρες;",
  "Ποιά είναι η μεγαλύτερη τιμή θερμοκρασίας και ποια η μικρότερη σήμερα και από ποιές μηχανές προέρχονται;",
];

const initialChat = {
  id: 1,
  title: "Factory Telemetry Session",
  snippet: "Ready for questions about the smart factory dataset.",
  timestamp: "Ready",
  messages: [
    {
      id: "assistant-welcome",
      role: "assistant",
      author: "Assistant",
      time: formatTime(new Date()),
      content:
        "Η εφαρμογή στέλνει ερωτήσεις στο τοπικό route /api/ask, το οποίο κάνει proxy στο backend FastAPI στο 127.0.0.1:8000/ask. Μπορεί να απαντήσει σε ερωτήσεις για τη βάση factory_telemetry_fixed και να επιστρέψει προαιρετικά δεδομένα για γράφημα.",
      chartData: null,
      status: "ready",
    },
  ],
};

const defaultHistoryData = {
  activeChatId: initialChat.id,
  chats: [initialChat],
};

function formatTime(date) {
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function getInitialTheme() {
  const storedTheme = window.localStorage.getItem(themeStorageKey);

  if (storedTheme === "light" || storedTheme === "dark") {
    return storedTheme;
  }

  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function getInitialHistory() {
  const storedHistory = window.localStorage.getItem(historyStorageKey);

  if (!storedHistory) {
    return defaultHistoryData;
  }

  try {
    const parsed = JSON.parse(storedHistory);

    if (
      !parsed ||
      typeof parsed !== "object" ||
      typeof parsed.activeChatId !== "number" ||
      !Array.isArray(parsed.chats) ||
      parsed.chats.length === 0
    ) {
      return defaultHistoryData;
    }

    return parsed;
  } catch {
    return defaultHistoryData;
  }
}

function buildMessage(role, author, content, extra = {}) {
  return {
    id: `${role}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    role,
    author,
    time: formatTime(new Date()),
    content,
    chartData: null,
    status: "ready",
    ...extra,
  };
}

function shortenText(value, limit = 72) {
  if (!value) {
    return "";
  }

  return value.length > limit ? `${value.slice(0, limit).trim()}...` : value;
}

function stripHtml(value) {
  if (!value) {
    return "";
  }

  const parser = new DOMParser();
  const doc = parser.parseFromString(String(value), "text/html");
  return (doc.body.textContent || "").trim();
}

function formatHistorySnippet(value, limit = 96) {
  return shortenText(stripHtml(value), limit);
}

function sanitizeAssistantHtml(value) {
  if (!value) {
    return "";
  }

  const parser = new DOMParser();
  const doc = parser.parseFromString(String(value), "text/html");
  const allowedTags = new Set(["P", "UL", "OL", "LI", "STRONG", "EM", "BR"]);

  Array.from(doc.body.querySelectorAll("*")).forEach((node) => {
    if (!allowedTags.has(node.tagName)) {
      const fragment = document.createDocumentFragment();
      while (node.firstChild) {
        fragment.appendChild(node.firstChild);
      }
      node.replaceWith(fragment);
      return;
    }

    Array.from(node.attributes).forEach((attribute) => {
      node.removeAttribute(attribute.name);
    });
  });

  return doc.body.innerHTML;
}

function createEmptyChat() {
  const id = Date.now();

  return {
    id,
    title: "New Chat",
    snippet: "Start a conversation with the factory API.",
    timestamp: "Draft",
    messages: [],
  };
}

function sanitizeChartData(chartData) {
  if (
    !chartData ||
    typeof chartData !== "object"
  ) {
    return null;
  }

  if (chartData.type === "multi_line" && Array.isArray(chartData.points)) {
    const points = chartData.points
      .map((point) => {
        const value = Number(point?.value);
        if (!point || !Number.isFinite(value)) {
          return null;
        }

        return {
          label: String(point.label),
          series: String(point.series),
          value,
        };
      })
      .filter(Boolean);

    if (points.length === 0) {
      return null;
    }

    return {
      type: "multi_line",
      xLabel: chartData.x_label ? String(chartData.x_label) : "x",
      yLabel: chartData.y_label ? String(chartData.y_label) : "value",
      seriesLabel: chartData.series_label ? String(chartData.series_label) : "series",
      points,
    };
  }

  if (chartData.type === "range" && Array.isArray(chartData.points)) {
    const points = chartData.points
      .map((point) => {
        const min = Number(point?.min);
        const max = Number(point?.max);
        if (!point || !Number.isFinite(min) || !Number.isFinite(max)) {
          return null;
        }

        const safeMin = Math.min(min, max);
        const safeMax = Math.max(min, max);
        return {
          label: String(point.label),
          min: safeMin,
          max: safeMax,
          value: safeMax,
          range: safeMax - safeMin,
        };
      })
      .filter(Boolean);

    if (points.length === 0) {
      return null;
    }

    return {
      type: "range",
      xLabel: chartData.x_label ? String(chartData.x_label) : "x",
      yLabel: chartData.y_label ? String(chartData.y_label) : "value",
      points,
    };
  }

  if (!Array.isArray(chartData.labels) || !Array.isArray(chartData.values)) {
    return null;
  }

  const points = chartData.labels
    .map((label, index) => ({
      label: String(label),
      value: Number(chartData.values[index]),
    }))
    .filter((point) => Number.isFinite(point.value));

  if (points.length === 0) {
    return null;
  }

  return {
    type: chartData.type === "line" ? "line" : "bar",
    xLabel: chartData.x_label ? String(chartData.x_label) : "x",
    yLabel: chartData.y_label ? String(chartData.y_label) : "value",
    points,
  };
}

const chartSeriesPalette = [
  "#00d4ff",
  "#ffb457",
  "#7ef29a",
  "#ff7aa2",
  "#9f8cff",
  "#ffd36b",
];

function buildRequestHistory(messages, nextQuestion) {
  const history = messages
    .filter((message) => {
      if (!message || typeof message.content !== "string") {
        return false;
      }

      if (message.status === "loading" || message.status === "error") {
        return false;
      }

      return message.role === "user" || message.role === "assistant";
    })
    .slice(-6)
    .map((message) => ({
      role: message.role,
      content:
        message.role === "assistant"
          ? stripHtml(message.content)
          : message.content.trim(),
    }))
    .filter((message) => message.content);

  history.push({
    role: "user",
    content: nextQuestion,
  });

  return history;
}

function parseChartDate(value) {
  const text = String(value);

  if (!/^\d{4}-\d{2}(-\d{2})?/.test(text)) {
    return null;
  }

  const parsed = new Date(text.replace(" ", "T"));
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatChartLabel(value) {
  if (!value) {
    return "";
  }

  const parsed = parseChartDate(value);

  if (!parsed) {
    return String(value);
  }

  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
  }).format(parsed);
}

function formatChartTooltipLabel(value) {
  if (!value) {
    return "";
  }

  const parsed = parseChartDate(value);

  if (!parsed) {
    return String(value);
  }

  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function formatChartValue(value) {
  if (!Number.isFinite(value)) {
    return value;
  }

  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: 2,
  }).format(value);
}

function sanitizeFileSegment(value) {
  return String(value || "chart")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "chart";
}

function downloadFile(filename, blob, mimeType) {
  const url = URL.createObjectURL(new Blob([blob], { type: mimeType }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function ChartPanel({ chartData }) {
  const gradientId = useId().replace(/:/g, "");
  const seriesNames = useMemo(
    () =>
      chartData.type === "multi_line"
        ? [...new Set(chartData.points.map((point) => point.series))]
        : [],
    [chartData]
  );
  const chartPoints = useMemo(() => {
    if (chartData.type !== "multi_line") {
      return chartData.points.map((point) => ({
        ...point,
        shortLabel: formatChartLabel(point.label),
      }));
    }

    const rowsByLabel = new Map();
    chartData.points.forEach((point) => {
      const existing = rowsByLabel.get(point.label) || {
        label: point.label,
        shortLabel: formatChartLabel(point.label),
      };
      existing[point.series] = point.value;
      rowsByLabel.set(point.label, existing);
    });
    return Array.from(rowsByLabel.values());
  }, [chartData]);
  const totalPoints = chartPoints.length;
  const [zoomRange, setZoomRange] = useState(() => ({
    startIndex: Math.max(0, totalPoints - Math.min(totalPoints, DEFAULT_VISIBLE_POINTS)),
    endIndex: Math.max(0, totalPoints - 1),
  }));
  const visiblePoints = chartPoints.slice(zoomRange.startIndex, zoomRange.endIndex + 1);
  const canZoomIn = visiblePoints.length > 8;
  const isZoomed = visiblePoints.length !== totalPoints;
  const visibleLineValues = useMemo(() => {
    if (chartData.type === "multi_line") {
      return visiblePoints.flatMap((point) =>
        seriesNames
          .map((seriesName) => point[seriesName])
          .filter((value) => Number.isFinite(value))
      );
    }

    if (chartData.type === "line") {
      return visiblePoints
        .map((point) => point.value)
        .filter((value) => Number.isFinite(value));
    }

    return [];
  }, [chartData.type, visiblePoints, seriesNames]);
  const lineYAxisDomain = useMemo(() => {
    if (visibleLineValues.length === 0) {
      return ["auto", "auto"];
    }

    const minValue = Math.min(...visibleLineValues);
    const maxValue = Math.max(...visibleLineValues);

    if (minValue === maxValue) {
      const basePadding = Math.abs(minValue) > 0 ? Math.abs(minValue) * 0.05 : 1;
      return [minValue - basePadding, maxValue + basePadding];
    }

    const padding = Math.max((maxValue - minValue) * 0.08, 0.5);
    return [minValue - padding, maxValue + padding];
  }, [visibleLineValues]);

  useEffect(() => {
    setZoomRange({
      startIndex: Math.max(0, totalPoints - Math.min(totalPoints, DEFAULT_VISIBLE_POINTS)),
      endIndex: Math.max(0, totalPoints - 1),
    });
  }, [totalPoints]);

  function updateZoomRange(startIndex, endIndex) {
    const safeStart = Math.max(0, Math.min(startIndex, totalPoints - 1));
    const safeEnd = Math.max(safeStart, Math.min(endIndex, totalPoints - 1));
    setZoomRange({ startIndex: safeStart, endIndex: safeEnd });
  }

  function handleZoomIn() {
    if (!canZoomIn) {
      return;
    }

    const currentSpan = zoomRange.endIndex - zoomRange.startIndex + 1;
    const nextSpan = Math.max(8, Math.floor(currentSpan * 0.7));
    const midpoint = zoomRange.startIndex + Math.floor(currentSpan / 2);
    const nextStart = Math.max(0, midpoint - Math.floor(nextSpan / 2));
    const nextEnd = Math.min(totalPoints - 1, nextStart + nextSpan - 1);
    updateZoomRange(Math.max(0, nextEnd - nextSpan + 1), nextEnd);
  }

  function handleZoomOut() {
    if (!isZoomed) {
      return;
    }

    const currentSpan = zoomRange.endIndex - zoomRange.startIndex + 1;
    const nextSpan = Math.min(totalPoints, Math.ceil(currentSpan * 1.4));
    const midpoint = zoomRange.startIndex + Math.floor(currentSpan / 2);
    const nextStart = Math.max(0, midpoint - Math.floor(nextSpan / 2));
    const nextEnd = Math.min(totalPoints - 1, nextStart + nextSpan - 1);
    updateZoomRange(Math.max(0, nextEnd - nextSpan + 1), nextEnd);
  }

  function handleResetZoom() {
    updateZoomRange(0, totalPoints - 1);
  }

  function handleBrushChange(range) {
    if (!range || typeof range.startIndex !== "number" || typeof range.endIndex !== "number") {
      return;
    }

    updateZoomRange(range.startIndex, range.endIndex);
  }

  function exportChartData(format) {
    const rows =
      chartData.type === "multi_line"
        ? chartData.points.map((point) => ({
            [chartData.xLabel]: point.label,
            [chartData.seriesLabel]: point.series,
            [chartData.yLabel]: point.value,
          }))
        : chartData.type === "range"
        ? chartData.points.map((point) => ({
            [chartData.xLabel]: point.label,
            min: point.min,
            max: point.max,
          }))
        : chartData.points.map((point) => ({
            [chartData.xLabel]: point.label,
            [chartData.yLabel]: point.value,
          }));
    const baseName = `${sanitizeFileSegment(chartData.yLabel)}-by-${sanitizeFileSegment(chartData.xLabel)}`;

    if (format === "csv") {
      const worksheet = XLSX.utils.json_to_sheet(rows);
      const csv = XLSX.utils.sheet_to_csv(worksheet);
      downloadFile(`${baseName}.csv`, csv, "text/csv;charset=utf-8;");
      return;
    }

    const worksheet = XLSX.utils.json_to_sheet(rows);
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, worksheet, "Chart Data");
    const output = XLSX.write(workbook, { bookType: "xlsx", type: "array" });
    downloadFile(
      `${baseName}.xlsx`,
      output,
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    );
  }

  return (
    <div className="chart-panel">
      <div className="chart-panel__header">
        <div>
          <strong>
            {chartData.type === "line"
              ? "Trend chart"
              : chartData.type === "multi_line"
                ? "Multi-series trend chart"
              : chartData.type === "range"
                ? "Range chart"
                : "Distribution chart"}
          </strong>
          <span>
            {chartData.xLabel} vs {chartData.yLabel}
          </span>
        </div>
        <span>{chartData.points.length} points</span>
      </div>

      <div className="chart-panel__actions">
        <div className="chart-panel__toolbar">
          <button
            className="chart-panel__button"
            type="button"
            onClick={handleZoomIn}
            disabled={!canZoomIn}
          >
            Zoom In
          </button>
          <button
            className="chart-panel__button"
            type="button"
            onClick={handleZoomOut}
            disabled={!isZoomed}
          >
            Zoom Out
          </button>
          <button
            className="chart-panel__button"
            type="button"
            onClick={handleResetZoom}
            disabled={!isZoomed}
          >
            Show All
          </button>
        </div>
        <div className="chart-panel__toolbar">
          <button
            className="chart-panel__button chart-panel__button--accent"
            type="button"
            onClick={() => exportChartData("csv")}
          >
            Export CSV
          </button>
          <button
            className="chart-panel__button chart-panel__button--accent"
            type="button"
            onClick={() => exportChartData("xlsx")}
          >
            Export Excel
          </button>
        </div>
      </div>

      <div className="chart-panel__canvas">
        <ResponsiveContainer width="100%" height="100%" minWidth={0} minHeight={260}>
          {chartData.type === "line" ? (
            <LineChart data={chartPoints} margin={{ top: 12, right: 16, bottom: 12, left: 0 }}>
              <defs>
                <linearGradient id={`${gradientId}-line`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="rgba(0, 212, 255, 0.30)" />
                  <stop offset="100%" stopColor="rgba(0, 212, 255, 0.02)" />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="rgba(148, 180, 211, 0.18)" vertical={false} />
              <XAxis
                dataKey="label"
                tickLine={false}
                axisLine={false}
                minTickGap={24}
                tick={{ fill: "var(--muted)", fontSize: 12 }}
                tickFormatter={formatChartLabel}
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                width={56}
                domain={lineYAxisDomain}
                tick={{ fill: "var(--muted)", fontSize: 12 }}
                tickFormatter={formatChartValue}
              />
              <Tooltip
                formatter={(value) => [formatChartValue(value), chartData.yLabel]}
                labelFormatter={(value) => formatChartTooltipLabel(value)}
                contentStyle={{
                  borderRadius: "16px",
                  border: "1px solid rgba(133, 195, 255, 0.18)",
                  background: "rgba(6, 17, 31, 0.94)",
                  color: "#f2f7ff",
                }}
              />
              <Line
                type="monotone"
                dataKey="value"
                stroke="#00d4ff"
                strokeWidth={3}
                dot={{ r: 2, strokeWidth: 0, fill: "#ffd36b" }}
                activeDot={{ r: 5, stroke: "#071521", strokeWidth: 2, fill: "#00d4ff" }}
              />
              <Brush
                dataKey="label"
                startIndex={zoomRange.startIndex}
                endIndex={zoomRange.endIndex}
                onChange={handleBrushChange}
                travellerWidth={12}
                stroke="#00d4ff"
                fill="rgba(0, 212, 255, 0.08)"
                tickFormatter={formatChartLabel}
                gap={1}
              />
            </LineChart>
          ) : chartData.type === "multi_line" ? (
            <LineChart data={chartPoints} margin={{ top: 12, right: 16, bottom: 12, left: 0 }}>
              <CartesianGrid stroke="rgba(148, 180, 211, 0.18)" vertical={false} />
              <XAxis
                dataKey="label"
                tickLine={false}
                axisLine={false}
                minTickGap={24}
                tick={{ fill: "var(--muted)", fontSize: 12 }}
                tickFormatter={formatChartLabel}
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                width={56}
                domain={lineYAxisDomain}
                tick={{ fill: "var(--muted)", fontSize: 12 }}
                tickFormatter={formatChartValue}
              />
              <Tooltip
                formatter={(value, name) => [formatChartValue(value), name]}
                labelFormatter={(value) => formatChartTooltipLabel(value)}
                contentStyle={{
                  borderRadius: "16px",
                  border: "1px solid rgba(133, 195, 255, 0.18)",
                  background: "rgba(6, 17, 31, 0.94)",
                  color: "#f2f7ff",
                }}
                labelStyle={{
                  color: "#f2f7ff",
                  fontWeight: 700,
                }}
                itemStyle={{
                  color: "#ffd36b",
                }}
              />
              <Legend
                verticalAlign="top"
                height={30}
                wrapperStyle={{ color: "var(--muted)", fontSize: "12px" }}
              />
              {seriesNames.map((seriesName, index) => (
                <Line
                  key={seriesName}
                  type="monotone"
                  dataKey={seriesName}
                  name={seriesName}
                  stroke={chartSeriesPalette[index % chartSeriesPalette.length]}
                  strokeWidth={3}
                  dot={{ r: 2, strokeWidth: 0 }}
                  activeDot={{ r: 5, stroke: "#071521", strokeWidth: 2 }}
                  connectNulls={false}
                />
              ))}
              <Brush
                dataKey="label"
                startIndex={zoomRange.startIndex}
                endIndex={zoomRange.endIndex}
                onChange={handleBrushChange}
                travellerWidth={12}
                stroke="#00d4ff"
                fill="rgba(0, 212, 255, 0.08)"
                tickFormatter={formatChartLabel}
                gap={1}
              />
            </LineChart>
          ) : chartData.type === "range" ? (
            <BarChart data={chartPoints} margin={{ top: 12, right: 16, bottom: 12, left: 0 }}>
              <CartesianGrid stroke="rgba(148, 180, 211, 0.18)" vertical={false} />
              <XAxis
                dataKey="label"
                tickLine={false}
                axisLine={false}
                minTickGap={24}
                tick={{ fill: "var(--muted)", fontSize: 12 }}
                tickFormatter={formatChartLabel}
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                width={56}
                tick={{ fill: "var(--muted)", fontSize: 12 }}
                tickFormatter={formatChartValue}
              />
              <Tooltip
                formatter={(value, name, item) => {
                  if (name === "min") {
                    return null;
                  }
                  if (name === "range") {
                    const point = item?.payload;
                    return [
                      `${formatChartValue(point?.min)} - ${formatChartValue(point?.max)}`,
                      `${chartData.yLabel} range`,
                    ];
                  }
                  return [formatChartValue(value), name];
                }}
                labelFormatter={(value) => formatChartTooltipLabel(value)}
                contentStyle={{
                  borderRadius: "16px",
                  border: "1px solid rgba(133, 195, 255, 0.18)",
                  background: "rgba(6, 17, 31, 0.94)",
                  color: "#f2f7ff",
                }}
                labelStyle={{
                  color: "#f2f7ff",
                  fontWeight: 700,
                }}
                itemStyle={{
                  color: "#ffd36b",
                }}
                wrapperStyle={{
                  outline: "none",
                }}
              />
              <Bar dataKey="min" stackId="range" fill="transparent" isAnimationActive={false} />
              <Bar dataKey="range" stackId="range" radius={[8, 8, 8, 8]}>
                {chartPoints.map((point) => (
                  <Cell
                    key={point.label}
                    fill="rgba(0, 212, 255, 0.72)"
                    stroke="#00d4ff"
                    strokeWidth={1}
                  />
                ))}
              </Bar>
              <Brush
                dataKey="label"
                startIndex={zoomRange.startIndex}
                endIndex={zoomRange.endIndex}
                onChange={handleBrushChange}
                travellerWidth={12}
                stroke="#00d4ff"
                fill="rgba(0, 212, 255, 0.08)"
                tickFormatter={formatChartLabel}
                gap={1}
              />
            </BarChart>
          ) : (
            <BarChart data={chartPoints} margin={{ top: 12, right: 16, bottom: 12, left: 0 }}>
              <defs>
                <linearGradient id={`${gradientId}-bar`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#00d4ff" />
                  <stop offset="100%" stopColor="#ffb457" />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="rgba(148, 180, 211, 0.18)" vertical={false} />
              <XAxis
                dataKey="label"
                tickLine={false}
                axisLine={false}
                minTickGap={24}
                tick={{ fill: "var(--muted)", fontSize: 12 }}
                tickFormatter={formatChartLabel}
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                width={56}
                tick={{ fill: "var(--muted)", fontSize: 12 }}
                tickFormatter={formatChartValue}
              />
              <Tooltip
                formatter={(value) => [formatChartValue(value), chartData.yLabel]}
                labelFormatter={(value) => formatChartTooltipLabel(value)}
                contentStyle={{
                  borderRadius: "16px",
                  border: "1px solid rgba(133, 195, 255, 0.18)",
                  background: "rgba(6, 17, 31, 0.94)",
                  color: "#f2f7ff",
                }}
              />
              <Bar dataKey="value" fill={`url(#${gradientId}-bar)`} radius={[8, 8, 2, 2]} />
              <Brush
                dataKey="label"
                startIndex={zoomRange.startIndex}
                endIndex={zoomRange.endIndex}
                onChange={handleBrushChange}
                travellerWidth={12}
                stroke="#00d4ff"
                fill="rgba(0, 212, 255, 0.08)"
                tickFormatter={formatChartLabel}
                gap={1}
              />
            </BarChart>
          )}
        </ResponsiveContainer>
      </div>

      <div className="chart-panel__footer">
        <div className="chart-panel__metric">
          <span>Min</span>
          <strong>
            {formatChartValue(
              Math.min(...chartData.points.map((point) => point.min ?? point.value))
            )}
          </strong>
        </div>
        <div className="chart-panel__metric">
          <span>Max</span>
          <strong>
            {formatChartValue(
              Math.max(...chartData.points.map((point) => point.max ?? point.value))
            )}
          </strong>
        </div>
        <div className="chart-panel__metric">
          <span>Visible</span>
          <strong>{visiblePoints.length}</strong>
        </div>
        <div className="chart-panel__metric">
          <span>Series</span>
          <strong>
            {chartData.type === "multi_line" ? `${seriesNames.length} machines` : chartData.yLabel}
          </strong>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [theme, setTheme] = useState(getInitialTheme);
  const [historyData, setHistoryData] = useState(getInitialHistory);
  const [messageInput, setMessageInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [exportingMessageId, setExportingMessageId] = useState(null);
  const messageStreamRef = useRef(null);

  async function handleExportFullResult(message) {
    if (!message.fullSql || exportingMessageId) {
      return;
    }
    setExportingMessageId(message.id);
    try {
      const response = await fetch("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sql: message.fullSql }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok || !payload) {
        throw new Error(
          (payload && payload.detail) || `Export failed with status ${response.status}.`
        );
      }
      const worksheet = XLSX.utils.aoa_to_sheet([payload.columns, ...payload.rows]);
      const workbook = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(workbook, worksheet, "Results");
      const output = XLSX.write(workbook, { bookType: "xlsx", type: "array" });
      downloadFile(
        `apotelesmata-${payload.row_count}.xlsx`,
        output,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
      );
    } catch (error) {
      window.console.error("Full export failed:", error);
    } finally {
      setExportingMessageId(null);
    }
  }

  useEffect(() => {
    document.body.dataset.theme = theme;
    window.localStorage.setItem(themeStorageKey, theme);
  }, [theme]);

  useEffect(() => {
    window.localStorage.setItem(historyStorageKey, JSON.stringify(historyData));
  }, [historyData]);

  const activeChat = useMemo(
    () =>
      historyData.chats.find((chat) => chat.id === historyData.activeChatId) ||
      historyData.chats[0],
    [historyData]
  );

  useEffect(() => {
    const stream = messageStreamRef.current;

    if (!stream) {
      return;
    }

    stream.scrollTo({
      top: stream.scrollHeight,
      behavior: "smooth",
    });
  }, [activeChat?.id, activeChat?.messages.length]);

  function setActiveChat(id) {
    setHistoryData((current) => ({
      ...current,
      activeChatId: id,
    }));
  }

  function createNewChat() {
    const nextChat = createEmptyChat();

    setHistoryData((current) => ({
      activeChatId: nextChat.id,
      chats: [nextChat, ...current.chats],
    }));
  }

  function updateChat(chatId, updater) {
    setHistoryData((current) => ({
      ...current,
      chats: current.chats.map((chat) => (chat.id === chatId ? updater(chat) : chat)),
    }));
  }

  function exportActiveConversation() {
    if (!activeChat) {
      return;
    }

    const exportPayload = {
      exportedAt: new Date().toISOString(),
      chat: {
        id: activeChat.id,
        title: activeChat.title,
        snippet: activeChat.snippet,
        timestamp: activeChat.timestamp,
        messages: activeChat.messages.map((message) => ({
          id: message.id,
          role: message.role,
          author: message.author,
          time: message.time,
          content: message.content,
          status: message.status,
          chartData: message.chartData,
        })),
      },
    };

    const fileName = `${sanitizeFileSegment(activeChat.title || "conversation")}.json`;
    const json = JSON.stringify(exportPayload, null, 2);
    downloadFile(fileName, json, "application/json;charset=utf-8;");
  }

  async function handleSend(questionOverride) {
    const content = (questionOverride ?? messageInput).trim();

    if (!content || isSending || !activeChat) {
      return;
    }

    const targetChatId = activeChat.id;
    const requestHistory = buildRequestHistory(activeChat.messages, content);
    const userMessage = buildMessage("user", "You", content);
    const pendingAssistantMessage = buildMessage(
      "assistant",
      "Assistant",
      "The bots are thinking hard about your question...",
      { status: "loading" }
    );

    setMessageInput("");
    setIsSending(true);
    updateChat(targetChatId, (chat) => ({
      ...chat,
      title: chat.messages.length === 0 ? shortenText(content, 40) || "New Chat" : chat.title,
      snippet: content,
      timestamp: "Sending...",
      messages: [...chat.messages, userMessage, pendingAssistantMessage],
    }));

    try {
      const response = await fetch(API_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          question: content,
          history: requestHistory,
        }),
      });

      if (!response.ok) {
        const errorPayload = await response.json().catch(() => null);
        const detail =
          errorPayload && typeof errorPayload.detail === "string"
            ? errorPayload.detail
            : `Request failed with status ${response.status}.`;
        throw new Error(detail);
      }

      const payload = await response.json();
      const assistantMessage = buildMessage(
        "assistant",
        "Assistant",
        typeof payload.answer === "string" && payload.answer.trim()
          ? payload.answer.trim()
          : "Το backend δεν επέστρεψε κείμενο απάντησης.",
        {
          chartData: sanitizeChartData(payload.chart_data),
          sql: typeof payload.sql === "string" ? payload.sql : null,
          truncated: Boolean(payload.truncated),
          fullSql: typeof payload.full_sql === "string" ? payload.full_sql : null,
          totalCount: typeof payload.total_count === "number" ? payload.total_count : null,
        }
      );

      updateChat(targetChatId, (chat) => ({
        ...chat,
        snippet: formatHistorySnippet(assistantMessage.content),
        timestamp: "Active now",
        messages: chat.messages.map((message) =>
          message.id === pendingAssistantMessage.id ? assistantMessage : message
        ),
      }));
    } catch (error) {
      const assistantMessage = buildMessage(
        "assistant",
        "Assistant",
        `The API request failed: ${error.message}`,
        { status: "error" }
      );

      updateChat(targetChatId, (chat) => ({
        ...chat,
        snippet: formatHistorySnippet(assistantMessage.content),
        timestamp: "API error",
        messages: chat.messages.map((message) =>
          message.id === pendingAssistantMessage.id ? assistantMessage : message
        ),
      }));
    } finally {
      setIsSending(false);
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar__glow"></div>

        <section className="brand-card">
          <div className="brand-lockup">
            <h1 className="brand-title">Industrial Copilot</h1>
          </div>
        </section>

        <section className="sidebar-panel sidebar-panel--history">
          <div className="panel-header">
            <div>
              <p className="eyebrow">History</p>
              <h2>Current Session</h2>
            </div>
          </div>
          <div className="history-list">
            {historyData.chats.map((item) => (
              <article
                key={item.id}
                className={`history-item${
                  item.id === historyData.activeChatId ? " is-active" : ""
                }`}
                onClick={() => setActiveChat(item.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    setActiveChat(item.id);
                  }
                }}
                tabIndex={0}
              >
                <div className="history-item__title">{item.title}</div>
                <p className="history-item__snippet">{formatHistorySnippet(item.snippet)}</p>
                <div className="history-item__meta">
                  <span>{item.timestamp}</span>
                  <span>{item.messages.length} msgs</span>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="sidebar-panel sidebar-panel--prompts">
          <div className="panel-header">
            <div>
              <p className="eyebrow">Demo Questions</p>
              <h2>Quick Starts</h2>
            </div>
          </div>
          <div className="prompt-list">
            {starterPrompts.map((prompt) => (
              <button
                key={prompt}
                className="prompt-chip"
                type="button"
                onClick={() => {
                  setMessageInput(prompt);
                }}
              >
                {prompt}
              </button>
            ))}
          </div>
        </section>
      </aside>

      <main className="chat-stage">
        <header className="chat-stage__header">
          <section className="header-content">
            <div>
              <p className="eyebrow">Active Chat</p>
              <h2>{activeChat?.title ?? "Conversation"}</h2>
            </div>
            <div className="header-actions">
              <button
                className="secondary-button"
                type="button"
                onClick={exportActiveConversation}
                disabled={!activeChat}
              >
                Export JSON
              </button>
              <button
                className="secondary-button theme-toggle"
                id="themeToggle"
                type="button"
                aria-pressed={theme === "dark"}
                onClick={() =>
                  setTheme((current) => (current === "dark" ? "light" : "dark"))
                }
              >
                {theme === "dark" ? "Switch to Light" : "Switch to Dark"}
              </button>
              <button className="primary-button" type="button" onClick={createNewChat}>
                New Chat
              </button>
            </div>
          </section>
        </header>

        <section ref={messageStreamRef} className="message-stream">
          {(activeChat?.messages ?? []).map((message) => (
            <section
              key={message.id}
              className={`message-card message-card--${message.role}${
                message.status === "loading"
                  ? " is-loading"
                  : message.status === "error"
                    ? " is-error"
                    : ""
              }`}
            >
              <div className="message-card__meta">
                <span>{message.author}</span>
                <span>{message.time}</span>
              </div>
              {message.role === "assistant" && message.status === "ready" ? (
                <div
                  className="message-card__content"
                  dangerouslySetInnerHTML={{ __html: sanitizeAssistantHtml(message.content) }}
                />
              ) : (
                <p>{message.content}</p>
              )}
              {message.chartData ? <ChartPanel chartData={message.chartData} /> : null}
              {message.role === "assistant" && message.status === "ready" && message.sql ? (
                <details className="message-card__sql">
                  <summary>Δες το SQL</summary>
                  <pre>{message.sql}</pre>
                </details>
              ) : null}
              {message.role === "assistant" && message.status === "ready" && message.truncated && message.fullSql ? (
                <button
                  type="button"
                  className="secondary-button"
                  disabled={exportingMessageId === message.id}
                  onClick={() => handleExportFullResult(message)}
                >
                  {exportingMessageId === message.id
                    ? "Λήψη..."
                    : `Λήψη όλων των αποτελεσμάτων${
                        message.totalCount ? ` (${message.totalCount})` : ""
                      }`}
                </button>
              ) : null}
            </section>
          ))}
        </section>

        <section className="composer-wrap">
          <label className="composer">
            <textarea
              id="messageInput"
              rows="1"
              value={messageInput}
              onChange={(event) => setMessageInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  handleSend();
                }
              }}
              placeholder="Ask about machine status, energy, anomalies, maintenance, failures, or daily trends..."
            />
            <div className="composer-actions">
              <button
                className="send-button"
                id="sendButton"
                type="button"
                onClick={() => handleSend()}
                disabled={isSending || !messageInput.trim()}
              >
                {isSending ? "Sending..." : "Send"}
              </button>
            </div>
          </label>
        </section>
      </main>
    </div>
  );
}
