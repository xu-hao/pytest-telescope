#!/usr/bin/env node
"use strict";

const {createHash} = require("node:crypto");
const {createReadStream, lstatSync, readlinkSync, statSync} = require("node:fs");
const {join} = require("node:path");

async function main() {
    if (process.argv[2] === "exit") process.exit(23);
    if (process.argv[2] === "signal") process.kill(process.pid, "SIGTERM");
    const hash = createHash("sha256");
    for await (const chunk of createReadStream(process.execPath)) hash.update(chunk);
    const directory = process.env.PATH.split(":")[0];
    const command = join(directory, "node");
    process.stdout.write(JSON.stringify({
        args: process.argv.slice(2), node_sha256: hash.digest("hex"),
        command_directory: directory, is_link: lstatSync(command).isSymbolicLink(),
        command_target: readlinkSync(command), directory_mode: statSync(directory).mode & 0o777,
    }) + "\n");
}

main().catch(() => { process.stderr.write("owned Node driver failed\n"); process.exitCode = 1; });
