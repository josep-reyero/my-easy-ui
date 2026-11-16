// Text components arrive as a block tree compiled on the server (see
// docs/SPEC.md, "Compiled form"). This turns the tree into DOM nodes. It never
// parses markdown or HTML; unknown node types are shown as their text.

import { h, safeLink } from "./dom.js";

const MAX_DEPTH = 12;

function inline(nodes, depth = 0) {
  if (!Array.isArray(nodes) || depth > MAX_DEPTH) return [];
  return nodes.map((n) => {
    if (!n || typeof n !== "object") return null;
    switch (n.t) {
      case "text":
        return typeof n.v === "string" ? n.v : "";
      case "b":
        return h("strong", null, inline(n.c, depth + 1));
      case "i":
        return h("em", null, inline(n.c, depth + 1));
      case "a":
        return safeLink(n.href, inline(n.c, depth + 1), "prose-link");
      default:
        return inline(n.c, depth + 1);
    }
  });
}

/** Render blocks; headings start at `baseLevel` (a text heading of level 1 becomes h{baseLevel}). */
export function renderBlocks(blocks, baseLevel = 3) {
  if (!Array.isArray(blocks)) return [];
  return blocks.map((b) => {
    if (!b || typeof b !== "object") return null;
    switch (b.t) {
      case "h": {
        const level = Math.min(4, Math.max(1, Number(b.level) || 1));
        const tag = `h${Math.min(6, baseLevel + level - 1)}`;
        return h(tag, { class: `prose-h prose-h${level}` }, inline(b.c));
      }
      case "p":
        return h("p", null, inline(b.c));
      case "ul":
      case "ol":
        return h(
          b.t,
          null,
          (Array.isArray(b.items) ? b.items : []).map((item) => h("li", null, inline(item))),
        );
      default:
        return h("p", null, inline(b.c));
    }
  });
}
