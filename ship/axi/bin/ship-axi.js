#!/usr/bin/env node
// ship-axi entry point. Installed on every Ship as /usr/local/bin/ship-axi.
import { tryFastPath } from "axi-sdk-js/fast-path";
import { VERSION } from "../src/version.js";

if (!tryFastPath(process.argv.slice(2), { version: VERSION })) {
  const { main } = await import("../src/cli.js");
  await main();
}
