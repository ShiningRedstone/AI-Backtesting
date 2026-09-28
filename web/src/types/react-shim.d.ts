// Minimal typings for the React APIs this app uses. @types/react could not be installed in
// the offline build environment; with network access run `npm install` and delete this file.
declare module "react" {
  export type Key = string | number;
  export interface ReactElement { type: unknown; props: unknown; key: Key | null }
  export type ReactNode = ReactElement | string | number | boolean | null | undefined | Iterable<ReactNode>;
  export type SetStateAction<S> = S | ((prev: S) => S);
  export type Dispatch<A> = (value: A) => void;
  export function useState<S>(initial: S | (() => S)): [S, Dispatch<SetStateAction<S>>];
  export function useReducer<S, A>(reducer: (s: S, a: A) => S, initial: S): [S, Dispatch<A>];
  export function useEffect(effect: () => void | (() => void), deps?: readonly unknown[]): void;
  export function useMemo<T>(factory: () => T, deps: readonly unknown[]): T;
  export function useCallback<T extends (...args: never[]) => unknown>(cb: T, deps: readonly unknown[]): T;
  export function useRef<T>(initial: T): { current: T };
  export function useId(): string;
  export interface Context<T> { Provider: (props: { value: T; children?: ReactNode }) => ReactElement | null }
  export function createContext<T>(defaultValue: T): Context<T>;
  export function useContext<T>(ctx: Context<T>): T;
  export const Fragment: (props: { children?: ReactNode }) => ReactElement | null;
  export const StrictMode: (props: { children?: ReactNode }) => ReactElement | null;
}
declare module "react-dom/client" {
  import type { ReactNode } from "react";
  export function createRoot(el: Element): { render(node: ReactNode): void };
}
declare module "react/jsx-runtime" {
  export const jsx: unknown;
  export const jsxs: unknown;
  export const Fragment: unknown;
  export namespace JSX {
    type Element = import("react").ReactElement;
    interface IntrinsicElements { [tag: string]: any }
    interface ElementChildrenAttribute { children: object }
    interface IntrinsicAttributes { key?: import("react").Key }
  }
}
