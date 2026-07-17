import { useEffect, useRef, useState } from "react";
import { createChart, CandlestickSeries, LineSeries, createSeriesMarkers } from "lightweight-charts";
import { api } from "../api";

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
      layout: { background: { color: "transparent" }, textColor: "var(--muted)" },
      grid: {
        vertLines: { color: "rgba(150,150,150,0.1)" },
        horzLines: { color: "rgba(150,150,150,0.1)" },
      },
      timeScale: { timeVisible: true, secondsVisible: false },
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
      .filter((t) => t.open_time)
      .map((t) => ({
        time: Math.floor(new Date(t.open_time).getTime() / 1000),
        position: t.side === "BUY" ? "belowBar" : "aboveBar",
        color: t.side === "BUY" ? "#16a34a" : "#dc2626",
        shape: t.side === "BUY" ? "arrowUp" : "arrowDown",
        text: t.side,
      }))
      .sort((a, b) => a.time - b.time);
    markersRef.current.setMarkers(markers);
  }, [trades]);

  return (
    <div className="panel">
      <h3>
        Live Price Chart <span className="muted">(candles + EMA fast/slow, trade entries marked)</span>
      </h3>
      {error && <p className="error-text">⚠ {error}</p>}
      <div ref={containerRef} />
    </div>
  );
}
