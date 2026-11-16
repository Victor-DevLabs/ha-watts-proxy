// Tests of the pure logic of the card: node --test tests/
const test = require("node:test");
const assert = require("node:assert");
const { execFileSync } = require("node:child_process");
const path = require("node:path");
const card = require("../card/watts-schedule-card.js");

function week(...days) {
  return days.map((periods) => {
    const d = Array(48).fill("0");
    for (const [a, b] of periods) for (let i = a; i < b; i++) d[i] = "5";
    return d.join("");
  }).join("");
}

const OFFICE = week(...Array(5).fill([[12, 31]]), [], []);
const SAMPLES = [
  OFFICE,
  week([[11, 19], [27, 32]], [[11, 19]], [[11, 19], [27, 32]], [], [], [[0, 48]], [[37, 46]]),
  "0".repeat(336),
  "5".repeat(336),
];

test("text in English and Spanish", () => {
  assert.strictEqual(card.scheduleText(OFFICE), "Mon–Fri: 06:00–15:30; Sat–Sun: all eco");
  assert.strictEqual(card.scheduleText(OFFICE, "es-ES"), "Lun–Vie: 06:00–15:30; Sáb–Dom: todo eco");
  assert.strictEqual(card.scheduleText("123"), "");
});

test("same text as the add-on", () => {
  const script = "import json,sys; sys.path.insert(0, sys.argv[1]); import watts_ha as w; " +
    "print(json.dumps([[w.schedule_text(s, l) for l in ('en', 'es')] for s in json.loads(sys.argv[2])]))";
  let out;
  try {
    out = execFileSync("python", ["-c", script, path.join(__dirname, "..", "watts_proxy"), JSON.stringify(SAMPLES)]);
  } catch (e) {
    return; // no Python available: skipped
  }
  const expected = JSON.parse(out.toString());
  SAMPLES.forEach((s, i) => {
    assert.strictEqual(card.scheduleText(s, "en"), expected[i][0]);
    assert.strictEqual(card.scheduleText(s, "es"), expected[i][1]);
  });
});

test("paint and copy", () => {
  const base = "0".repeat(336);
  const p = card.paint(base, 20, 15, "5");
  assert.strictEqual(p.slice(15, 21), "555555");
  assert.strictEqual(p.replace(/0/g, "").length, 6);
  const c = card.copyDay(OFFICE, 0, [5, 6]);
  assert.strictEqual(c.slice(5 * 48), OFFICE.slice(0, 48).repeat(2));
  assert.strictEqual(c.slice(0, 5 * 48), OFFICE.slice(0, 5 * 48));
});

test("old attribute names still work", () => {
  assert.deepStrictEqual(card.zoneAttrs({ semana: "x", comando: "watts_proxy/a/set/horario", orden_en_curso: true, zona: "Z" }),
    { week: "x", topic: "watts_proxy/a/set/horario", pending: true, zone: "Z" });
  assert.deepStrictEqual(card.zoneAttrs({ week: "y", command_topic: "t", pending: false, zone: "W" }),
    { week: "y", topic: "t", pending: false, zone: "W" });
});
