#!/usr/bin/env node
"use strict";

// A real CLI driver: Node owns Chromium and speaks its remote-debugging pipe.
const {spawn} = require("node:child_process");
const {createHash} = require("node:crypto");
const {createReadStream, mkdtempSync, rmSync} = require("node:fs");
const {tmpdir} = require("node:os");
const {join} = require("node:path");

async function sha256(path) {
    const hash = createHash("sha256");
    for await (const chunk of createReadStream(path)) hash.update(chunk);
    return hash.digest("hex");
}

async function main() {
    const profile = mkdtempSync(join(tmpdir(), "telescope-owned-browser-"));
    const browser = spawn(process.argv[2], [
        "--headless=new", "--remote-debugging-pipe", "--no-first-run",
        "--disable-background-networking", "--user-data-dir=" + profile,
    ], {stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"], detached: true});
    let exitCode = null;
    let closed = false;
    const exited = new Promise((resolve) => browser.once("close", (code) => {
        exitCode = code;
        closed = true;
        resolve();
    }));
    const pending = new Map();
    const events = new Map();
    let nextId = 0;
    let received = Buffer.alloc(0);
    let stderrBytes = 0;
    let fatal = null;
    const fail = (message) => {
        fatal = new Error(message);
        for (const row of pending.values()) {
            clearTimeout(row.timer);
            row.reject(fatal);
        }
        pending.clear();
        for (const row of events.values()) {
            clearTimeout(row.timer);
            row.reject(fatal);
        }
        events.clear();
    };
    browser.once("error", () => fail("browser process could not start"));
    browser.once("exit", () => fail("browser transport closed"));
    browser.stderr.on("data", (chunk) => {
        stderrBytes += chunk.length;
        if (stderrBytes > 1024 * 1024) {
            fail("browser stderr exceeded its fixture bound");
            browser.kill("SIGTERM");
        }
    });
    browser.stdio[4].on("data", (chunk) => {
        if (received.length + chunk.length > 1024 * 1024) {
            fail("browser protocol exceeded its fixture bound");
            browser.kill("SIGTERM");
            return;
        }
        received = Buffer.concat([received, chunk]);
        let end;
        while ((end = received.indexOf(0)) !== -1) {
            const encoded = received.subarray(0, end);
            received = received.subarray(end + 1);
            let message;
            try { message = JSON.parse(encoded.toString("utf8")); }
            catch { fail("browser returned invalid protocol JSON"); return; }
            if (Object.hasOwn(message, "id")) {
                const row = pending.get(message.id);
                if (!row) continue;
                clearTimeout(row.timer);
                pending.delete(message.id);
                if (message.error) row.reject(new Error("browser rejected a protocol command"));
                else row.resolve(message.result);
            } else {
                const key = message.sessionId + ":" + message.method;
                const row = events.get(key);
                if (!row) continue;
                clearTimeout(row.timer);
                events.delete(key);
                row.resolve(message.params);
            }
        }
    });
    const request = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
        if (fatal) { reject(fatal); return; }
        const id = ++nextId;
        const timer = setTimeout(() => {
            pending.delete(id);
            reject(new Error("browser command deadline exceeded"));
        }, 10000);
        pending.set(id, {resolve, reject, timer});
        browser.stdio[3].write(JSON.stringify({id, method, params, sessionId}) + "\0");
    });
    const event = (sessionId, method) => new Promise((resolve, reject) => {
        const key = sessionId + ":" + method;
        const timer = setTimeout(() => {
            events.delete(key);
            reject(new Error("browser event deadline exceeded"));
        }, 10000);
        events.set(key, {resolve, reject, timer});
    });
    const stop = async () => {
        if (!closed) {
            try {
                browser.stdio[3].write(JSON.stringify({id: ++nextId, method: "Browser.close"}) + "\0");
            } catch {}
            const terminate = setTimeout(() => {
                try { process.kill(-browser.pid, "SIGTERM"); } catch {}
            }, 3000);
            const kill = setTimeout(() => {
                try { process.kill(-browser.pid, "SIGKILL"); } catch {}
            }, 6000);
            let cleanupTimer;
            try {
                await Promise.race([exited, new Promise((_, reject) => {
                    cleanupTimer = setTimeout(() => reject(new Error("browser cleanup deadline exceeded")), 9000);
                })]);
            } finally {
                clearTimeout(terminate);
                clearTimeout(kill);
                clearTimeout(cleanupTimer);
            }
        }
        fail("fixture is closed");
        rmSync(profile, {recursive: true});
    };
    const watchdog = setTimeout(() => {
        fail("browser fixture overall deadline exceeded");
        try { process.kill(-browser.pid, "SIGKILL"); } catch {}
    }, 45000);
    let answer;
    try {
        const target = await request("Target.createTarget", {url: "about:blank"});
        const attached = await request("Target.attachToTarget", {targetId: target.targetId, flatten: true});
        const session = attached.sessionId;
        await request("Page.enable", {}, session);
        const loaded = event(session, "Page.loadEventFired");
        loaded.catch(() => {}); // The same rejection is observed below after navigation.
        const html = "<!doctype html><button id='button' onclick=\"document.getElementById('result').textContent='clicked'\">Click</button><p id='result'>waiting</p>";
        await request("Page.navigate", {url: "data:text/html," + encodeURIComponent(html)}, session);
        await loaded;
        const position = await request("Runtime.evaluate", {
            expression: "JSON.stringify((()=>{const r=document.getElementById('button').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})())",
            returnByValue: true,
        }, session);
        const point = JSON.parse(position.result.value);
        await request("Input.dispatchMouseEvent", {type: "mousePressed", button: "left", clickCount: 1, ...point}, session);
        await request("Input.dispatchMouseEvent", {type: "mouseReleased", button: "left", clickCount: 1, ...point}, session);
        const observed = await request("Runtime.evaluate", {
            expression: "document.getElementById('result').textContent", returnByValue: true,
        }, session);
        if (observed.result.value !== "clicked") throw new Error("real browser click did not update DOM");
        answer = {clicked: observed.result.value, args: process.argv.slice(3), node_sha256: await sha256(process.execPath)};
    } finally {
        clearTimeout(watchdog);
        await stop();
    }
    if (exitCode !== 0) throw new Error("browser did not exit cleanly");
    process.stdout.write(JSON.stringify(answer) + "\n");
}

main().catch((error) => {
    process.stderr.write(error.message + "\n");
    process.exitCode = 1;
});
