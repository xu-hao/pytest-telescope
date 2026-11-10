#!/usr/bin/env node
"use strict";

const {readFileSync, writeFileSync} = require("node:fs");
const {join} = require("node:path");

if (process.argv.length !== 3) process.exit(23);
const input = readFileSync(join(__dirname, "input.txt"), "utf8");
writeFileSync(process.argv[2], input.toUpperCase(), {flag: "wx", mode: 0o600});
process.stdout.write("built\n");
