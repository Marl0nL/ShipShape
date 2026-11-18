// Loads the desk mod's views under Node: "gpui-kit", "gpui-base" and "herdr" resolve to the
// stand-ins beside this file (node --import ./stand-in/register.js, package.json's test script).
import { registerHooks } from "node:module";

const STAND_INS = { "gpui-kit": "./gpui.js", "gpui-base": "./gpui.js", herdr: "./herdr.js" };

registerHooks({
  resolve(specifier, context, next) {
    if (STAND_INS[specifier]) return { url: new URL(STAND_INS[specifier], import.meta.url).href, shortCircuit: true };
    return next(specifier, context);
  },
});
