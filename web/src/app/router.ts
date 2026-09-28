import { createContext, useContext, useEffect, useState } from "react";

/** Hash routes: #/strategies/STR_... -> ["strategies", "STR_..."]; query after "?". */
export interface Route { parts: string[]; query: URLSearchParams }

function parse(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [path, q = ""] = raw.split("?");
  return { parts: path.split("/").filter(Boolean).map(decodeURIComponent), query: new URLSearchParams(q) };
}

/** Only the shell subscribes to hashchange; everything else reads the route from context, so all
 *  components see the same route in the same render (no page reacts to another page's URL). */
export function useHashRoute(): Route {
  const [route, setRoute] = useState<Route>(parse);
  useEffect(() => {
    const on = () => { setRoute(parse()); window.scrollTo(0, 0); };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export const RouteContext = createContext<Route>({ parts: [], query: new URLSearchParams() });
export const useRoute = (): Route => useContext(RouteContext);

export const go = (path: string) => { window.location.hash = path.startsWith("#") ? path : `#${path}`; };
export const href = (path: string) => `#${path}`;
