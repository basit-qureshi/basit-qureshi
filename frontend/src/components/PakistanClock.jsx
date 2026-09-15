import { useEffect, useState } from "react";
import { formatTime } from "../time";

export default function PakistanClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);
  return <div className="pakistan-clock"><span>PAKISTAN TIME</span><time>{formatTime(now)} <b>PKT</b></time></div>;
}
