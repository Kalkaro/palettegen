---
name: palette-ui
description: "Design, implement, or refine web interfaces in the visual language of this repository's palette generator: image-led editorial layouts, Base16-derived adaptive color systems, restrained dark or light surfaces, lowercase typography, rounded controls, tactile motion, palette swatches, history rails, and polished loading or transition states. Use for new pages, components, mockups, or frontend changes that should match, extend, or borrow from the palette generator UI in palette-showcase.html."
---

# Palette UI

Create interfaces that feel native to the repository's palette generator instead of reproducing it literally.

## Workflow

1. Inspect the target code, framework, existing tokens, and interaction states before editing.
2. Read [references/design-language.md](references/design-language.md) for the source UI's system and implementation rules.
3. Preserve existing product behavior and conventions unless the request explicitly calls for a redesign.
4. Sketch the information hierarchy around one dominant visual or task, then place secondary content in a narrow rail or compact grid.
5. Express colors through semantic custom properties. When a Base16 palette exists, map components to it rather than adding unrelated fixed colors.
6. Implement complete states: initial, loading, success, empty, error, disabled, hover, active, focus-visible, and reduced-motion.
7. Verify the result at desktop, tablet, and narrow mobile widths. Run the repository's relevant checks.

## Design decisions

- Favor an image-first composition, generous negative space, and a clear asymmetry between the primary canvas and supporting content.
- Keep prose and labels lowercase when product copy permits. Use short, direct microcopy.
- Use a neutral sans-serif for interface text and monospace only for machine-readable values such as color names, hashes, or identifiers.
- Use rounded geometry consistently: large media panels, medium cards and swatches, pill primary actions, and circular icon actions.
- Make interaction feedback tactile but quiet. Prefer small lifts, gentle scale changes, fades, and staggered reveals over decorative animation.
- Let generated or user-selected colors recolor the surrounding shell. Retain readable contrast for text placed directly on dynamic colors.
- Avoid generic dashboard styling, dense card grids, excessive borders, gradients used as decoration, and unrelated accent colors.

## Implementation guardrails

- Reuse semantic HTML and native controls. Supply accessible names for icon-only actions.
- Keep focus indicators strong and visible; do not rely on hover alone.
- Use `aria-live` or `role="status"` for meaningful asynchronous updates without creating noisy announcements.
- Respect `prefers-reduced-motion` and keep the interface usable when all animation is effectively disabled.
- Crossfade media only after the incoming asset has loaded; retain the old asset until the new one is ready.
- Prevent stale asynchronous work from overwriting newer user actions by using cancellation, abort signals, or request-version checks.
- Calculate foreground color from luminance for text rendered on arbitrary swatches.
- Treat the source file as evidence, not a template to duplicate wholesale. Reuse only the patterns needed by the requested feature.

## Source of truth

When working inside this repository, inspect `palette-showcase.html` for exact current behavior. If it conflicts with the bundled reference, follow the live source and update the reference when the divergence is intentional.
