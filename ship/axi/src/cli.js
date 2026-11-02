// ship-axi: the Ship's own AXI tool (W7). Read-only in phase 1.

import { existsSync } from "node:fs";
import { AxiError, installSessionStartHooks, runAxiCli } from "axi-sdk-js";
import { VERSION } from "./version.js";
import { homeView, readStatus, RULES } from "./view.js";

export { VERSION };

const INSTALLED = "/usr/local/bin/ship-axi";

const TOP_LEVEL_HELP = `usage: ship-axi [command]

Tells agents about the Ship they are on and how it is managed.

commands:
  (none)        this Ship: name, tags, agents, sign-ins, tools, attention
  rules         what agents may and may not change on a Ship
  setup hooks   add the Ship's context to every new Claude Code session
  request       (not available yet) ask FleetLedger for something
  message       (not available yet) send a note to another Ship
`;

const NOT_YET = {
  request: "ship-axi request will let agents ask FleetLedger for help (a sign-in, a tool).",
  message: "ship-axi message will let agents send a note to an agent on another Ship.",
};

function notAvailable(command) {
  return async () => {
    throw new AxiError(`${NOT_YET[command]} It is not available yet.`, "NOT_AVAILABLE", [
      "Run `ship-axi` to see this Ship's status",
    ]);
  };
}

export async function main(argv = process.argv.slice(2), stdout = process.stdout) {
  await runAxiCli({
    description: "Context about this Ship and how it is managed",
    version: VERSION,
    argv,
    stdout,
    topLevelHelp: TOP_LEVEL_HELP,
    home: async () => {
      const { doc, note } = readStatus();
      return homeView(doc, note);
    },
    commands: {
      rules: async () => RULES,
      setup: async (args) => {
        if (args[0] !== "hooks") {
          throw new AxiError("Unknown setup command", "USAGE", ["Run `ship-axi setup hooks`"]);
        }
        installSessionStartHooks({
          marker: "ship-axi",
          binaryNames: ["ship-axi"],
          distEntrypoints: ["bin/ship-axi.js"],
          // The stable link, not a release folder that a later update removes.
          execPath: existsSync(INSTALLED) ? INSTALLED : undefined,
        });
        return { setup: "hooks installed or already up to date" };
      },
      request: notAvailable("request"),
      message: notAvailable("message"),
      // ship-axi is not published to npm: it updates with the Ship's tooling.
      update: async () => ({
        update: "ship-axi updates with the Ship's tooling",
        help: ["FleetLedger runs `ship-fleet update <ship>`; nothing to do on the Ship"],
      }),
    },
  });
}
