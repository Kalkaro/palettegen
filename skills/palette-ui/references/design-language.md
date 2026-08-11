# Palette generator design language

## Visual character

The interface is an editorial utility: calm, image-led, tactile, and technically precise. It combines a near-black adaptive shell with a large wallpaper canvas, a narrow generation-history rail, and a Base16 swatch dock. The composition is spacious rather than dashboard-dense.

## Tokens

Use a Base16-compatible token set when dynamic palettes are available:

| Role | Preferred token |
| --- | --- |
| Page background | `--base00` |
| Raised surface | `--base01` |
| Subtle border or inactive fill | `--base02` |
| Muted copy | `--base04` |
| Primary copy and control fill | `--base05` |
| High-emphasis copy | `--base07` |
| Generated swatches | `--base00` through `--base0F` |

The source defaults are neutral charcoal (`#18191c`) through off-white (`#fafafa`). Keep hard-coded black or white for overlays and text on photography where palette colors would reduce legibility.

Use `color-mix(in srgb, token percentage, transparent)` for quiet borders, overlays, and glows. Avoid introducing a conventional brand accent unless the product already has one; the generated palette is the accent system.

For arbitrary swatches, derive foreground color from relative luminance. The source switches around luminance `0.38` between near-black `#18191c` and near-white `#fafafa`.

## Typography and copy

- Prefer Figtree, followed by a system sans-serif stack.
- Use a large, tightly tracked display title with a compact line height; the source scales roughly from `2.4rem` to `5.8rem`.
- Use medium weights rather than uniformly bold text.
- Keep supporting labels compact, often between `.62rem` and `.82rem`.
- Use monospace for palette keys and hexadecimal values only.
- Prefer lowercase, short labels: `generate`, `history`, `copy hash`, `generating palette…`.

## Composition

Desktop uses two aligned columns: a fluid primary region and a narrow `180–220px` secondary rail, separated by about `16–26px`. Keep the page centered with a generous maximum width (the source uses `1340px`) and fluid outer padding.

The dominant media surface:

- occupies most of the first viewport;
- uses `object-fit: cover`;
- has a large radius around `28px`;
- includes a subtle deep shadow;
- uses a lower gradient shade for metadata legibility;
- places compact metadata and actions along the lower edge.

Place the palette immediately under the media so it reads as a derived artifact. Use 8 columns on desktop, 4 on tablet, and 2 on narrow phones. Place history in a sticky sidebar on desktop and convert it to an auto-filling thumbnail grid below the main content on smaller screens.

## Component recipes

### Primary action

Use a high-contrast pill with compact type and comfortable horizontal padding. On hover, lift it about `2px` and add a soft shadow; on active, scale it to roughly `.97`. Disabled actions remain visible and communicate waiting.

### Icon action

Use a circular or near-circular button with an inline SVG using `currentColor`. Provide both `aria-label` and `title` when the meaning is not visible in adjacent text.

### Media canvas

Clip the canvas with a stable radius and isolate its paint to prevent transition artifacts. Load a new image invisibly, wait for decoding or `load`, insert it above the previous image, then crossfade. Remove the previous image only after the transition finishes.

### Palette swatch

Use a button so clicking can copy the value. Set color and readable ink through custom properties. Stack the lowercase Base16 name above the hex value near the bottom-left. Reveal swatches with a short stagger; on hover, slightly lift, enlarge, brighten, saturate, and soften the radius.

### History card

Use a `16 / 9` button containing a cover image and a timestamp over a bottom gradient. Apply a slight image zoom and saturation increase on hover. Stagger the initial card reveal but cap the stagger index so long histories do not feel slow.

### Status and toast

Keep progress feedback close to the content it affects. Pair a small spinner with literal status copy. Use a centered bottom pill toast for brief copy confirmations and recoverable errors. Update text content safely; do not inject status strings as HTML.

## Motion

Use `cubic-bezier(.2,.8,.2,1)` for the signature responsive movement. Typical durations:

- press and hover feedback: `220–350ms`;
- content reveals: `450–750ms`;
- color adaptation and media fades: `650–900ms`.

Animate opacity and transforms for most reveals. Use grid-row interpolation when a section must open while preserving flow. Keep animation purposeful: reveal fresh output, communicate waiting, or preserve continuity during replacement.

Under `prefers-reduced-motion: reduce`, reduce all animation and transition durations to effectively instantaneous values and remove delays.

## Responsive behavior

At approximately `780px`:

- stack the header controls below the title;
- collapse the workspace to one column;
- remove sticky positioning from history;
- turn history into an auto-fill grid with cards at least `150px` wide;
- reduce palette columns from 8 to 4;
- slightly reduce the main media radius.

At approximately `430px`:

- reduce page padding to about `18px`;
- reduce palette columns from 4 to 2;
- make swatches wider than tall;
- stack media metadata and its actions when necessary.

Test intermediate widths; do not assume only the named breakpoints matter.

## Interaction and async behavior

- Model busy labels as meaningful phases such as fetching, generating, and complete.
- Allow intentional replacement or skipping only when the backend supports it and make the control state explicit.
- Maintain a monotonically increasing request version, an abort controller, or equivalent so a late response cannot replace a newer selection.
- Keep the prior successful output visible while regenerating when possible.
- Load history independently and fail softly by changing its status rather than breaking the main workflow.
- Use the Clipboard API for copy actions and confirm success with a status message.

## Accessibility checklist

- Use real buttons and links for actions.
- Give icon-only controls accessible names.
- Support keyboard activation for any intentionally button-like non-button element; prefer a button when semantics allow.
- Show focus-visible outlines with strong contrast and an offset.
- Provide useful image alt text for primary content and empty alt text for redundant thumbnails.
- Announce generation and copy results with polite status regions.
- Preserve readable foreground contrast on all dynamic colors.
- Ensure the experience remains coherent with motion disabled.

## Avoid

- Do not turn every region into a bordered card.
- Do not add bright blue or purple accents by habit.
- Do not use glassmorphism across the whole page; reserve translucency for small overlays.
- Do not crowd the media canvas with persistent controls.
- Do not use motion without a state change or hierarchy purpose.
- Do not copy the palette generator's content labels into unrelated products; carry over the system, not the subject matter.
