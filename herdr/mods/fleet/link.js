// Herdr's link handler for the Fleet panel (Herdr 0.1.17 and later; older Herdr never loads it):
// a Ctrl/Cmd-click on an https link in one of the panel's job terminals for a Ship opens it in
// the browser and profile settings.json maps that Ship's tag to. Everything else is "pass", and
// Herdr opens it as usual. The logic is in browsers.js; see README.md, "Links in job tabs".

import * as herdr from "herdr";
import { handle } from "./browsers.js";

/** @type {import("herdr").HandleLink} */
export const handleLink = (click) => handle(click, herdr);
