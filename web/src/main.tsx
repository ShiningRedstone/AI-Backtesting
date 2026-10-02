import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { applyTheme, initialTheme } from "./app/context";

applyTheme(initialTheme());                         // ADR-86: first paint in the remembered theme
createRoot(document.getElementById("root") as HTMLElement).render(<StrictMode><App /></StrictMode>);
