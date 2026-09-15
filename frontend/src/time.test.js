import test from "node:test";
import assert from "node:assert/strict";
import { parseUtcTime, formatTime, chartTime } from "./time.js";

for (const zone of ["UTC", "Asia/Karachi", "America/New_York"]) {
  test("PKT display is independent of browser timezone: " + zone, () => {
    process.env.TZ = zone;
    for (const stamp of ["2026-09-14T16:53:20", "2026-09-14 16:53:20",
                         "2026-09-14T16:53:20Z", "2026-09-14T21:53:20+05:00"]) {
      assert.equal(formatTime(stamp, { withDate: false }), "21:53:20");
      assert.equal(parseUtcTime(stamp).toISOString(), "2026-09-14T16:53:20.000Z");
    }
    assert.equal(formatTime("2026-09-14T21:30:00Z"), "15 Sept, 02:30:00");
    assert.equal(chartTime(Date.parse("2026-09-14T16:53:20Z") / 1000), "21:53");
  });
}
test("missing or invalid dates do not invent trade times", () => {
  for (const value of [null, "", "not a date"]) {
    assert.equal(parseUtcTime(value), null);
    assert.equal(formatTime(value), "—");
  }
});
