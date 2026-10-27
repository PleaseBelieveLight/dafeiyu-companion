"use strict";

// Execute the complete page against the shipped HTML IDs and masked API data.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const pageRoot = process.argv[2] || path.resolve(__dirname, "../pages/插件配置");
const fixturePath = process.argv[3];

class Element {
    constructor(id = "") {
        this.id = id;
        this._value = "";
        this.checked = false;
        this.dataset = {};
        this.innerHTML = "";
        this.textContent = "";
        this.children = [];
        this.classes = new Set();
        this.classList = {
            add: (...names) => names.forEach(name => this.classes.add(name)),
            remove: (...names) => names.forEach(name => this.classes.delete(name)),
            toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name),
        };
    }
    get value() { return this._value; }
    set value(value) { this._value = String(value); }
    setAttribute(name, value) { this[name] = String(value); }
    addEventListener() {}
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.children.push(child); }
    insertBefore(child) { this.children.push(child); }
    querySelectorAll() { return []; }
    querySelector(selector) {
        this.queries ||= {};
        return this.queries[selector] ||= new Element(selector);
    }
    closest() { return null; }
    replaceChildren(text) { this.textContent = text; }
    remove() {}
}

async function checkPage(optimizerEnabled, includeLegacyControl = false) {
    const html = fs.readFileSync(path.join(pageRoot, "index.html"), "utf8");
    const source = fs.readFileSync(path.join(pageRoot, "app.js"), "utf8");
    const config = fixturePath ? JSON.parse(fs.readFileSync(fixturePath, "utf8")) : {
        permission_config: {}, usage_config: {}, cache_config: {}, reply_config: {},
        optimizer_config: {},
        router_config: { chain_text2img: "draw", chain_selfie: "draw", chain_video: "video" },
        persona_config: { active_persona_id: "test", profiles: [{ id: "test", persona_name: "Test", persona_base_prompt: "", persona_ref_image: [] }] },
        providers: [{ id: "draw", api_type: "openai_image", base_url: "https://example.invalid/v1", model: "test-image", available_models: ["test-image"], api_keys: "", timeout: 60 }],
        video_providers: [], presets: [],
    };
    config.optimizer_config.use_astrbot_provider = optimizerEnabled;
    const nodes = new Map([...html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element(match[1])]));
    if (includeLegacyControl) nodes.set("opt_astrbot", new Element("opt_astrbot"));
    const body = new Element("body");
    body.classList.add("is-loading");
    const content = new Element("content");
    const errors = [];
    const reads = [];
    const bridge = {
        ready: async () => ({}),
        apiGet: async endpoint => {
            reads.push(endpoint);
            if (endpoint === "get_config") return structuredClone(config);
            if (endpoint === "get_usage_stats") return { success: true, stats: { total: 0, users: [] } };
            if (endpoint === "get_cache_stats") return { success: true, stats: {} };
            throw new Error("Unexpected API read: " + endpoint);
        },
        apiPost: async () => { throw new Error("This test must not save settings"); },
    };
    const context = vm.createContext({
        window: { AstrBotPluginPage: bridge, addEventListener() {} },
        document: {
            body,
            getElementById: id => nodes.get(id) || null,
            querySelector: selector => selector === ".content" ? content : null,
            querySelectorAll: () => [],
            createElement: () => new Element(),
            addEventListener() {},
        },
        console: { error: error => errors.push(error), info() {}, warn() {} },
        URL, structuredClone,
        setTimeout() {}, clearTimeout() {},
    });
    vm.runInContext(source.replace(/init\(\);\s*$/, "globalThis.pageReady = init();"), context);
    await context.pageReady;
    if (errors.length) throw errors[0];
    assert.equal(vm.runInContext("initialized", context), true);
    assert.equal(nodes.get("page-status").querySelector("strong").textContent, "READY");
    assert.equal(body.classes.has("is-loading"), false);
    const payload = JSON.parse(vm.runInContext("JSON.stringify(buildPayload())", context));
    assert.equal(payload.optimizer_config.use_astrbot_provider, optimizerEnabled);
    assert.deepEqual(payload.router_config, config.router_config);
    assert.equal(payload.providers.length, config.providers.length);
    assert.deepEqual(payload.providers.map(provider => provider.model), config.providers.map(provider => provider.model));
    assert.deepEqual(reads.sort(), ["get_cache_stats", "get_config", "get_usage_stats"]);
}

(async () => {
    await checkPage(false);
    await checkPage(true);
    await checkPage(true, true);
    process.stdout.write("PASS: page reaches READY and preserves settings in all 3 cases\n");
})().catch(error => {
    process.stderr.write(error.stack + "\n");
    process.exitCode = 1;
});
