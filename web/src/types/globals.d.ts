/** Injected by web/build.mjs from edgelab/__init__.py (the one application version). */
declare const __EDGELAB_VERSION__: string;

/** Markdown files are bundled as plain text (web/build.mjs loader). */
declare module "*.md" {
  const text: string;
  export default text;
}
