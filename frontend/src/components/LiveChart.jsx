import { useEffect, useRef, useState } from "react";
import { createChart, CandlestickSeries, LineSeries, createSeriesMarkers } from "lightweight-charts";
import { api } from "../api";
import { chartTime, formatTime, parseUtcTime } from "../time";

export default function LiveChart({ trades }) {
  const containerRef = useRef(null);
  const candleSeriesRef = useRef(null);
  const emaFastRef = useRef(null);
  const emaSlowRef = useRef(null);
  const markersRef = useRef(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: 340,
      layout: { background: { color: "transparent" }, textColor: getComputedStyle(containerRef.current).getPropertyValue("--muted").trim() },
      grid: {
        vertLines: { color: "rgba(150,150,150,0.1)" },
        horzLines: { color: "rgba(150,150,150,0.1)" },
      },
      timeScale: { timeVisible: true, secondsVisible: false, tickMarkFormatter: chartTime },
      // The chart library labels the time axis in UTC unless told otherwise, so
      // the axis is shifted into the account owner's zone to match the rest of
      // the dashboard. Only the labels move; the data is untouched.
      localization: {
        timeFormatter: (ts) => formatTime(ts),
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#16a34a",
      downColor: "#dc2626",
      borderVisible: false,
      wickUpColor: "#16a34a",
      wickDownColor: "#dc2626",
    });
    const emaFast = chart.addSeries(LineSeries, { color: "#2563eb", lineWidth: 1, priceLineVisible: false });
    const emaSlow = chart.addSeries(LineSeries, { color: "#f59e0b", lineWidth: 1, priceLineVisible: false });

    candleSeriesRef.current = candleSeries;
    emaFastRef.current = emaFast;
    emaSlowRef.current = emaSlow;
    markersRef.current = createSeriesMarkers(candleSeries, []);

    function handleResize() {
      chart.applyOptions({ width: containerRef.current.clientWidth });
    }
    window.addEventListener("resize", handleResize);

    let cancelled = false;
    async function load() {
      try {
        const data = await api.getCandles(200);
        if (cancelled) return;
        candleSeriesRef.current.setData(data.candles);
        emaFastRef.current.setData(data.ema_fast);
        emaSlowRef.current.setData(data.ema_slow);
        setError(null);
      } catch (err) {
        if (!cancelled) setError(err.message);
      }
    }
    load();
    const interval = setInterval(load, 4000);

    return () => {
      cancelled = true;
      clearInterval(interval);
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, []);

  useEffect(() => {
    if (!markersRef.current || !trades) return;
    const markers = trades
      .filter((t) => parseUtcTime(t.open_time))
      .map((t) => ({
        time: Math.floor(parseUtcTime(t.open_time).getTime() / 60000) * 60,
        position: t.side === "BUY" ? "belowBar" : "aboveBar",
        color: t.side === "BUY" ? "#16a34a" : "#dc2626",
        shape: t.side === "BUY" ? "arrowUp" : "arrowDown",
        text: t.side,
      }))
      .sort((a, b) => a.time - b.time);
    markersRef.current.setMarkers(markers);
  }, [trades]);

  return (
    <div className="panel chart-panel">
      <div className="panel-heading"><div><span className="eyebrow">MARKET OVERVIEW</span><h3>Live price chart</h3></div><span className="timezone-label">PKT · UTC+5</span></div>
      <p className="chart-legend"><span className="legend-fast">EMA 9</span><span className="legend-slow">EMA 21</span><span>Trade entries marked</span></p>
      {error && <p className="error-text">⚠ {error}</p>}
      <div ref={containerRef} />
    </div>
  );
}
