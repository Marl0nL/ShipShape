// ShipShape: a sea chart of the fleet. One mod, in roles (props { role }):
//   chart   the Mediterranean: spaces as ships (one VM, one Ship), agents as their crew, tags as home harbours
//   muster  the muster roll: every agent, the ones that need you first
//   helm    the top bar: the shown ship, its tabs, the fleet's tally, window buttons
//   pane    a pane's header
//   overview  the watch: every agent's last word, shown in the console when no ship is selected

import { View, div } from "gpui-kit";
import { props } from "herdr";
import Chart from "./chart.js";
import Muster from "./muster.js";
import Helm from "./helm.js";
import Pane from "./pane.js";
import Overview from "./overview.js";

const ROLES = { chart: Chart, muster: Muster, helm: Helm, pane: Pane, overview: Overview };

export default class ShipShape extends View {
  init(p, cx) {
    const pr = props() ?? p ?? {};
    const R = ROLES[pr.role];
    this.role = R ? new R() : null;
    this.role?.init?.(pr, cx);
  }

  render(cx) {
    if (!this.role) return div().child(`shipshape: no role "${props()?.role}"`);
    return this.role.render(cx);
  }
}
