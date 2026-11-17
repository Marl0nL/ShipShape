// Each Ship's FleetLedger tags, by Ship name, for the desk: a space on another machine wears the
// tags of the Ship of the same name. It imports nothing from gpui or herdr: model.js hands it the
// Herdr functions it uses (`host`: readTextFile, exec, home), and the tests hand it a stand-in.
//
// Where they come from, first that answers:
//   1. ~/.ssh/shipshape/fleet.json, which a trusted machine's self-refresh keeps current: this
//      machine's bundle (contracts/machine-bundle.schema.json), checked by the Fleet panel's own
//      reader (fleet-model.js is a link to ../fleet/model.js; install-desk.sh copies the file).
//   2. `ship-fleet ls --json --offline`, for a computer that runs FleetLedger itself and has no
//      self-refresh; also read with the Fleet panel's reader.
// When neither does, `problem` says why, for the desk to show instead of failing silently.

import { fleetFilePath, readFleetFile, readShips, firstLine, TAG } from "./fleet-model.js";

/** How often fleet.json is read again (a small local file; the desk's tick is 1.5 s). */
export const FILE_MS = 30000;
/** How often the local ship-fleet is asked, when there is no fleet.json (as before: 5 minutes). */
export const LEDGER_MS = 300000;

const tagsOf = (list) => [...new Set(list.map((t) => t.toLowerCase()).filter((t) => TAG.test(t)))];

export class ShipTags {
  constructor(host, now = () => Date.now()) {
    this.host = host;
    this.now = now;
    this.tags = new Map();
    this.from = null; // "fleet.json" | "ship-fleet" | null
    this.problem = null; // why fleet.json was ignored, or why there are no Ship tags
    this.fileAt = -Infinity;
    this.lsAt = -Infinity;
    this.ls = null; // the local ship-fleet's last answer
    this.busy = null;
  }

  /** The Ship's tags, or none. */
  get(machine) {
    return this.tags.get(machine ?? "") ?? [];
  }

  /** Read again if due. True when what the desk shows may have changed. Never throws. */
  read() {
    if (this.busy || this.now() - this.fileAt < FILE_MS) return Promise.resolve(false);
    this.busy = this.readNow().finally(() => {
      this.busy = null;
    });
    return this.busy;
  }

  key() {
    return JSON.stringify([this.from, this.problem, [...this.tags]]);
  }

  async readNow() {
    const before = this.key();
    this.fileAt = this.now();
    const path = fleetFilePath(this.host);
    let file;
    try {
      file = readFleetFile(await this.host.readTextFile(path));
    } catch (e) {
      file = { error: `Couldn't read ${path}: ${firstLine(e?.message ?? e) || "no reason given"}` };
    }
    if (file.ships) {
      this.tags = new Map(file.ships.map((s) => [s.name, tagsOf(s.tags)]));
      this.from = "fleet.json";
      this.problem = null;
      return this.key() !== before;
    }
    // No fleet.json (no self-refresh here), or one that is ignored: the local FleetLedger, if any,
    // asked at once when fleet.json has just gone, else every five minutes.
    const ignored = file.error ? `${file.error}; ignored` : null;
    if (this.from === "fleet.json" || !this.ls || this.now() - this.lsAt >= LEDGER_MS) {
      this.lsAt = this.now();
      this.ls = await this.askLedger();
    }
    const ls = this.ls;
    this.tags = ls.ships ? new Map(ls.ships.map((s) => [s.name, tagsOf(s.tags)])) : new Map();
    this.from = ls.ships ? "ship-fleet" : null;
    const why = ls.ships ? [ignored, ls.refused].filter(Boolean).join("; ") : `${ignored ?? `no ${path} (this computer has no self-refresh)`}; ${ls.error}`;
    this.problem = why || null;
    return this.key() !== before;
  }

  async askLedger() {
    let r;
    try {
      r = await this.host.exec("ship-fleet", ["ls", "--json", "--offline"], { timeout: 20000 });
    } catch (e) {
      return { error: `no FleetLedger on this computer (${firstLine(e?.message ?? e) || "ship-fleet did not run"})` };
    }
    if (r.code !== 0) return { error: `ship-fleet ls failed: ${firstLine(r.stderr, r.stdout) || `exit ${r.code}`}` };
    const ls = readShips(r.stdout);
    if (ls.error) return { error: ls.error };
    // Entries the reader refused (an older or newer FleetLedger): say so, never drop them silently.
    return { ships: ls.ships, refused: ls.refused.length ? `ship-fleet ls: left out ${ls.refused.join("; ")}` : null };
  }
}
