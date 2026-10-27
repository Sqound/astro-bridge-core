import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../wifi_bridge/static/i18n.js", import.meta.url), "utf8");
function load(saved = null) {
  const context = {
    window: {}, localStorage: { getItem: () => saved },
    document: { documentElement: { dataset: {} }, addEventListener() {} }
  };
  vm.runInNewContext(source, context);
  return { i18n: context.window.BridgeI18n, root: context.document.documentElement };
}

test("English is the actual default and unsupported storage values are ignored", () => {
  for (const saved of [null, "xx", "../../fr"]) {
    const { i18n, root } = load(saved);
    assert.equal(i18n.locale, "en");
    assert.equal(root.lang, "en");
    assert.equal(i18n.t("Telescope connections"), "Telescope connections");
  }
});

test("French catalog interpolates named values and all ten choices have truthful coverage", () => {
  const { i18n } = load("fr");
  assert.equal(i18n.t("USB connection {letter}", { letter: "B" }), "Connexion USB B");
  assert.equal(i18n.t("Connected to {name}", { name: "<custom telescope>" }), "Connecté à <custom telescope>");
  assert.equal(i18n.locales.length, 10);
  const expected=i18n.locales.filter(locale=>Object.keys(JSON.parse(readFileSync(new URL(`../locales/${locale.tag}.json`,import.meta.url),"utf8"))).length===Object.keys(i18n.catalogs.en).length).map(locale=>locale.tag);
  assert.equal(expected.length,10);
  assert.deepEqual(i18n.locales.filter(locale=>locale.complete).map(locale=>locale.tag),expected);
  for (const locale of i18n.locales.filter(locale => !locale.complete)) {
    const partial = load(locale.tag);
    assert.equal(partial.i18n.locale, locale.tag);
    assert.equal(partial.root.lang, "en");
    assert.equal(partial.i18n.t("Telescope connections"), "Telescope connections");
  }
});

test("HTML and client keys, interpolation and literal addresses match across ten locales", () => {
  const { i18n } = load();
  const html = readFileSync(new URL("../wifi_bridge/static/index.html", import.meta.url), "utf8");
  const decode = value => value.replaceAll("&amp;", "&").replaceAll("&quot;", '"').replaceAll("&#x27;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">");
  const keys = [...html.matchAll(/data-i18n(?:-aria-label|-placeholder)?="([^"]*)"/g)].map(match => decode(match[1]));
  for (const file of ["app.js", "theme.js", "events.js", "pwa.js", "secure.js"]) {
    const script = readFileSync(new URL("../wifi_bridge/static/" + file, import.meta.url), "utf8");
    keys.push(...[...script.matchAll(/\bt\("([^"\n]+)"/g)].map(match => match[1]));
  }
  for (const key of keys) assert.ok(Object.hasOwn(i18n.catalogs.en, key), "Missing English key: " + key);
  const placeholders = text => [...text.matchAll(/\{([a-zA-Z][a-zA-Z0-9_]*)\}/g)].map(match => match[1]).sort();
  for (const [key, english] of Object.entries(i18n.catalogs.en)) {
    for(const locale of i18n.locales.filter(locale=>locale.complete)){
      const value=i18n.catalogs[locale.tag][key];assert.equal(typeof value,"string",locale.tag+": "+key);
      assert.deepEqual(placeholders(value),placeholders(english),locale.tag+": "+key);
      for(const literal of key.match(/https?:\/\/[A-Za-z0-9./:_-]+|(?:[0-9]{1,3}\.){3}[0-9]{1,3}|\.local\b|\/api\/v[0-9]+(?:\/[A-Za-z0-9_-]+)*|\/interfaces\b/g)||[])assert.ok(value.includes(literal),locale.tag+": address/URL changed: "+key);
    }
  }
});
